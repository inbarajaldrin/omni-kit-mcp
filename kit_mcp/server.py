"""Generic MCP server (front-end) for the omni.kit.mcp bridge.

The LLM-facing half: a FastMCP server that is a TCP *client* to the Kit-side
bridge. It connects, discovers the registry via ``list_tools``, and registers
each canonical tool dynamically — hardcoding nothing about any project.

One bridge socket serves N of these front-ends concurrently; a "per-project
MCP server" is just this binary with a namespace filter, not a separate port:

    OMNI_KIT_MCP_PORT   bridge port. Optional: unset, the client discovers a
                        lone running bridge via its runtime portfile (and
                        errors loudly on ambiguity). Set it to pin a process.
                        OMNI_KIT_MCP_HOST optional dial override; unset, the
                        discovered bridge's advertised host (else loopback).
    KIT_MCP_NAMESPACE   comma-separated namespace allowlist; unset => all.
    KIT_MCP_BUILTINS    "0" hides the bridge's bare builtins (run_python,
                        reload_tools) — the curated-agent view.
    KIT_MCP_NAME / KIT_MCP_INSTRUCTIONS   cosmetic server identity.

Canonical bridge names are ``<namespace>.<tool>``; MCP tool names cannot
contain dots, so they are exposed as ``<namespace>__<tool>`` and mapped back
on dispatch.

Project-specific POLICY plugs in via three hook lists (it must not live here):
  - register_pre_dispatch_hook(fn): fn(name, params) -> Optional[str]
        May mutate params in place and/or return a refusal string to BLOCK.
  - register_post_dispatch_hook(fn): fn(name, params, result) -> Optional[str]
        Sees every outcome and may return a replacement result (e.g. the same
        answer with a condition of the world annotated onto it), or None to
        leave it. Runs on bridge answers, bridge errors, and pre-hook refusals
        alike — a call that just failed is when a caller most needs telling.
  - register_meta_category_hook(fn): fn(name) -> Optional[str]
        Returns an MCP ``_meta`` category for a tool name, or None.

``kit_mcp.gate`` is the mechanism a policy hook can be written against: a pure
deny-gate over a declared policy file. It, too, holds no policy.
"""

import json
import logging
import os
import re
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from .client import BridgeClient, BridgeError

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("KitMCPServer")

SERVER_NAME = os.getenv("KIT_MCP_NAME", "KitMCP")
SERVER_INSTRUCTIONS = os.getenv(
    "KIT_MCP_INSTRUCTIONS",
    "Omniverse Kit integration through the Model Context Protocol. Tools are "
    "dynamically discovered from the omni.kit.mcp bridge in the running Kit app.")

RESPONSE_TIMEOUT_S = 300.0


def _namespace_filter() -> Optional[set]:
    raw = os.getenv("KIT_MCP_NAMESPACE", "").strip()
    if not raw:
        return None
    return {ns.strip() for ns in raw.split(",") if ns.strip()} or None


def _builtins_enabled() -> bool:
    return os.getenv("KIT_MCP_BUILTINS", "1") not in ("0", "false", "no")


# ---- Policy hooks (populated by the host project; empty => pure passthrough) ----
_PRE_DISPATCH_HOOKS: List[Callable[[str, Dict[str, Any]], Optional[str]]] = []
_POST_DISPATCH_HOOKS: List[Callable[[str, Dict[str, Any], str], Optional[str]]] = []
_META_CATEGORY_HOOKS: List[Callable[[str], Optional[str]]] = []


def register_pre_dispatch_hook(fn: Callable[[str, Dict[str, Any]], Optional[str]]) -> None:
    """Register a pre-dispatch policy hook. fn(name, params) may mutate params
    and return a refusal string to block, or None to proceed."""
    _PRE_DISPATCH_HOOKS.append(fn)


def register_post_dispatch_hook(
        fn: Callable[[str, Dict[str, Any], str], Optional[str]]) -> None:
    """Register a post-dispatch policy hook. fn(name, params, result) may return
    a replacement result, or None to leave it as is.

    It sees every outcome of the call — the bridge's answer, a bridge error, and
    a refusal a pre-dispatch hook returned — because a caller most needs to be
    told the state of the world on the calls that did not work. Hooks run in
    registration order and each sees the previous one's result."""
    _POST_DISPATCH_HOOKS.append(fn)


def register_meta_category_hook(fn: Callable[[str], Optional[str]]) -> None:
    """Register a hook mapping a tool name to an MCP _meta category (or None)."""
    _META_CATEGORY_HOOKS.append(fn)


# ==================== Bridge connection ====================
# The wire protocol lives in kit_mcp.client (the reference client): framing,
# envelope decoding, health checks, and the reconnect-retry are its job. The
# gateway holds one persistent BridgeClient.

_client: Optional[BridgeClient] = None


def get_client() -> BridgeClient:
    global _client
    if _client is None:
        # host/port unset -> the client resolves the endpoint ($OMNI_KIT_MCP_HOST /
        # $OMNI_KIT_MCP_PORT, else portfile discovery) — one resolution path.
        _client = BridgeClient(timeout=RESPONSE_TIMEOUT_S)
    return _client


# ==================== Dynamic tool registration ====================

# MCP-facing name -> canonical bridge name (dots are illegal in MCP tool names).
_NAME_MAP: Dict[str, str] = {}
# MCP-facing name -> the bridge meta it was registered with, so rediscovery can
# detect schema/description changes (reload_tools on the bridge side) and
# re-register instead of silently serving a stale schema.
_REGISTERED_META: Dict[str, Dict[str, Any]] = {}


def registered_tools() -> List[str]:
    """The canonical bridge names currently registered, sorted.

    The roster a policy's matchers can be checked against: a gate naming a tool this does not
    list is gating nothing. Empty before discovery has run, which is itself worth knowing —
    checking a policy against a roster nobody has filled would clear every name in it."""
    return sorted(_NAME_MAP.values())


def _mcp_name(canonical: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", canonical.replace(".", "__"))


def _visible(canonical: str, meta: Dict[str, Any]) -> bool:
    """Apply the view filters: namespace allowlist + builtins gate."""
    namespace = meta.get("namespace")
    if namespace is None:  # bridge builtins (run_python, reload_tools)
        return _builtins_enabled()
    allow = _namespace_filter()
    return allow is None or namespace in allow


def _drop_tool(server: FastMCP, name: str) -> None:
    """Remove one tool from FastMCP so it can be re-registered with a new
    schema. FastMCP has no public remove; its ToolManager keeps a plain dict —
    this is the one sanctioned private-attr touch, and it lives only here."""
    server._tool_manager._tools.pop(name, None)


def discover_and_register_tools(server: FastMCP) -> List[str]:
    """Query the bridge registry and register each visible tool.

    New tools are added; tools whose bridge meta (description/parameters)
    changed since their registration — e.g. after a bridge-side reload_tools —
    are RE-registered so the served schema never goes stale. Returns the
    canonical names added or updated."""
    result = get_client().call("list_tools")
    tools = result.get("tools", {})

    changed = []
    for canonical, meta in sorted(tools.items()):
        if not _visible(canonical, meta):
            continue
        name = _mcp_name(canonical)
        if name in _NAME_MAP:
            if _REGISTERED_META.get(name) == meta:
                continue
            _drop_tool(server, name)          # schema/description changed
            changed.append(f"{canonical} (updated)")
        else:
            changed.append(canonical)
        _NAME_MAP[name] = canonical
        _REGISTERED_META[name] = meta
        register_dynamic_tool(server, name, canonical, meta)
    if changed:
        logger.info(f"Registered/updated {len(changed)} bridge tools: {changed}")
    return changed


_KIND_ANNOTATIONS = {"boolean": bool, "integer": int, "number": float,
                     "array": list, "object": dict, "string": str}


def _build_tool_function(name: str, canonical: str, description: str,
                         fields: List[Dict[str, Any]]):
    """Build a function with a real signature/annotations for FastMCP schemas.

    ``fields`` are the bridge-served field specs (the schema vocabulary is
    interpreted once, bridge-side — schema.py); this only renders them.
    Required fields without a declared default become required parameters;
    optional fields default to their declared default, else None — and None
    values are dropped before dispatch so bridge-side handler defaults apply.
    """
    from typing import Literal, Optional

    # def-order: required-without-default parameters must precede defaulted ones.
    ordered = sorted(fields, key=lambda f: not (f["required"] and not f["has_default"]))
    param_list = []
    annotations: Dict[str, Any] = {"return": str}
    for f in ordered:
        annotation = (Literal[tuple(f["enum"])] if f["kind"] == "enum"
                      else _KIND_ANNOTATIONS[f["kind"]])
        if f["required"] and not f["has_default"]:
            param_list.append(f["name"])
        else:
            default = f["default"] if f["has_default"] else None
            if default is None:
                annotation = Optional[annotation]
            param_list.append(f"{f['name']}={default!r}")
        annotations[f["name"]] = annotation

    params_str = ", ".join(param_list)
    # No docstring in the exec'd source — descriptions are arbitrary text and
    # embedding them invites quote/newline breakage; set __doc__ after.
    func_code = (
        f"def {name}({params_str}) -> str:\n"
        f"    return _tool_impl({canonical!r}, locals())\n"
    )
    local_ns = {"_tool_impl": _tool_implementation}
    exec(func_code, local_ns)
    func = local_ns[name]
    func.__doc__ = description
    func.__annotations__.update(annotations)
    return func


def _tool_implementation(canonical: str, params: Dict[str, Any]) -> str:
    """Shared implementation for all dynamic tools.

    Every call crosses both policy hook lists, which is the point of putting them
    here: this is where every path lands, raw-socket callers included. The result
    a post-dispatch hook sees is the one the caller would have received, whatever
    produced it."""
    # Drop unset optional params so bridge-side handler defaults apply — and so a
    # policy hook judges the call the bridge will actually receive rather than the
    # signature's placeholders.
    params = {k: v for k, v in params.items() if v is not None}
    return _post_dispatch(canonical, params, _dispatch(canonical, params))


def _dispatch(canonical: str, params: Dict[str, Any]) -> str:
    """Run the pre-dispatch hooks and, if none refused, the bridge call."""
    # Each hook may mutate params in place and/or return a refusal string to BLOCK
    # the call. A buggy hook degrades to an error string rather than crashing the
    # dispatch — and blocks, because a policy that did not run is not a policy that
    # allowed anything.
    for hook in _PRE_DISPATCH_HOOKS:
        try:
            blocked = hook(canonical, params)
        except Exception as e:
            hook_name = getattr(hook, "__name__", repr(hook))
            logger.error(f"Pre-dispatch hook {hook_name} raised for {canonical}: {e}")
            return f"Error: pre-dispatch hook {hook_name} failed: {e}"
        if blocked:
            return blocked

    try:
        result = get_client().call(canonical, params)
        return json.dumps(result, indent=2, default=repr)
    except BridgeError as e:
        detail = f"\n{json.dumps(e.details, default=repr)[:2000]}" if e.details else ""
        logger.error(f"Bridge error in {canonical}: {e}")
        return f"Error: {e}{detail}"
    except (OSError, TimeoutError, RuntimeError) as e:
        logger.error(f"Error in {canonical}: {e}")
        return f"Error: {e}"


def _post_dispatch(canonical: str, params: Dict[str, Any], result: str) -> str:
    """Let each post-dispatch hook replace the result, in registration order.

    A hook that raises is reported and skipped, never allowed to cost the caller an
    answer the bridge already gave: an annotation failing is not the call failing."""
    for hook in _POST_DISPATCH_HOOKS:
        try:
            replacement = hook(canonical, params, result)
        except Exception as e:
            hook_name = getattr(hook, "__name__", repr(hook))
            logger.error(f"Post-dispatch hook {hook_name} raised for {canonical}: {e}")
            continue
        if replacement is not None:
            result = replacement
    return result


def register_dynamic_tool(server: FastMCP, name: str, canonical: str,
                          meta: Dict[str, Any]) -> None:
    description = meta.get("description", f"Execute {canonical} on the Kit bridge")
    if "fields" not in meta:
        raise RuntimeError(
            f"bridge served no field specs for {canonical!r} — the parameter "
            "schema is interpreted bridge-side (omni_kit_mcp.schema); update "
            "the omni.kit.mcp extension to match this gateway.")
    handler = _build_tool_function(name, canonical, description, meta["fields"])

    # Optional _meta category from policy hooks (failures are non-fatal).
    category = None
    for hook in _META_CATEGORY_HOOKS:
        try:
            category = hook(canonical)
        except Exception as e:
            logger.error(f"Meta-category hook failed for {canonical}: {e}")
            category = None
        if category is not None:
            break

    if category is not None:
        server.tool(meta={"category": category})(handler)
    else:
        server.tool()(handler)


# ==================== Server ====================

@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    try:
        logger.info("KitMCP server starting up")
        try:
            discover_and_register_tools(server)
        except Exception as e:
            logger.warning(f"Could not reach the Kit bridge on startup: {e}")
            logger.warning("Start Kit with omni.kit.mcp enabled, then call refresh_tools.")
        yield {}
    finally:
        global _client
        if _client:
            logger.info("Disconnecting from the Kit bridge")
            _client.close()
            _client = None
        logger.info("KitMCP server shut down")


mcp = FastMCP(SERVER_NAME, instructions=SERVER_INSTRUCTIONS, lifespan=server_lifespan)


@mcp.tool()
def refresh_tools() -> str:
    """Re-discover the bridge registry and register any new tools (use after
    the bridge's reload_tools, after enabling a project, or after Kit restarts)."""
    try:
        added = discover_and_register_tools(mcp)
    except Exception as e:
        return f"Error: {e}"
    return (f"Registered/updated {len(added)} tool(s): {added}" if added
            else "No new or changed tools (registry unchanged).")


def main() -> None:
    """Run the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
