"""Consumer-tool auto-loader for the omni.kit.mcp bridge.

The project contract: a tool project is a plain Python package — NOT a Kit
extension — that declares its identity and, optionally, an entrypoint:

    MCP_NAMESPACE = "arm"                 # public tool prefix (defaults to the
                                          # module name if omitted)

    def register(registrar):              # or register_tools(registrar)
        registrar.add(ToolDefinition(...))
        # or: @registrar.tool("desc", {...params})

A package with no entrypoint is a DECLARED TOOLSET: its public functions
register as the tools directly, schemas derived from signatures (toolset.py).

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
modules are imported and registered. A module that fails is rolled back and
the rest are still attempted, so one pass names EVERY module that cannot load.

AT BOOT A FAILED MODULE REFUSES THE DOORWAY (``load_configured_tool_modules``).
The modules were CONFIGURED: somebody is going to dial their tools by name. A
doorway that skipped a module that failed to import would come up looking
healthy and answer ``unknown tool`` later, one layer away from the cause. So
boot does not skip: it raises ``ToolModuleLoadRefusal`` naming every failed
module with its full traceback, serves none of the configured modules, and
``refuse_boot`` writes a record a launcher can read, holds the refusal for
``get_mcp_bridge`` to raise, and asks Kit to quit — a boot failure with its
cause, never a doorway that is missing a namespace. A windowed, hand-launched
app quits with the same refusal.

Hot reload: ``reload_tool_module`` (backing the ``reload_tools`` builtin)
unregisters the module's owner (draining in-flight work), purges the module
tree from sys.modules — EXCEPT the submodule named ``_state``, the documented
home for live handles that must survive a reload — re-imports, re-registers.
"""

import importlib
import json
import os
import re
import sys
import time
import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import carb
except ImportError:  # outside Kit (lint / standalone tests) — stay pure-stdlib
    carb = None


def _log(level: str, msg: str) -> None:
    if carb is not None:
        getattr(carb, f"log_{level}")(msg)
    else:
        print(msg, file=sys.stderr if level == "error" else sys.stdout)


# Knob identities (env names, settings keys, precedence) live in knobs.py —
# defined once, shared with extension.py and scripts/install.py.
from . import knobs

# Entrypoint names a tool package may expose, in priority order.
_ENTRYPOINTS = ("register", "register_tools")
#: Optional, and asked for separately from the tool entrypoint — see _install_policy.
_POLICY_ENTRYPOINT = "install_policy"

# Submodule basenames preserved across reload_tool_module — live-state homes.
_STATE_MODULE = "_state"   # exact leaf name, not a suffix (34c6830)


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
    """Import one tool package and register it — via its entrypoint when it
    has one, else as a DECLARED TOOLSET (its public functions become the
    tools; see toolset.py).

    Returns {"namespace": ..., "tools": [...]}. On failure the
    half-registered owner is rolled back so a retry/reload starts clean.
    """
    from .bridge import ADMISSION_DECLARATION
    from .toolset import register_toolset, source_hash

    mod = importlib.import_module(mod_name)
    entry = next(
        (getattr(mod, n) for n in _ENTRYPOINTS if callable(getattr(mod, n, None))),
        None)
    if entry is None:
        declared = register_toolset(bridge, mod, owner_id=mod_name,
                                    namespace=_namespace_for(mod, mod_name))
        try:
            _install_policy(bridge, mod, mod_name)
        except Exception:
            # Rolled back for the same reason the entrypoint path rolls back, and it is the
            # sharper case: a package whose TOOLS registered and whose GATE did not leaves
            # callable tools with nothing refusing them — armed-looking and open.
            bridge.unregister_owner(mod_name)
            raise
        return declared

    namespace = _namespace_for(mod, mod_name)
    registrar = bridge.register_owner(
        owner_id=mod_name, namespace=namespace,
        metadata={"module": mod_name, "source_hash": source_hash(mod),
                  "admission": getattr(mod, ADMISSION_DECLARATION, None)})
    try:
        entry(registrar)
        _install_policy(bridge, mod, mod_name)
    except Exception:
        bridge.unregister_owner(mod_name)
        raise
    tools = sorted(bridge.get_owners().get(mod_name, {}).get("tools", []))
    return {"namespace": namespace, "tools": tools}



def _install_policy(bridge, mod, mod_name: str) -> None:
    """Let a tool package install a call-refusing policy, if it declares one.

    Optional and separate from ``register`` on purpose. A DECLARED TOOLSET has no entrypoint —
    its public functions are its tools — and a package should not have to give that up to say
    which calls may not run. So the two are asked for independently:

        def install_policy(registrar):        # optional, either style of package
            registrar.refuse_calls_with(fn)

    A policy that cannot be installed FAILS THE LOAD rather than being skipped. A tool package
    whose tools registered and whose gate did not is the shape that looks armed and is not.
    """
    hook = getattr(mod, _POLICY_ENTRYPOINT, None)
    if not callable(hook):
        return
    registrar = bridge.registrar_for(mod_name)
    hook(registrar)
    _log("info", f"[omni.kit.mcp] {mod_name} installed a call policy")


#: The boot refusal's name — what a log reader and a cell's bringup match on.
TOOL_MODULE_LOAD_REFUSED = "DOORWAY_TOOL_MODULE_LOAD_REFUSED"
#: The record ``refuse_boot`` writes: ``<log dir>/boot-refusal-<pid>-<ns stamp>.json``, in the
#: directory the doorway's other diary (``tool-failures-<pid>.log``) already lives in. The stamp
#: is in the NAME because a reader tells this launch's record from an earlier one's by name, and
#: a pid is not an identity there: in a container Kit's pid is namespaced and repeats from one
#: launch to the next inside the same allocation.
BOOT_REFUSAL_PREFIX = "boot-refusal-"
BOOT_REFUSAL_SCHEMA = "omni.kit.mcp.boot-refusal/1"
#: What Kit is asked to exit with. Not 1: a reader can tell this exit from any other failure.
BOOT_REFUSAL_EXIT = 78


class ToolModuleLoadRefusal(RuntimeError):
    """Configured tool modules failed to load, so the doorway does not come up.

    ``failures`` is ``{module: full formatted traceback}`` — the traceback, not the message: the
    message of the failure this was written for was ``dictionary changed size during iteration``,
    which names neither the dictionary nor the line.
    """

    code = TOOL_MODULE_LOAD_REFUSED

    def __init__(self, failures: Dict[str, str], configured: List[str]):
        self.failures = dict(failures)
        self.configured = list(configured)
        blocks = "\n".join(f"--- {name} ---\n{frames.rstrip()}"
                            for name, frames in self.failures.items())
        #: The refusal without its code in front — what a caller that already prints the code
        #: (a standalone carrier lifts ``.code`` and ``.detail``) shows beside it.
        self.detail = (
            f"{len(self.failures)} of {len(self.configured)} configured tool "
            f"module(s) failed to load: {', '.join(self.failures)}. The doorway does NOT come up "
            f"without them: their tools are dialled by name, and a doorway missing a configured "
            f"namespace answers 'unknown tool' a whole run later instead of saying so here. "
            f"None of the configured modules is served, and Kit is asked to quit — in EVERY "
            f"launch, a hand-launched windowed Isaac included: a module registered with "
            f"install.py add-project that no longer loads ends that app at startup too, by "
            f"this name, rather than leaving it up without the tools it was told to serve. "
            f"Fix the module or remove it from the configured list.\n{blocks}")
        super().__init__(f"{self.code}: {self.detail}")


def _load_each(bridge, modules: Optional[List[str]],
               paths: Optional[List[str]]) -> Tuple[List[str], Dict[str, Any], Dict[str, str]]:
    """Attempt every module once: (the list attempted, results, {failed module: traceback})."""
    prepend_tool_paths(paths)
    if modules is None:
        modules = resolve_tool_modules()

    results: Dict[str, Any] = {}
    failures: Dict[str, str] = {}
    for mod_name in modules:
        try:
            results[mod_name] = _register_one(bridge, mod_name)
            _log("info", f"[omni.kit.mcp] autoload: {mod_name} -> {results[mod_name]}")
        except Exception as e:
            results[mod_name] = f"ERROR: {e}"
            failures[mod_name] = traceback.format_exc()
            _log("error", f"[omni.kit.mcp] autoload: failed to load {mod_name}: "
                          f"{type(e).__name__}: {e}\n{failures[mod_name]}")
    return list(modules), results, failures


def load_tool_modules(bridge, modules: Optional[List[str]] = None,
                      paths: Optional[List[str]] = None) -> Dict[str, Any]:
    """Load + register every named tool package, reporting each outcome.

    Returns {module: {"namespace", "tools"} | "ERROR: ..."}. This is the REPORTING form — a
    caller that asked for a list and reads the answer. Boot does not use it: a doorway starting
    up has no reader for the map, so it goes through ``load_configured_tool_modules``, which
    refuses instead.
    """
    return _load_each(bridge, modules, paths)[1]


def load_configured_tool_modules(bridge, modules: Optional[List[str]] = None,
                                 paths: Optional[List[str]] = None) -> Dict[str, Any]:
    """Boot's loader: every configured module loads, or ``ToolModuleLoadRefusal``.

    On a refusal the modules that DID load are unregistered too. A doorway is not going to be
    served at all, and a caller that catches the refusal must not be left holding a bridge with
    part of the configured surface on it — that is the shape this exists to end.
    """
    attempted, results, failures = _load_each(bridge, modules, paths)
    if not failures:
        return results
    for mod_name in attempted:
        if mod_name not in failures:
            bridge.unregister_owner(mod_name)
    raise ToolModuleLoadRefusal(failures, attempted)


def boot_refusal_dir() -> str:
    """Where the boot refusal record goes: the doorway's own log directory."""
    from .bridge import LOG_DIR_ENV, _runtime_dir
    return os.getenv(LOG_DIR_ENV) or _runtime_dir()


def _post_quit(code: int) -> None:
    """Ask Kit to quit with ``code``, by the quit nothing can cancel when this Kit has one.

    ``post_quit`` is CANCELLABLE: ``omni.kit.window.file`` cancels a shutdown that finds pending
    stage edits and re-posts it with no code, so the exit becomes 0 and the refusal's own code
    is lost. ``post_uncancellable_quit`` is the call that means what this means. A Kit without
    it gets ``post_quit`` — said by name in the log, because the exit code may then not be ours.
    """
    import omni.kit.app
    app = omni.kit.app.get_app()
    uncancellable = getattr(app, "post_uncancellable_quit", None)
    if callable(uncancellable):
        uncancellable(code)
        return
    _log("warn", f"[omni.kit.mcp] BOOT_REFUSAL_QUIT_CANCELLABLE: this Kit has no "
                 f"post_uncancellable_quit; asking with post_quit({code}), which another "
                 f"extension may cancel or re-post with a different exit code")
    app.post_quit(code)


def _write_boot_refusal(refusal: ToolModuleLoadRefusal) -> str:
    directory = boot_refusal_dir()
    path = os.path.join(
        directory, f"{BOOT_REFUSAL_PREFIX}{os.getpid()}-{time.time_ns()}.json")
    os.makedirs(directory, exist_ok=True)
    partial = path + ".partial"
    with open(partial, "w") as fh:
        json.dump({"schema": BOOT_REFUSAL_SCHEMA, "code": refusal.code, "pid": os.getpid(),
                   "at": time.time(), "configured": refusal.configured,
                   "failed": sorted(refusal.failures), "tracebacks": refusal.failures,
                   "exit_code": BOOT_REFUSAL_EXIT}, fh, indent=2, sort_keys=True)
    os.replace(partial, path)
    return path


def refuse_boot(refusal: ToolModuleLoadRefusal,
                quit_app: Callable[[int], None] = _post_quit) -> Optional[str]:
    """Make a boot refusal reach whoever is waiting on this doorway, then ask Kit to quit.

    Four places, because the waiters differ: the log (a human, and the cell's stderr); the
    bridge module (``get_mcp_bridge`` raises this refusal from now on, so a standalone carrier
    asking in-process reports THIS code and traceback rather than "bridge not initialized"); a
    record file (a composed cell's bringup polls for it, so the failure is read as a boot
    failure with its cause the moment it happens — not as a readiness deadline with none); and
    the process itself (a Kit left running with no doorway holds a GPU for nothing).

    The record is written BEFORE the quit is asked for, and the quit does not depend on it: a
    log directory that cannot be written is reported by name and Kit is asked to quit all the
    same — otherwise a full disk would turn this refusal back into a Kit that stays up with no
    socket and nothing saying why. Returns the record path, or None when it could not be
    written. The caller still raises the refusal; this does not swallow it.
    """
    from .bridge import _hold_boot_refusal
    _hold_boot_refusal(refusal)
    _log("error", f"[omni.kit.mcp] {refusal}")
    path: Optional[str] = None
    try:
        path = _write_boot_refusal(refusal)
    except OSError as error:
        _log("error", f"[omni.kit.mcp] BOOT_REFUSAL_RECORD_UNWRITTEN: the boot refusal could "
                      f"not be recorded under {boot_refusal_dir()!r} ({error!r}); a bringup "
                      f"polling for the record will not see it. The refusal above stands and "
                      f"Kit is still asked to quit with exit {BOOT_REFUSAL_EXIT}")
    else:
        _log("error", f"[omni.kit.mcp] boot refusal recorded at {path}; asking Kit to quit "
                      f"with exit {BOOT_REFUSAL_EXIT}")
    quit_app(BOOT_REFUSAL_EXIT)
    return path


def purge_pycache(directory: str) -> None:
    """Delete one directory's __pycache__ so the next import recompiles.

    The reasoning, once: .pyc validation is (mtime, size), so an edit within
    the same second that keeps the size constant — routine in an agent's
    edit->reload/push cycle — would otherwise serve stale bytecode. Every
    stale-bytecode guard (module reload here, file push in builtins.put_file)
    goes through this.
    """
    import shutil
    shutil.rmtree(os.path.join(directory, "__pycache__"), ignore_errors=True)


def _purge_bytecode(module: str) -> None:
    """Delete __pycache__ trees under a tool package so re-import recompiles."""
    import importlib.util
    try:
        spec = importlib.util.find_spec(module)
    except Exception:
        return
    if spec is None or not spec.origin or spec.origin in ("built-in", "frozen"):
        return
    if spec.submodule_search_locations:  # package: purge caches in its tree
        for root in spec.submodule_search_locations:
            for dirpath, _dirnames, _ in os.walk(root):
                purge_pycache(dirpath)
    else:  # single-file module
        purge_pycache(os.path.dirname(spec.origin))


def reload_tool_module(bridge, module: str) -> Dict[str, Any]:
    """Hot-reload one autoloaded tool package (backs the reload_tools builtin).

    Unregister (draining), purge the module tree from sys.modules except
    the ``_state`` submodule, re-import, re-register. Live handles parked in a
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
    # Exact-segment match, not endswith: a tool module legitimately named e.g.
    # ``read_state`` must NOT be exempted from the purge, or it survives reload
    # bound to its OLD sibling imports (2026-07-15: a stale read_state kept a
    # split-mode _common alive across an articulation-mode flip).
    purged = [name for name in list(sys.modules)
              if (name == module or name.startswith(prefix))
              and name.rsplit(".", 1)[-1] != _STATE_MODULE]
    for name in purged:
        del sys.modules[name]

    # Drop cached bytecode too: .pyc validation is (mtime, size), so an edit
    # within the same second that keeps the size constant — routine in an
    # agent's edit->reload cycle — would otherwise serve stale code.
    importlib.invalidate_caches()
    _purge_bytecode(module)

    info = _register_one(bridge, module)
    return {"module": module, "reloaded": purged, **info}
