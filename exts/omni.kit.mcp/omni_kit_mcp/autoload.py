"""Consumer-tool auto-loader for the omni.kit.mcp bridge.

The project contract: a tool project is a plain Python package — NOT a Kit
extension — that declares its identity and exposes one entrypoint:

    MCP_NAMESPACE = "arm"                 # public tool prefix (defaults to the
                                          # module name if omitted)

    def register(registrar):              # or register_tools(registrar)
        registrar.add(ToolDefinition(...))
        # or: @registrar.tool("desc", {...params})

The autoloader owns the owner lifecycle: it calls ``register_owner`` (owner_id
= module name, the plain-package analogue of ext_id) and hands the entrypoint
an owner-bound ``OwnerRegistrar`` — authors never manage owner ids, teardown,
or the socket. Tools are advertised canonically as ``<namespace>.<tool>``.

Where the module list comes from (first non-empty source wins; env is the
per-launch override, settings are the persistent registration written by
``scripts/install.py add-project``):

    knobs.TOOL_MODULES / knobs.TOOL_PATHS  (env override, else persistent
    settings — knobs.py is the single home of every knob's identity)

Paths are prepended to sys.path (what --ext-folder does for extensions);
modules are imported and registered. Per-module failures are isolated so one
bad project can't block the others or wedge bridge startup.

Hot reload: ``reload_tool_module`` (backing the ``reload_tools`` builtin)
unregisters the module's owner (draining in-flight work), purges the module
tree from sys.modules — EXCEPT submodules named ``*_state``, the documented
home for live handles that must survive a reload — re-imports, re-registers.
"""

import importlib
import os
import re
import sys
from typing import Any, Dict, List, Optional

try:
    import carb
except ImportError:  # outside Kit (lint / standalone tests) — stay pure-stdlib
    carb = None


def _log(level: str, msg: str) -> None:
    if carb is not None:
        getattr(carb, f"log_{level}")(msg)
    else:
        print(msg)


# Knob identities (env names, settings keys, precedence) live in knobs.py —
# defined once, shared with extension.py and scripts/install.py.
from . import knobs

# Entrypoint names a tool package may expose, in priority order.
_ENTRYPOINTS = ("register", "register_tools")

# Submodule basenames preserved across reload_tool_module — live-state homes.
_STATE_SUFFIX = "_state"


def _split_list(raw) -> List[str]:
    """Split a comma/whitespace-separated string (or pass a list through)."""
    if isinstance(raw, (list, tuple)):
        return [str(m).strip() for m in raw if str(m).strip()]
    return [m for m in re.split(r"[,\s]+", (raw or "").strip()) if m]


def resolve_tool_modules() -> List[str]:
    """Module list: env override if set, else persistent settings."""
    env = _split_list(knobs.TOOL_MODULES.read_env())
    return env if env else _split_list(knobs.TOOL_MODULES.read_settings())


def resolve_tool_paths() -> List[str]:
    """sys.path additions: env override if set, else persistent settings.

    The env form is os.pathsep-separated (shell convention); the settings form
    is a list (or comma/whitespace string) like every other list knob.
    """
    env = knobs.TOOL_PATHS.read_env()
    if env:
        return [p.strip() for p in env.split(os.pathsep) if p.strip()]
    return _split_list(knobs.TOOL_PATHS.read_settings())


def has_autoload_request() -> bool:
    """True if env or settings name any tool modules to load."""
    return bool(resolve_tool_modules())


def prepend_tool_paths(paths: Optional[List[str]] = None) -> List[str]:
    """Prepend tool dirs onto sys.path. Returns the dirs actually added."""
    if paths is None:
        paths = resolve_tool_paths()
    added = []
    for p in paths:
        ap = os.path.abspath(os.path.expanduser(p))
        if os.path.isdir(ap) and ap not in sys.path:
            sys.path.insert(0, ap)
            added.append(ap)
    return added


def _namespace_for(mod, mod_name: str) -> str:
    """The package's declared MCP_NAMESPACE, defaulting to its module name."""
    ns = getattr(mod, "MCP_NAMESPACE", None)
    if not ns:
        ns = mod_name.rsplit(".", 1)[-1]
    return str(ns)


def _register_one(bridge, mod_name: str) -> Dict[str, Any]:
    """Import one tool package, register its owner, run its entrypoint.

    Returns {"namespace": ..., "tools": [...]}. On entrypoint failure the
    half-registered owner is rolled back so a retry/reload starts clean.
    """
    mod = importlib.import_module(mod_name)
    entry = next(
        (getattr(mod, n) for n in _ENTRYPOINTS if callable(getattr(mod, n, None))),
        None)
    if entry is None:
        raise ValueError(f"no {' or '.join(_ENTRYPOINTS)}(registrar) entrypoint")

    namespace = _namespace_for(mod, mod_name)
    registrar = bridge.register_owner(
        owner_id=mod_name, namespace=namespace, metadata={"module": mod_name})
    try:
        entry(registrar)
    except Exception:
        bridge.unregister_owner(mod_name)
        raise
    tools = sorted(bridge.get_owners().get(mod_name, {}).get("tools", []))
    return {"namespace": namespace, "tools": tools}


def load_tool_modules(bridge, modules: Optional[List[str]] = None,
                      paths: Optional[List[str]] = None) -> Dict[str, Any]:
    """Load + register every configured tool package.

    Returns {module: {"namespace", "tools"} | "ERROR: ..."} for logging.
    """
    prepend_tool_paths(paths)
    if modules is None:
        modules = resolve_tool_modules()

    results: Dict[str, Any] = {}
    for mod_name in modules:
        try:
            results[mod_name] = _register_one(bridge, mod_name)
            _log("info", f"[omni.kit.mcp] autoload: {mod_name} -> {results[mod_name]}")
        except Exception as e:
            results[mod_name] = f"ERROR: {e}"
            _log("error", f"[omni.kit.mcp] autoload: failed to load {mod_name}: {e}")
    return results


def _purge_bytecode(module: str) -> None:
    """Delete __pycache__ trees under a tool package so re-import recompiles."""
    import importlib.util
    import shutil
    try:
        spec = importlib.util.find_spec(module)
    except Exception:
        return
    if spec is None or not spec.origin or spec.origin in ("built-in", "frozen"):
        return
    if spec.submodule_search_locations:  # package: purge caches in its tree
        for root in spec.submodule_search_locations:
            for dirpath, dirnames, _ in os.walk(root):
                if "__pycache__" in dirnames:
                    shutil.rmtree(os.path.join(dirpath, "__pycache__"),
                                  ignore_errors=True)
    else:  # single-file module
        shutil.rmtree(os.path.join(os.path.dirname(spec.origin), "__pycache__"),
                      ignore_errors=True)


def reload_tool_module(bridge, module: str) -> Dict[str, Any]:
    """Hot-reload one autoloaded tool package (backs the reload_tools builtin).

    Unregister (draining), purge the module tree from sys.modules except
    ``*_state`` submodules, re-import, re-register. Live handles parked in a
    state module survive; everything else picks up fresh code.
    """
    # Reload doubles as FIRST load (a project added after Kit launch comes up
    # without a restart) — so make sure the configured tool paths are on
    # sys.path, exactly as startup's load_tool_modules would have done.
    prepend_tool_paths()

    owner_id = bridge.find_owner_by_module(module)
    if owner_id is None:
        _log("warn", f"[omni.kit.mcp] reload_tools: {module} was not loaded; loading fresh")
    else:
        bridge.unregister_owner(owner_id)

    prefix = module + "."
    purged = [name for name in list(sys.modules)
              if (name == module or name.startswith(prefix))
              and not name.rsplit(".", 1)[-1].endswith(_STATE_SUFFIX)]
    for name in purged:
        del sys.modules[name]

    # Drop cached bytecode too: .pyc validation is (mtime, size), so an edit
    # within the same second that keeps the size constant — routine in an
    # agent's edit->reload cycle — would otherwise serve stale code.
    importlib.invalidate_caches()
    _purge_bytecode(module)

    info = _register_one(bridge, module)
    return {"module": module, "reloaded": purged, **info}
