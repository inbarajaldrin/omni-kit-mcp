"""Built-in bridge verbs — the transport's own vocabulary, bare-named.

``run_python`` is the universal escape hatch (arbitrary Python on Kit's main
thread, with optional persistent sessions); ``list_tools`` is tool discovery;
``reload_tools`` hot-reloads one autoloaded tool package in place;
``list_bridges`` reports every bridge advertised on this box (so remote
callers, who can't read local portfiles, can find ephemeral-port siblings);
``put_file``/``get_file``/``stat_file`` are the file data plane: move files
to/from the bridge's box and verify what's there, so a remote driver can ship
tool code and assets — and skip unchanged ones — without a side-channel
rsync/scp.

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
    "the module (the submodule named _state is preserved so live handles "
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

    from .sessions import PRELOADED

    # Preloaded convenience symbols — the names come from sessions.PRELOADED
    # (the store's never-persist policy), so injection and policy can't drift.
    preload = {"omni": omni, "carb": carb,
               "Usd": Usd, "UsdGeom": UsdGeom, "Sdf": Sdf, "Gf": Gf}
    assert set(preload) == set(PRELOADED)
    exec_globals = {**preload, "__builtins__": __builtins__}
    if persistent:
        exec_globals.update(bridge.sessions.namespace(session_id))

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

    payload = {
        "output": capture.getvalue(),
        "result": exec_globals.get("result", None),
    }
    if persistent:
        payload["session_id"] = session_id
        payload["session_vars"] = bridge.sessions.persist(session_id, exec_globals)
    return payload


_PUT_FILE_DESCRIPTION = (
    "Write a file on the bridge's box (content base64-encoded). run_python "
    "executes on the box, so code and assets must exist on ITS disk — this "
    "verb ships them from a remote driver without a side-channel rsync/scp. "
    "Writing a .py purges the sibling __pycache__ so the next import can't "
    "serve stale bytecode. Content rides one NDJSON frame in memory: right "
    "for code and tool-sized assets, not multi-GB payloads."
)
_PUT_FILE_PARAMETERS = {
    "path": {"type": "string", "required": True,
             "description": "Destination path on the bridge's box (~ expands)."},
    "content_b64": {"type": "string", "required": True,
                    "description": "File content, base64-encoded."},
    "mkdirs": {"type": "boolean",
               "description": "Create missing parent directories (default true)."},
}


def cmd_put_file(bridge, path: str, content_b64: str, mkdirs: bool = True) -> Dict[str, Any]:
    """Write bytes to the box's disk; the box-side half of remote file push."""
    import base64
    import binascii
    import os

    from .autoload import purge_pycache

    try:
        data = base64.b64decode(content_b64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise ToolError(f"content_b64 is not valid base64: {e}")
    p = os.path.abspath(os.path.expanduser(path))
    parent = os.path.dirname(p)
    if mkdirs and parent:
        os.makedirs(parent, exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    if p.endswith(".py"):
        purge_pycache(parent)   # a pushed edit must never serve stale bytecode
    return {"path": p, "bytes": len(data)}


def cmd_get_file(bridge, path: str) -> Dict[str, Any]:
    """Read a file from the box's disk, base64-encoded; the pull half."""
    import base64
    import os

    p = os.path.abspath(os.path.expanduser(path))
    try:
        with open(p, "rb") as f:
            data = f.read()
    except OSError as e:
        raise ToolError(f"cannot read {p}: {e}")
    return {"path": p, "bytes": len(data),
            "content_b64": base64.b64encode(data).decode("ascii")}


def cmd_stat_file(bridge, path: str) -> Dict[str, Any]:
    """Existence/size/sha256 of a box-side file — the verify half of the file
    data plane. A remote driver ensures instead of blindly pushing: stat, skip
    when the hash matches, push only what changed — and can fail fast naming
    exactly which asset is stale, whatever bulk channel carried it."""
    import hashlib
    import os

    p = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(p):
        return {"path": p, "exists": False}
    h = hashlib.sha256()
    size = 0
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            size += len(chunk)
    return {"path": p, "exists": True, "bytes": size, "sha256": h.hexdigest()}


def cmd_status(bridge) -> Dict[str, Any]:
    """Instance identity over the wire — WHICH bridge answered this socket.

    On a multi-instance box (several bridged Kit processes on distinct ports)
    a client verifies it reached the instance it intended: pid + port +
    headless-vs-headful + app version, plus what's registered. The filesystem
    portfile advertises the same identity, but only same-box clients can read
    it — status is the surface a REMOTE client gets."""
    import os
    import platform
    import socket as _socket
    import time

    from .bridge import ADMISSION_CAPABILITY, HOLD_CAPABILITY, _app_identity

    ident = _app_identity()
    started = bridge._started_at
    ident.update({
        "pid": os.getpid(),
        "port": bridge._port,
        "bind_host": bridge._bind_host,
        "hostname": _socket.gethostname(),
        "python": platform.python_version(),
        "started": started,
        "uptime_s": round(time.time() - started, 1) if started else None,
        "namespaces": sorted(bridge._namespaces),
        "tools": len(bridge.get_registered_tools(include_internal=True)),
        # namespace -> sha256 of the serving module's source tree: a client
        # compares against its local copy to detect the box serving older
        # code than it expects (fingerprinted at registration/reload).
        "toolsets": {o["namespace"]: o["metadata"]["source_hash"]
                     for o in bridge.get_owners().values()
                     if o["metadata"].get("source_hash")},
        # The admission contract this bridge speaks, what each tool module declared, and what
        # is running and waiting — how a caller learns a box holds only one half of the repair.
        "admission": bridge.admission_report(),
        # The capability by name, at the top level where a client looks for it without knowing
        # the report's shape. Absent on a doorway that predates request admission.
        "doorway_admission": ADMISSION_CAPABILITY,
        # THE HOLD (`bridge.HOLD_VERB`): whether one stands, whose, why and what it has refused —
        # never its token. `status` is the one verb a hold does not refuse, so this is readable
        # by everyone while it stands. The capability string is absent on an older doorway.
        "hold": bridge.hold_report(),
        "doorway_hold": HOLD_CAPABILITY,
    })
    return ident


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
    from .bridge import EXCLUSIVE_ATTRIBUTE, ToolDefinition  # deferred: bridge imports this module

    # `run_python` IS EXCLUSIVE, ALWAYS. Its body is whatever the caller sent, and a body may
    # advance the simulator from inside the call (`app.update()`, a `World.step`, a composer's
    # settle) — which destroys every request started beside it. The bridge cannot read the body
    # to find out, so the verb is given the main loop to itself every time. A body that calls
    # back into the doorway in-process is refused `DOORWAY_BUSY` at once; a body that pumps while
    # waiting for a LATER request has that request refused at its deadline, by name, not hung.
    run_python = functools.partial(cmd_run_python, bridge)
    setattr(run_python, EXCLUSIVE_ATTRIBUTE, True)

    return [
        ToolDefinition(
            name="run_python",
            description=_RUN_PYTHON_DESCRIPTION,
            parameters=_RUN_PYTHON_PARAMETERS,
            handler=run_python,
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
        ToolDefinition(
            name="put_file",
            description=_PUT_FILE_DESCRIPTION,
            parameters=_PUT_FILE_PARAMETERS,
            handler=functools.partial(cmd_put_file, bridge),
        ),
        ToolDefinition(
            name="get_file",
            description="Read a file from the bridge box's disk, returned "
                        "base64-encoded (content_b64). Pull half of put_file.",
            parameters={"path": {"type": "string", "required": True,
                                 "description": "Path on the bridge's box (~ expands)."}},
            handler=functools.partial(cmd_get_file, bridge),
        ),
        ToolDefinition(
            name="status",
            description="Instance identity: pid, port, headless-vs-headful, "
                        "app + version, uptime, registered namespaces — verify "
                        "you reached the instance you intended.",
            parameters={},
            handler=functools.partial(cmd_status, bridge),
        ),
        ToolDefinition(
            name="stat_file",
            description="Existence, size, and sha256 of a file on the bridge's "
                        "box — lets a remote driver skip unchanged pushes and "
                        "verify assets instead of blindly re-syncing.",
            parameters={"path": {"type": "string", "required": True,
                                 "description": "Path on the bridge's box (~ expands)."}},
            handler=functools.partial(cmd_stat_file, bridge),
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
