"""Generic MCP server (front-end) for the omni.kit.mcp bridge.

The LLM-facing half: a FastMCP server that is a TCP *client* to the Kit-side
bridge. It connects, discovers the registry via ``list_tools``, and registers
each canonical tool dynamically — hardcoding nothing about any project.

One bridge socket serves N of these front-ends concurrently; a "per-project
MCP server" is just this binary with a namespace filter, not a separate port:

    OMNI_KIT_MCP_PORT   bridge port. Optional: unset, the client discovers a
                        lone running bridge via its runtime portfile (and
                        errors loudly on ambiguity). Set it to pin a process.
                        OMNI_KIT_MCP_HOST optional, default localhost.
    KIT_MCP_NAMESPACE   comma-separated namespace allowlist; unset => all.
    KIT_MCP_BUILTINS    "0" hides the bridge's bare builtins (run_python,
                        reload_tools) — the curated-agent view.
    KIT_MCP_NAME / KIT_MCP_INSTRUCTIONS   cosmetic server identity.

Canonical bridge names are ``<namespace>.<tool>``; MCP tool names cannot
contain dots, so they are exposed as ``<namespace>__<tool>`` and mapped back
on dispatch.

Project-specific POLICY plugs in via two hook lists (it must not live here):
  - register_pre_dispatch_hook(fn): fn(name, params) -> Optional[str]
        May mutate params in place and/or return a refusal string to BLOCK.
  - register_meta_category_hook(fn): fn(name) -> Optional[str]
        Returns an MCP ``_meta`` category for a tool name, or None.
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


def _resolve_host() -> str:
    return os.getenv("OMNI_KIT_MCP_HOST", "localhost")


def _namespace_filter() -> Optional[set]:
    raw = os.getenv("KIT_MCP_NAMESPACE", "").strip()
    if not raw:
        return None
    return {ns.strip() for ns in raw.split(",") if ns.strip()} or None


def _builtins_enabled() -> bool:
    return os.getenv("KIT_MCP_BUILTINS", "1") not in ("0", "false", "no")


# ---- Policy hooks (populated by the host project; empty => pure passthrough) ----
_PRE_DISPATCH_HOOKS: List[Callable[[str, Dict[str, Any]], Optional[str]]] = []
_META_CATEGORY_HOOKS: List[Callable[[str], Optional[str]]] = []


def register_pre_dispatch_hook(fn: Callable[[str, Dict[str, Any]], Optional[str]]) -> None:
    """Register a pre-dispatch policy hook. fn(name, params) may mutate params
    and return a refusal string to block, or None to proceed."""
    _PRE_DISPATCH_HOOKS.append(fn)


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
        _client = BridgeClient(host=_resolve_host(), port=None,   # port: $OMNI_KIT_MCP_PORT
                               timeout=RESPONSE_TIMEOUT_S)
    return _client


# ==================== Dynamic tool registration ====================

# MCP-facing name -> canonical bridge name (dots are illegal in MCP tool names).
_NAME_MAP: Dict[str, str] = {}
# MCP-facing name -> the bridge meta it was registered with, so rediscovery can
# detect schema/description changes (reload_tools on the bridge side) and
# re-register instead of silently serving a stale schema.
_REGISTERED_META: Dict[str, Dict[str, Any]] = {}


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


def _build_tool_function(name: str, canonical: str, description: str,
                         tool_params: Dict[str, Any]):
    """Build a function with a real signature/annotations for FastMCP schemas."""
    from typing import Literal

    param_list = []
    annotations: Dict[str, Any] = {"return": str}
    for param_name, param_def in tool_params.items():
        ptype = param_def.get("type", "string")
        enum_values = param_def.get("enum")
        if enum_values:
            annotation, default = Literal[tuple(enum_values)], enum_values[0]
        elif ptype == "boolean":
            annotation, default = bool, False
        elif ptype == "integer":
            annotation, default = int, 0
        elif ptype == "number":
            annotation, default = float, 0.0
        elif ptype == "array":
            annotation, default = list, None
        elif ptype == "object":
            annotation, default = dict, None
        else:
            annotation, default = str, ""
        param_list.append(f"{param_name}={default!r}")
        annotations[param_name] = annotation

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
    """Shared implementation for all dynamic tools."""
    # Drop unset optional params so bridge-side handler defaults apply.
    params = {k: v for k, v in params.items() if v is not None}

    # Pre-dispatch policy hooks (host project plugs these in): each may mutate
    # params in place and/or return a refusal string to BLOCK the call. A buggy
    # hook degrades to an error string rather than crashing the dispatch.
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


def register_dynamic_tool(server: FastMCP, name: str, canonical: str,
                          meta: Dict[str, Any]) -> None:
    description = meta.get("description", f"Execute {canonical} on the Kit bridge")
    handler = _build_tool_function(name, canonical, description,
                                   meta.get("parameters", {}))

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
