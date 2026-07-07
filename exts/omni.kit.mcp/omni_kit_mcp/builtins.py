"""Built-in bridge verbs — the transport's own vocabulary, bare-named.

``run_python`` is the universal escape hatch (arbitrary Python on Kit's main
thread, with optional persistent sessions); ``list_tools`` is tool discovery;
``reload_tools`` hot-reloads one autoloaded tool package in place;
``list_bridges`` reports every bridge advertised on this box (so remote
callers, who can't read local portfiles, can find ephemeral-port siblings).

Handlers here follow the same contract as any tool: return the payload dict,
or raise (``ToolError`` to attach diagnostics). The bridge owns the envelope.
"""

import functools
import traceback
from typing import Any, Dict, List

from .protocol import ToolError

_RUN_PYTHON_DESCRIPTION = (
    "Execute Python code on the Kit main thread with full access to the "
    "Omniverse APIs (omni, carb, pxr preloaded; assign to `result` to return a "
    "value). Set persistent=True with a session_id to keep variables alive "
    "between calls — useful for multi-step workflows where later calls need "
    "results from earlier ones."
)
_RUN_PYTHON_PARAMETERS = {
    "code": {"type": "string", "required": True,
             "description": "Python code to execute on Kit's main thread"},
    "session_id": {
        "type": "string",
        "description": "Session identifier for persistent execution. Use the same ID "
                       "across calls to share state. Defaults to 'default'.",
    },
    "persistent": {
        "type": "boolean",
        "description": "If true, variables persist across calls with the same session_id.",
    },
}

_RELOAD_TOOLS_DESCRIPTION = (
    "Hot-reload one autoloaded tool package: unregister its owner, re-import "
    "the module (submodules named *_state are preserved so live handles "
    "survive), and re-register its tools. Follow with the MCP front-end's "
    "refresh so new schemas are rediscovered."
)
_RELOAD_TOOLS_PARAMETERS = {
    "module": {"type": "string", "required": True,
               "description": "Tool-package module name as registered "
                              "(see list_tools namespaces / owners)."},
}


def cmd_run_python(bridge, code: str, session_id: str = "default",
                   persistent: bool = False) -> Dict[str, Any]:
    """Execute Python on Kit's main thread; optionally persist the namespace.

    Since everything is in-process, persisted values (functions, prim
    references, numpy arrays) survive across calls with the same session_id.
    """
    import io
    import sys

    import carb
    import omni
    from pxr import Gf, Sdf, Usd, UsdGeom

    # Preloaded convenience symbols — not user-defined, never saved to sessions.
    _builtin_keys = {"omni", "carb", "Usd", "UsdGeom", "Sdf", "Gf", "__builtins__"}

    exec_globals = {
        "omni": omni, "carb": carb,
        "Usd": Usd, "UsdGeom": UsdGeom, "Sdf": Sdf, "Gf": Gf,
        "__builtins__": __builtins__,
    }
    if persistent and session_id in bridge._python_sessions:
        exec_globals.update(bridge._python_sessions[session_id])

    old_stdout = sys.stdout
    sys.stdout = capture = io.StringIO()
    try:
        exec(code, exec_globals)
    except Exception as e:
        raise ToolError(str(e), details={
            "output": capture.getvalue(),
            "traceback": traceback.format_exc(),
        })
    finally:
        sys.stdout = old_stdout

    if persistent:
        bridge._python_sessions[session_id] = {
            k: v for k, v in exec_globals.items()
            if not k.startswith("_") and k not in _builtin_keys
        }

    payload = {
        "output": capture.getvalue(),
        "result": exec_globals.get("result", None),
    }
    if persistent:
        payload["session_id"] = session_id
        payload["session_vars"] = list(bridge._python_sessions.get(session_id, {}).keys())
    return payload


def cmd_list_bridges(bridge) -> Dict[str, Any]:
    """All bridges advertised on THIS box (live portfiles, self included).

    The runtime dir is local to the box, so remote callers can't read it —
    but they can ask any reachable bridge (typically the stable installed-app
    port) to report its siblings, e.g. ephemeral-port instances."""
    import json as _json
    import os as _os
    from .bridge import _runtime_dir

    found = []
    try:
        names = _os.listdir(_runtime_dir())
    except OSError:
        return {"bridges": found}
    for name in names:
        try:
            with open(_os.path.join(_runtime_dir(), name)) as f:
                info = _json.load(f)
            _os.kill(int(info["pid"]), 0)   # liveness
            found.append(info)
        except Exception:
            continue
    return {"bridges": found}


def cmd_list_tools(bridge) -> Dict[str, Any]:
    """Discovery: the advertised registry, canonical names only."""
    return {"tools": bridge.get_registered_tools()}


def cmd_reload_tools(bridge, module: str) -> Dict[str, Any]:
    """Hot-reload one autoloaded tool package in place (see autoload.py)."""
    from .autoload import reload_tool_module
    return reload_tool_module(bridge, module)


def builtin_tools(bridge) -> List:
    """The ToolDefinitions the bridge registers on itself at construction."""
    from .bridge import ToolDefinition  # deferred: bridge imports this module

    return [
        ToolDefinition(
            name="run_python",
            description=_RUN_PYTHON_DESCRIPTION,
            parameters=_RUN_PYTHON_PARAMETERS,
            handler=functools.partial(cmd_run_python, bridge),
        ),
        ToolDefinition(
            name="list_bridges",
            description="List all omni.kit.mcp bridges advertised on this box "
                        "(this Kit process and any siblings, e.g. a second "
                        "instance on an ephemeral port).",
            parameters={},
            handler=functools.partial(cmd_list_bridges, bridge),
        ),
        ToolDefinition(
            name="reload_tools",
            description=_RELOAD_TOOLS_DESCRIPTION,
            parameters=_RELOAD_TOOLS_PARAMETERS,
            handler=functools.partial(cmd_reload_tools, bridge),
        ),
        # Served like any tool, hidden from its own listing.
        ToolDefinition(
            name="list_tools",
            description="List all tools registered on this MCP bridge.",
            parameters={},
            handler=functools.partial(cmd_list_tools, bridge),
            internal=True,
        ),
    ]
