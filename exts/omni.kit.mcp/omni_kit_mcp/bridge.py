"""omni.kit.mcp — generic MCP socket bridge for Omniverse Kit.

One bridge, one port, per Kit process. Providers are *owners*: each project
registers an owner with a unique namespace and publishes tools under canonical
``<namespace>.<tool>`` names (e.g. ``arm.play_scene``). Clients are raw-TCP
NDJSON connections (see protocol.py); many may be connected concurrently, and
each is isolated — one client's disconnect, timeout, or garbage input never
affects the bridge, other clients, or provider state.

Execution model: sockets run on worker threads, but USD/PhysX are not
thread-safe, so every tool handler is hopped onto Kit's main thread
(``omni.kit.async_engine.run_coroutine``). The scheduler is injectable
(``schedule_coroutine``) so the full server is testable under plain asyncio
outside Kit.

Teardown safety: dispatch resolves a tool under the registry lock into an
immutable snapshot and refcounts the owner in flight; ``unregister_owner``
removes tools from resolution immediately and defers final cleanup until
in-flight work drains — so hot-reloading one project can't tear state out from
under a running call or affect co-loaded projects.
"""

import asyncio
import collections
import contextvars
import hmac
import inspect
import json
import os
import socket
import struct
import sys
import threading
import time
import traceback
import uuid
import weakref
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

from . import knobs, protocol, schema
from .protocol import ToolError
from .sessions import SessionStore
from . import telemetry


_CURRENT_REQUEST = contextvars.ContextVar("omni_kit_mcp_current_request", default=None)


def request_caller_disconnected() -> bool:
    request = _CURRENT_REQUEST.get()
    return bool(request is not None and request.conn.closed)

try:
    import carb

    def _log_info(msg): carb.log_info(msg)
    def _log_warn(msg): carb.log_warn(msg)
    def _log_error(msg): carb.log_error(msg)
except ImportError:  # outside Kit (tests / lint) — stay pure-stdlib
    def _log_info(msg): print(msg)
    def _log_warn(msg): print(msg)
    def _log_error(msg): print(msg)


# Owner id the bridge registers its own verbs under. Its namespace is None, so
# built-in names stay bare (run_python, not x.run_python) — they are the
# transport's own verbs, like rosbridge's op codes, not project tools.
BUILTIN_OWNER_ID = "omni.kit.mcp"


@dataclass(frozen=True)
class ToolDefinition:
    """One tool as its author declares it: local name, MCP metadata, handler.

    ``handler`` may be sync or async and receives the request's ``params`` as
    keyword arguments. It returns its payload (any JSON-serializable value;
    ``None`` becomes ``{}``) or raises — ``ToolError`` to attach structured
    diagnostics, anything else for a generic error. Handlers never build the
    wire envelope. ``internal`` tools are served but hidden from discovery.
    """

    name: str
    description: str
    parameters: Dict[str, Any]
    handler: Callable[..., Any]
    internal: bool = False


@dataclass(frozen=True)
class OwnerRegistration:
    """One provider: stable identity + its public namespace."""

    owner_id: str
    namespace: Optional[str]  # None only for the bridge's own builtins
    display_name: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)  # e.g. {"module": ...}


@dataclass(frozen=True)
class RegisteredTool:
    """A tool as the registry serves it: canonical name + owning provider."""

    canonical_name: str
    local_name: str
    owner_id: str
    namespace: Optional[str]
    definition: ToolDefinition


class OwnerRegistrar:
    """Owner-bound registration facade — what a tool package's ``register()``
    entrypoint receives instead of the raw bridge.

    Scopes every registration to one owner: authors never see owner ids, cannot
    clobber other projects' tools, and cannot touch the socket lifecycle.
    """

    def __init__(self, bridge: "McpBridge", owner_id: str, namespace: Optional[str]):
        self._bridge = bridge
        self.owner_id = owner_id
        self.namespace = namespace

    def refuse_calls_with(self, fn: Callable[[str, Dict[str, Any]], Optional[str]]) -> None:
        """Install policy that may refuse a call. `fn(name, params)` returns a reason, or None.

        Owner-scoped only in WHO installed it: a refusal governs every call the bridge dispatches,
        not just this owner's tools, because a gate that covered one project's names would be
        walked around by asking for another's.
        """
        self._bridge.register_pre_dispatch_hook(fn)

    def add(self, tool: ToolDefinition) -> str:
        """Register one tool under this owner; returns its canonical name."""
        return self._bridge._register_tool(self.owner_id, tool)

    def tool(self, description: str, parameters: Optional[Dict[str, Any]] = None,
             name: Optional[str] = None, internal: bool = False):
        """Decorator sugar: ``@registrar.tool("desc", {...params})`` on a handler."""
        def decorate(fn):
            self.add(ToolDefinition(
                name=name or fn.__name__,
                description=description,
                parameters=parameters or {},
                handler=fn,
                internal=internal,
            ))
            return fn
        return decorate

    def window(self, title: str) -> str:
        """Declare an ``omni.ui.Window`` title this owner builds.

        A window built by one module instance survives ``reload_tools`` with
        closures over the dead module's state — the dispatch layer hot-swaps
        but the on-screen UI keeps driving old code. Declaring the title here
        makes reloads self-healing: the bridge destroys the owner's declared
        windows when the owner is unregistered (reload/teardown), and any
        stale same-titled window is destroyed at declaration time too. The
        window-spawning tool just rebuilds fresh on its next invocation.

        Off-Kit (no ``omni.ui``) this records the title and otherwise no-ops.
        """
        return self._bridge._register_window(self.owner_id, title)


@dataclass
class _ClientConnection:
    """One accepted socket: its thread, and a send lock so a main-thread
    response and a socket-thread error reply can't interleave mid-frame."""

    connection_id: str
    sock: socket.socket
    address: tuple
    thread: Optional[threading.Thread] = None
    send_lock: threading.Lock = field(default_factory=threading.Lock)
    closed: bool = False
    #: The one request this connection has sent and not yet been answered (see `_submit`).
    pending: Any = None
    #: Refusals owed to frames that arrived while `pending` was unanswered. They are sent right
    #: after its answer, never before: the protocol has no ids, so ORDER is the only correlation.
    deferred: List[Any] = field(default_factory=list)

    def transmit(self, obj: Any) -> None:
        """Write one frame. The caller holds `send_lock`."""
        self.sock.sendall(protocol.encode_frame(obj))

    def send(self, obj: Any) -> None:
        with self.send_lock:
            self.transmit(obj)

    def hang_up(self) -> None:
        """Close the socket; the read loop sees it and forgets the connection."""
        try:
            self.sock.close()
        except Exception:                                                     # noqa: BLE001
            pass


class _LocalCaller:
    """An in-process caller (the panel, a host extension) as admission sees a connection.

    It has no socket: its answer is handed to `on_reply`. One per call, so the one-request-per-
    connection rule holds by construction and a caller never has to track it.
    """

    def __init__(self, connection_id: str, on_reply: Callable[[Dict[str, Any]], None]):
        self.connection_id = connection_id
        self.closed = False
        self.pending = None
        self.deferred: List[Any] = []
        self.send_lock = threading.Lock()
        self._on_reply = on_reply

    def transmit(self, obj: Any) -> None:
        self._on_reply(obj)

    def send(self, obj: Any) -> None:
        with self.send_lock:
            self.transmit(obj)

    def hang_up(self) -> None:
        self.closed = True


@dataclass
class _Request:
    """One request as admission holds it: DATA until it is started, never a task before that."""

    seq: int
    conn: Any
    command: Dict[str, Any]
    name: Optional[str]
    exclusive: bool
    arrived: float              # time.monotonic()
    queue_s: float              # how long it may wait to START: min(what it stated, the cap)
    queue_stated: Optional[float]   # what the client stated; None = it stated nothing
    state: str = "queued"       # queued | started | refused | dropped | finished
    started: Optional[float] = None
    entered: bool = False       # its coroutine has begun — or it has been forbidden to begin
    replied: bool = False
    released: bool = False

    @property
    def deadline(self) -> float:
        return self.arrived + self.queue_s


@dataclass
class _Hold:
    """The one hold this bridge carries (see `HOLD_VERB`)."""

    token: str
    holder: str
    reason: str
    conn: Any
    placed_at: float                 # time.time(), for the record
    placed_mono: float               # time.monotonic(), for `held_s`
    refused: Dict[str, int] = field(default_factory=dict)

    def describe(self, now: float) -> Dict[str, Any]:
        """What anyone may read about the hold: never the token."""
        return {"held": True, "holder": self.holder, "reason": self.reason,
                "connection": getattr(self.conn, "connection_id", None),
                "placed_at": self.placed_at, "held_s": round(now - self.placed_mono, 3),
                "refused": sum(self.refused.values()), "refused_tools": dict(self.refused)}


#: Set to True on a handler FUNCTION that advances the simulator from inside its own call. The
#: bridge then gives that request the main loop to itself (`McpBridge._submit`).
EXCLUSIVE_ATTRIBUTE = "__omni_kit_mcp_exclusive__"

#: THE ADMISSION CONTRACT THIS BRIDGE SPEAKS. A tool package declares the contract it was written
#: against as a module-level `DOORWAY_ADMISSION = <int>`; a package may read this constant to learn
#: whether the bridge it is loaded into honours its exclusive marks at all. The two are halves of
#: one repair and are worthless apart: marks an older bridge ignores, or a bridge whose tools carry
#: no marks, both keep the defect while looking fixed. So each half REFUSES BY NAME without the
#: other (`DOORWAY_ADMISSION_UNDECLARED` here; `DOORWAY_ADMISSION_UNSUPPORTED` from the package).
ADMISSION_CONTRACT = 1
ADMISSION_DECLARATION = "DOORWAY_ADMISSION"
#: The same fact as a CAPABILITY a remote client reads off `status` (`doorway_admission`): this
#: doorway serialises exclusive tools and answers `DOORWAY_BUSY`. A client that depends on that —
#: one that must not run against a doorway where a second request is silently destroyed — checks
#: for this string and refuses on its absence. An older doorway's `status` carries no such key.
ADMISSION_CAPABILITY = "exclusive-v1"

#: THE QUEUE BUDGET IS THE CALLER'S, NEVER THE SERVER'S. A request states `queue_s` — the longest
#: it may wait to START — and one that states nothing starts at once or is refused at once. The
#: server does not invent a patience for a caller: an older client's own timeout is unknown here,
#: and a queued request whose caller has already given up is the loss this exists to end. What
#: the server does own is a CAP on what may be stated, and how many requests may wait at all.
DEFAULT_QUEUE_CAP_S = 900.0
DEFAULT_QUEUE_LIMIT = 64          # QUEUED requests, running ones excluded; the 65th is refused
QUEUE_CAP = knobs.Knob(env="OMNI_KIT_MCP_QUEUE_CAP_S", leaf="queueCapSeconds")
QUEUE_LIMIT = knobs.Knob(env="OMNI_KIT_MCP_QUEUE_LIMIT", leaf="queueLimit")

#: A send that cannot make progress for this long means the caller is not reading. The socket is
#: then closed: one silent client must not hold the thread that is answering it.
SEND_TIMEOUT_S = 10.0
#: Frames a connection may send while one of its requests is unanswered before it is closed.
MAX_DEFERRED = 8

#: THE HOLD: ONE CALLER HAS THE DOORWAY TO ITSELF, AND EVERY OTHER FRAME IS REFUSED BY NAME.
#:
#: Mechanism only, like admission and the deny-gate: who holds and why are the caller's. A
#: connection places a hold with `{"type": HOLD_VERB, "params": {"token", "holder", "reason"}}`;
#: from then on every frame whose top-level `HOLD_FIELD` is not that token is answered `HELD`
#: (it did NOT run), and the in-process entrances are refused the same way. The holder's own
#: frames — from ANY connection — carry the token beside `type`/`params`/`queue_s`, never inside
#: `params`, which a handler receives as its keyword arguments.
#:
#: A HOLD CANNOT STRAND. It belongs to the connection that placed it and is released when that
#: connection closes, whatever closed it: an explicit `RELEASE_VERB`, the holder exiting, the
#: holder being killed (the kernel closes its socket), the bridge stopping. There is no timer: a
#: hold that expired under a live holder would hand the doorway back mid-evaluation.
#:
#: WHAT IS EXEMPT: `status` alone — a read of this bridge's identity and of the hold itself, which
#: touches no tool and no stage. Every refusal is counted by tool name, and the release answers
#: with those counts: the holder's evidence that the doorway was closed to everyone else.
HOLD_VERB = "doorway.hold"
RELEASE_VERB = "doorway.release"
HOLD_FIELD = "hold"
HOLD_EXEMPT = ("status",)
#: A token is the caller's secret for the hold's lifetime; one shorter than this is refused.
HOLD_TOKEN_MIN_CHARS = 32
#: The capability a client reads off `status` before relying on a hold. Absent on an older doorway.
HOLD_CAPABILITY = "hold-v1"
HELD = "DOORWAY_HELD"
HOLD_REFUSED = "DOORWAY_HOLD_REFUSED"

#: The typed refusals admission gives. Each is the envelope's `refusal` key and leads its message.
BUSY = "DOORWAY_BUSY"
PIPELINED = "DOORWAY_PIPELINED"
UNDECLARED = "DOORWAY_ADMISSION_UNDECLARED"
UNMARKED_PUMP = "DOORWAY_UNMARKED_PUMP"
CANCELLED = "DOORWAY_REQUEST_CANCELLED"
LOST = "DOORWAY_REQUEST_LOST"
SCHEDULER_FAILED = "DOORWAY_SCHEDULER_FAILED"


def _declared_number(knob: "knobs.Knob", default: float) -> float:
    """A knob as a non-negative number. A value that is not one REFUSES the launch by name: a
    deadline silently replaced by its default is a configuration nobody can see."""
    raw = knob.read()
    if raw is None:
        return float(default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{knob.env}={raw!r} is not a number") from None
    if value < 0:
        raise ValueError(f"{knob.env}={raw!r} is negative")
    return value


class McpBridge:
    """Owner-scoped tool registry + multi-client NDJSON socket server."""

    def __init__(self, schedule_coroutine: Optional[Callable] = None):
        # Injectable main-thread scheduler; None -> Kit's run_coroutine, resolved
        # lazily so the module imports (and tests run) outside Kit.
        self._schedule_coroutine = schedule_coroutine

        self._lock = threading.RLock()
        self._owners: Dict[str, OwnerRegistration] = {}
        self._namespaces: Dict[str, str] = {}          # namespace -> owner_id
        self._tools: Dict[str, RegisteredTool] = {}    # canonical name -> tool
        self._owner_tools: Dict[str, Set[str]] = {}    # owner_id -> canonical names
        self._owner_windows: Dict[str, Set[str]] = {}  # owner_id -> omni.ui window titles
        self._in_flight: Dict[str, int] = {}           # owner_id -> live dispatch count
        # REQUEST ADMISSION (see `_submit`). A socket request is either started at once or kept
        # here until an EXCLUSIVE one — a handler that pumps the main loop from inside its own
        # task — has the loop to itself and has given it back.
        self._admit_lock = threading.RLock()
        self._queue: "collections.deque[_Request]" = collections.deque()   # FIFO by arrival
        self._started: Dict[int, _Request] = {}        # started or scheduled, not yet released
        self._exclusive: Optional[_Request] = None     # the one that has the loop to itself
        self._sequence = 0
        self._counts: Dict[str, int] = collections.Counter()
        self._queue_cap_s = _declared_number(QUEUE_CAP, DEFAULT_QUEUE_CAP_S)
        self._queue_limit = int(_declared_number(QUEUE_LIMIT, DEFAULT_QUEUE_LIMIT))
        # Verbs the RUNTIME WITNESS caught advancing the loop from inside an ordinary call
        # (`_witness_pump`): exclusive from then on, and named in `status`.
        self._unmarked_pumps: Dict[str, int] = {}
        # The deadline watch: a THREAD, because the main loop may be inside a pump for minutes
        # and a refusal that waits for it is the silence this exists to end.
        self._watch_wake = threading.Condition(self._admit_lock)
        self._watch_thread: Optional[threading.Thread] = None
        self._abandoned: "collections.deque[_Request]" = collections.deque()
        self._draining: Set[str] = set()               # owners awaiting final cleanup
        self._telemetry = telemetry.configured()
        # THE HOLD (see `HOLD_VERB`), under the admission lock; None when nobody holds the doorway.
        self._hold: Optional[_Hold] = None

        # Policy that may REFUSE a call, consulted before anything is resolved or run. The
        # mechanism is here because this is the single entrance every caller uses; WHAT to refuse
        # and WHY belong to the host project, exactly as the tool registry does.
        self._pre_dispatch: List[Callable[[str, Dict[str, Any]], Optional[str]]] = []

        # Persistent run_python sessions — the store owns the persist policy
        # and the staleness contract (see sessions.py).
        self.sessions = SessionStore()

        # Socket server state.
        self._socket: Optional[socket.socket] = None
        self._server_thread: Optional[threading.Thread] = None
        self._running = False
        self._connections: Dict[str, _ClientConnection] = {}
        self._port: Optional[int] = None
        self._bind_host: Optional[str] = None
        self._started_at: Optional[float] = None

        self._register_builtins()

    # ==================== Owner registry ====================

    def register_owner(self, owner_id: str, namespace: str, *,
                       display_name: Optional[str] = None,
                       metadata: Optional[Dict[str, Any]] = None) -> OwnerRegistrar:
        """Register a provider and return its owner-bound registrar.

        Rejects duplicate owner ids and duplicate namespaces — a namespace is a
        public API prefix and two projects claiming one is a config error worth
        failing loudly at the source.
        """
        if not namespace or "." in namespace:
            raise ValueError(f"invalid namespace {namespace!r}: non-empty, no dots")
        with self._lock:
            if owner_id in self._owners:
                raise ValueError(f"owner {owner_id!r} is already registered")
            if namespace in self._namespaces:
                raise ValueError(
                    f"namespace {namespace!r} is already owned by "
                    f"{self._namespaces[namespace]!r}")
            self._owners[owner_id] = OwnerRegistration(
                owner_id=owner_id, namespace=namespace,
                display_name=display_name, metadata=dict(metadata or {}))
            self._namespaces[namespace] = owner_id
            self._owner_tools[owner_id] = set()
            self._in_flight.setdefault(owner_id, 0)
        return OwnerRegistrar(self, owner_id, namespace)

    def _register_tool(self, owner_id: str, tool: ToolDefinition) -> str:
        """Registry write path (via OwnerRegistrar). Returns the canonical name."""
        with self._lock:
            owner = self._owners.get(owner_id)
            if owner is None or owner_id in self._draining:
                raise ValueError(f"owner {owner_id!r} is not registered")
            canonical = (tool.name if owner.namespace is None
                         else f"{owner.namespace}.{tool.name}")
            if canonical in self._tools:
                raise ValueError(f"tool {canonical!r} is already registered")
            self._tools[canonical] = RegisteredTool(
                canonical_name=canonical, local_name=tool.name,
                owner_id=owner_id, namespace=owner.namespace, definition=tool)
            self._owner_tools[owner_id].add(canonical)
            return canonical

    def _register_window(self, owner_id: str, title: str) -> str:
        """Record an owner's omni.ui window title (via OwnerRegistrar.window)
        and destroy any stale same-titled window left by a previous module
        instance. Destroyed again on unregister so reloads are self-healing."""
        with self._lock:
            owner = self._owners.get(owner_id)
            if owner is None or owner_id in self._draining:
                raise ValueError(f"owner {owner_id!r} is not registered")
            self._owner_windows.setdefault(owner_id, set()).add(title)
        _destroy_ui_window(title)
        return title

    def unregister_owner(self, owner_id: str) -> None:
        """Remove an owner. Tools leave resolution immediately; final cleanup
        defers until the owner's in-flight dispatches drain (see _dispatch).
        Declared windows are destroyed now — they belong to the module
        instance being retired, and letting them live would leave on-screen UI
        driving dead closures after a reload."""
        with self._lock:
            if owner_id not in self._owners:
                return
            for canonical in self._owner_tools.get(owner_id, set()):
                self._tools.pop(canonical, None)
            windows = self._owner_windows.pop(owner_id, set())
            self._draining.add(owner_id)
            if self._in_flight.get(owner_id, 0) == 0:
                self._finalize_owner_locked(owner_id)
        for title in windows:   # UI teardown outside the registry lock
            _destroy_ui_window(title)

    def _finalize_owner_locked(self, owner_id: str) -> None:
        """Complete a drained owner's removal. Caller holds the lock."""
        owner = self._owners.pop(owner_id, None)
        if owner and owner.namespace is not None:
            self._namespaces.pop(owner.namespace, None)
        self._owner_tools.pop(owner_id, None)
        self._owner_windows.pop(owner_id, None)
        self._in_flight.pop(owner_id, None)
        self._draining.discard(owner_id)

    def get_registered_tools(self, include_internal: bool = False) -> Dict[str, Dict[str, Any]]:
        """Discovery view: canonical name -> metadata. Canonical names only —
        the advertised surface never changes with co-load state. ``fields``
        carries the parameters pre-interpreted as field specs (schema.py) so
        renderers never read raw parameter schemas."""
        with self._lock:
            return {
                t.canonical_name: {
                    "description": t.definition.description,
                    "parameters": t.definition.parameters,
                    "fields": schema.field_specs(t.definition.parameters),
                    "namespace": t.namespace,
                    "owner_id": t.owner_id,
                }
                for t in self._tools.values()
                if include_internal or not t.definition.internal
            }

    def get_owners(self) -> Dict[str, Dict[str, Any]]:
        """Owner registry view (for panels / diagnostics)."""
        with self._lock:
            return {
                o.owner_id: {
                    "namespace": o.namespace,
                    "display_name": o.display_name,
                    "metadata": dict(o.metadata),
                    "tools": sorted(self._owner_tools.get(o.owner_id, set())),
                    "windows": sorted(self._owner_windows.get(o.owner_id, set())),
                    "draining": o.owner_id in self._draining,
                }
                for o in self._owners.values()
            }

    def find_owner_by_module(self, module: str) -> Optional[str]:
        """Owner id whose metadata records the given autoloaded module, if any."""
        with self._lock:
            for o in self._owners.values():
                if o.metadata.get("module") == module and o.owner_id not in self._draining:
                    return o.owner_id
        return None

    def clear_sessions(self) -> None:
        """Drop all persistent run_python sessions. The bridge extension calls
        this on every stage-open event — a new stage kills the prims/handles
        saved session vars point at (see sessions.py)."""
        self.sessions.clear()

    def _register_builtins(self) -> None:
        """The verbs the transport itself owns, bare-named (no namespace)."""
        from . import builtins as bi  # deferred: builtins imports this module's types
        with self._lock:
            self._owners[BUILTIN_OWNER_ID] = OwnerRegistration(
                owner_id=BUILTIN_OWNER_ID, namespace=None,
                display_name="omni.kit.mcp builtins")
            self._owner_tools[BUILTIN_OWNER_ID] = set()
            self._in_flight[BUILTIN_OWNER_ID] = 0
        for tool in bi.builtin_tools(self):
            self._register_tool(BUILTIN_OWNER_ID, tool)

    # ==================== Dispatch ====================

    def register_pre_dispatch_hook(
            self, fn: Callable[[str, Dict[str, Any]], Optional[str]]) -> None:
        """Install policy that may refuse a call. `fn(name, params)` returns a reason, or None.

        MECHANISM ONLY, the same rule the tool registry follows: this knows how to ask, and
        nothing about which calls should be refused or why. Those live in the host project, which
        installs its hook when it registers its tools.
        """
        with self._lock:
            self._pre_dispatch.append(fn)

    def registrar_for(self, owner_id: str) -> OwnerRegistrar:
        """The owner-bound facade for an owner already registered.

        For the second thing a tool package may be asked to do — install its policy — after its
        tools are in. It gets the same facade its entrypoint would have, rather than the raw
        bridge, so the rule that authors never touch the socket lifecycle still holds.
        """
        with self._lock:
            owner = self._owners.get(owner_id)
            if owner is None:
                raise KeyError(f"no owner {owner_id!r} is registered")
            namespace = self._namespaces_of(owner_id)
        return OwnerRegistrar(self, owner_id, namespace)

    def _namespaces_of(self, owner_id: str) -> Optional[str]:
        for namespace, owner in self._namespaces.items():
            if owner == owner_id:
                return namespace
        return None

    def clear_pre_dispatch_hooks(self) -> int:
        """Remove every installed policy; returns how many there were.

        For a host project whose policy is bound to something that has ended — a run, a stage, a
        session. A policy that outlived what it was about would go on refusing against a state
        nobody is maintaining any more, which is a different wrong answer, not a safer one.
        """
        with self._lock:
            count = len(self._pre_dispatch)
            self._pre_dispatch.clear()
        return count

    def pre_dispatch_hooks(self) -> int:
        """How many are installed. Readable so a caller can assert it armed exactly one."""
        with self._lock:
            return len(self._pre_dispatch)

    def _refused(self, name: str, params: Dict[str, Any]) -> Optional[str]:
        """The first refusal any hook gives, or None.

        A HOOK THAT RAISES REFUSES. It was installed to decide whether this call may proceed, and
        one that broke did not decide — treating its failure as consent would turn the one moment
        policy was unavailable into the one moment everything is permitted. This is the same rule
        the gate itself applies to a state it cannot read.
        """
        with self._lock:
            hooks = list(self._pre_dispatch)
        for hook in hooks:
            try:
                reason = hook(name, params)
            except Exception as error:                                        # noqa: BLE001
                _log_error(f"[omni.kit.mcp] pre-dispatch hook failed on {name}: {error}")
                return (f"a policy hook failed while deciding whether {name!r} may run "
                        f"({type(error).__name__}: {error}), so this call is refused rather than "
                        f"allowed. A hook that could not decide has not decided")
            if reason:
                return reason
        return None

    async def dispatch(self, name: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """One call from a caller that is ALREADY a coroutine on the main loop.

        ADMITTED AT ONCE OR REFUSED AT ONCE, never queued: whoever awaits this is somebody's task
        already, so the bridge cannot hold it as data, and a task parked across a pump is the
        thing admission exists to prevent. It starts only when it would have been the head of an
        empty queue; otherwise it is answered `DOORWAY_BUSY` naming what is running. A caller
        that wants to WAIT uses `submit_local`, which queues like a socket request.

        This is also what makes a body that calls back into the doorway from inside its own
        exclusive call a typed refusal rather than a wait on itself.
        """
        params = params or {}
        refusal = self._admission_refusal(name)
        if refusal is not None:
            return refusal
        with self._admit_lock:
            # AN IN-PROCESS CALLER CARRIES NO FRAME, SO IT CANNOT CARRY A HOLD'S TOKEN: refused while
            # any hold stands, decided in the critical section that would start it.
            held = self._held_refusal_locked(name, None)
            if held is not None:
                return held
            request = self._new_request_locked(_LocalCaller("in-process", lambda _obj: None),
                                               {"type": name, "params": params})
            if self._queue or not self._may_start_locked(request):
                self._counts["refused_busy"] += 1
                return self._busy_locked(request, "in_process")
            self._claim_start_locked(request)
        try:
            return await self._dispatch_admitted(name, params)
        finally:
            self._start(self._release(request))

    async def _dispatch_admitted(self, name: str,
                                 params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Dispatch unchanged when no telemetry sink is declared; otherwise census the call."""
        if self._telemetry is None:
            return await self._dispatch_unobserved(name, params)
        started = time.time()
        response = await self._dispatch_unobserved(name, params)
        if not self._telemetry.record(name, params or {}, started, response):
            _log_warn(f"[omni.kit.mcp] authoring telemetry dropped a {name!r} event")
        return response

    async def _dispatch_unobserved(self, name: str,
                                   params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Resolve + execute one tool call; returns the response envelope.

        The single entrance to tool logic: socket requests, the panel's buttons,
        and any in-process caller all come through here, so a click and an agent
        call are the same code path by construction.
        """
        params = params or {}
        notice = None

        # POLICY FIRST, before the tool is even resolved. Before, for two reasons: a refused call
        # must not refcount an owner it never entered, and a gate is about a NAME rather than
        # about whether that name happens to be registered right now — a policy that could be
        # evaded by asking for a tool the registry has not loaded yet would be no policy.
        #
        # This is the only place worth putting it. The gateway front-ends are TCP clients of this
        # socket, so a gate installed in one of them governs that agent alone and is walked around
        # by anything else that dials the port. Here, a socket request, a panel button and an
        # in-process caller are the same code path by construction.
        refusal = self._refused(name, params)
        if refusal is not None:
            return protocol.error(refusal, error_type="Refused", tool=name, refused=True)

        # Snapshot-resolve under lock; refcount the owner in flight.
        with self._lock:
            tool = self._tools.get(name)
            if tool is None:
                return protocol.error(
                    f"unknown tool: {name!r}",
                    known_tools=sorted(k for k, t in self._tools.items()
                                       if not t.definition.internal))
            owner_id = tool.owner_id
            handler = tool.definition.handler
            self._in_flight[owner_id] = self._in_flight.get(owner_id, 0) + 1

        try:
            if inspect.iscoroutinefunction(handler):
                result = await handler(**params)
            else:
                witness = self._arm_pump_witness(name, handler)
                try:
                    result = handler(**params)
                finally:
                    notice = self._read_pump_witness(name, witness)
                if inspect.isawaitable(result):
                    result = await result
            answer = protocol.success(result)
            if notice is not None:
                answer["doorway_notice"] = notice      # beside the result, never inside it
            return answer
        except ToolError as e:
            # RECORDED BEFORE IT IS SERIALISED. A ToolError carries the handler's own traceback in
            # `details`, and `run_python` wraps every exception this way. Logged here, the frames
            # survive in the Kit log even when a caller surfaces only the message; otherwise they
            # would exist only inside a reply envelope.
            _record_tool_failure(name, e, params, e.details.get("traceback"))
            return protocol.error(
                str(e), **{"error_type": type(e).__name__, **e.details})
        except TypeError as e:
            # Most common authoring/caller mismatch: bad kwargs. Name the tool's
            # declared parameters in the reply so the caller can self-correct.
            _record_tool_failure(name, e, params, traceback.format_exc())
            return protocol.error(
                f"{name}: {e}",
                error_type=type(e).__name__,
                expected_parameters=list(tool.definition.parameters.keys()),
                traceback=traceback.format_exc())
        except Exception as e:
            _record_tool_failure(name, e, params, traceback.format_exc())
            return protocol.error(str(e), error_type=type(e).__name__,
                                  traceback=traceback.format_exc())
        finally:
            with self._lock:
                self._in_flight[owner_id] = max(0, self._in_flight.get(owner_id, 1) - 1)
                if owner_id in self._draining and self._in_flight[owner_id] == 0:
                    self._finalize_owner_locked(owner_id)

    # THE RUNTIME WITNESS. Which handlers pump is DECLARED by their packages and checked there by a
    # syntax-tree test, and a static reading of a dynamic language has a residue. So the bridge
    # also watches, for every synchronous handler that is not exclusive, unconditionally: a plain
    # loop callback is queued before the handler is called and cancelled when it returns. A
    # synchronous handler never yields, so that callback can only have RUN if the handler
    # re-entered the loop — which is the pump. A verb caught this way is named, counted, reported
    # by `status`, and exclusive from then on: an unmarked pump announces itself the first time
    # instead of destroying other callers' requests for as long as nobody notices.

    def _arm_pump_witness(self, name: str, handler) -> Optional[Dict[str, Any]]:
        if getattr(handler, EXCLUSIVE_ATTRIBUTE, False) or name in self._unmarked_pumps:
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None                      # not called on a loop: there is nothing to re-enter
        witness: Dict[str, Any] = {"live": True, "fired": False}

        def fired():
            if witness["live"]:
                witness["fired"] = True

        witness["handle"] = loop.call_soon(fired)
        return witness

    def _read_pump_witness(self, name: str, witness: Optional[Dict[str, Any]]) -> Optional[str]:
        """The notice to carry on this call's reply when the witness fired, else None."""
        if witness is None:
            return None
        witness["live"] = False
        witness["handle"].cancel()
        if not witness["fired"]:
            return None
        with self._admit_lock:
            self._unmarked_pumps[name] = self._unmarked_pumps.get(name, 0) + 1
            self._counts["unmarked_pump"] += 1
            # WHAT THE PUMP STRANDED IS STARTED AGAIN. A request scheduled beside this handler
            # and not yet entered had its task refused by the nested loop; that task will never
            # be stepped again, and — because it never entered — its handler has not run. So a
            # fresh coroutine for the same request loses nothing and repeats nothing. If the
            # first one was in fact still alive, whichever enters second finds the request
            # already claimed and returns (`_execute_and_reply`).
            stranded = [r for r in sorted(self._started.values(), key=lambda r: r.seq)
                        if not r.entered]
            self._counts["rescued"] += len(stranded)
        notice = (
            f"{UNMARKED_PUMP}: {name!r} advanced the main loop from inside its own call and is "
            f"not marked exclusive ({EXCLUSIVE_ATTRIBUTE}). {len(stranded)} request(s) started "
            f"beside it had their tasks destroyed and were started again"
            f"{' (' + ', '.join(repr(r.name) for r in stranded) + ')' if stranded else ''}. "
            f"It is treated as exclusive from now on in this process; the mark belongs in its "
            f"tool package, and its absence there is a miss of that package's own derivation "
            f"test")
        _log_error(f"[omni.kit.mcp] {notice}")
        self._start(stranded)
        return notice

    def _schedule(self, coro):
        """Hop a coroutine onto the main loop (Kit's run_coroutine by default).

        Returns whatever the scheduler returns — a task or a future — so the caller can observe
        how the scheduled work ends. A scheduler that raises is the caller's to roll back."""
        if self._schedule_coroutine is None:
            from omni.kit.async_engine import run_coroutine
            self._schedule_coroutine = run_coroutine
        return self._schedule_coroutine(coro)

    # ==================== Socket server ====================

    def start(self, port: int, host: str = "localhost") -> None:
        """Bind the socket and start accepting clients. Idempotent.

        ``port=0`` binds an OS-assigned ephemeral port; the actual port is
        learned from the socket and advertised via the portfile (discovery
        makes it findable), so concurrent processes never need coordinated
        port numbers."""
        with self._lock:
            if self._running:
                _log_info(f"[omni.kit.mcp] server already running on port {self._port}")
                return
            self._running = True
        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._socket.bind((host, port))
            port = self._socket.getsockname()[1]   # learn the real port (0 -> ephemeral)
            with self._lock:
                self._port = port
                self._bind_host = host
                self._started_at = time.time()
            self._socket.listen(socket.SOMAXCONN)
            self._server_thread = threading.Thread(
                target=self._accept_loop, name="omni.kit.mcp-accept", daemon=True)
            self._server_thread.start()
            _write_portfile(host, port)
            _log_info(f"[omni.kit.mcp] serving on {host}:{port}")
        except Exception as e:
            _log_error(f"[omni.kit.mcp] failed to start server: {e}")
            self.stop()
            raise

    def stop(self) -> None:
        """Stop the server, close all client connections, join threads."""
        with self._lock:
            self._running = False
            connections = list(self._connections.values())
        if self._socket:
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None
        # Close client sockets to unblock their recv()s, then join.
        for conn in connections:
            try:
                conn.sock.close()
            except Exception:
                pass
        if self._server_thread:
            if self._server_thread.is_alive():
                self._server_thread.join(timeout=2.0)
            self._server_thread = None
        for conn in connections:
            if conn.thread and conn.thread.is_alive():
                conn.thread.join(timeout=2.0)
        with self._lock:
            self._connections.clear()
        with self._admit_lock:
            if self._hold is not None:
                _log_warn(f"[omni.kit.mcp] hold by {self._hold.holder!r} released: the server "
                          f"stopped")
            self._hold = None
        if self._port:
            _remove_portfile(self._port)
        _log_info("[omni.kit.mcp] server stopped")

    def _accept_loop(self) -> None:
        self._socket.settimeout(1.0)
        while self._running:
            try:
                sock, address = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                if not self._running:
                    break
                continue
            except Exception as e:
                if self._running:
                    _log_error(f"[omni.kit.mcp] accept error: {e}")
                continue
            _bound_sends(sock)
            conn = _ClientConnection(
                connection_id=uuid.uuid4().hex[:8], sock=sock, address=address)
            conn.thread = threading.Thread(
                target=self._serve_client, args=(conn,),
                name=f"omni.kit.mcp-client-{conn.connection_id}", daemon=True)
            with self._lock:
                self._connections[conn.connection_id] = conn
            _log_info(f"[omni.kit.mcp] client {conn.connection_id} connected from {address}")
            conn.thread.start()

    def _serve_client(self, conn: _ClientConnection) -> None:
        """One client's read loop. Every failure path is scoped to this
        connection only — no shared state is touched on the way out."""
        decoder = protocol.FrameDecoder()
        try:
            while self._running:
                try:
                    data = conn.sock.recv(65536)
                except (OSError, socket.timeout):
                    break
                if not data:
                    break
                for command, decode_err in decoder.feed(data):
                    if decode_err is not None:
                        self._refuse_frame(conn, None, protocol.error(decode_err))
                        continue
                    if not isinstance(command, dict) or "type" not in command:
                        self._refuse_frame(conn, command, protocol.error(
                            'malformed request: expected {"type": ..., "params": {...}}'))
                        continue
                    self._submit(conn, command)
        finally:
            with self._lock:
                self._connections.pop(conn.connection_id, None)
            # A CALLER THAT HAS GONE IS OWED NOTHING, AND MUST NOT BE SERVED: what it left waiting
            # is removed now, so a side-effecting verb never starts for a caller that cannot hear
            # its answer (a recorder that timed out must not start the NEXT episode's recording).
            self._start(self._forget_connection(conn))
            # AND A HOLD DIES WITH ITS CONNECTION: whatever closed it, the doorway is given back.
            self._release_hold_of(conn, "its connection closed")
            try:
                conn.sock.close()
            except Exception:
                pass
            _log_info(f"[omni.kit.mcp] client {conn.connection_id} disconnected")

    # ---------------------------------------------------------------- request admission
    #
    # WHY A REQUEST MAY HAVE TO WAIT. Kit steps its asyncio loop from an observer on the app's
    # update event (`omni.kit.async_engine`: "runs asyncio EventLoop every update for one
    # iteration"). A handler that advances the simulator from inside its own call — it calls
    # `app.update()` under a doorway task — re-fires that observer and re-enters the loop while a
    # task step is still on the stack. asyncio refuses to enter a second TASK then, the refused
    # step is never retried, and the request is LOST: its caller hears nothing until its own
    # timeout. With two clients on one simulator Kit reports "Cannot enter into task …
    # _execute_and_reply … while another task … _execute_and_reply … is being executed", then
    # "Task was destroyed but it is pending", and the second client hears nothing until its own
    # timeout. One client at a time never shows it.
    #
    # THE RULE. A request is DATA until it is started, and nothing is started while an exclusive
    # one runs. A handler DECLARES that it pumps (`EXCLUSIVE_ATTRIBUTE` on the function).
    #
    #   ONE QUEUE, BY ARRIVAL, AND ONLY ITS HEAD IS EVER CONSIDERED. An ordinary head starts when
    #   no exclusive request is running; an exclusive head starts when nothing at all is. Nothing
    #   behind the head is looked at, so neither class can be jumped by the other: an exclusive
    #   request waits only for what arrived before it, and what arrived after it waits behind it.
    #
    #   NO SILENT WAIT. A request states its QUEUE BUDGET (`queue_s`): the longest it may wait
    #   to START. One that states nothing starts at once or is refused at once. Past its budget a
    #   waiting request is answered `DOORWAY_BUSY`, naming what is running ahead and for how
    #   long, by a THREAD: the main loop may be inside the very pump that is in the way. The
    #   budget bounds the WAIT, not the verb: a verb that starts in time and then outruns its
    #   caller is that caller's reserve to state, and nothing here interrupts a running verb.
    #
    #   A CALLER THAT HAS GONE IS NEVER SERVED. A closed connection's waiting request is removed,
    #   and `closed` is read again immediately before a request starts.
    #
    #   ONE UNANSWERED REQUEST PER CONNECTION. The protocol has no request ids, so a second frame
    #   on a connection still owed an answer is refused `DOORWAY_PIPELINED` and never queued.
    #
    #   EXACTLY ONE TERMINAL FRAME, AND ADMISSION CANNOT WEDGE. A request's `state` moves away from
    #   `queued` once, under the admission lock, to whichever of start and refusal takes the lock
    #   first; its reply and its release are each claimed once the same way. How scheduled work
    #   ENDS is observed three ways, because none of them covers the others: the coroutine's own
    #   `finally` (it ran), a done-callback (cancelled before it ran), and a finalizer on the
    #   coroutine object (destroyed before it ran, where no callback fires at all).
    #
    # Every frame, from any thread, leaves through `_ClientConnection.send` — a blocking socket
    # under that connection's own lock. The asyncio loop never touches a socket here, so a
    # refusal sent from the watch thread cannot interleave with a reply sent from the main one.

    def _is_exclusive(self, command: Dict[str, Any]) -> bool:
        name = command.get("type")
        if not isinstance(name, str):
            return False
        with self._lock:
            tool = self._tools.get(name)
        if tool is None:
            return False
        return bool(getattr(tool.definition.handler, EXCLUSIVE_ATTRIBUTE, False)
                    or name in self._unmarked_pumps)

    def _admission_refusal(self, name: Any) -> Optional[Dict[str, Any]]:
        """`DOORWAY_ADMISSION_UNDECLARED` for a tool whose PACKAGE declares no admission contract.

        Only a package loaded as a module is asked: it is one half of a footprint, and the half
        that carries the exclusive marks. A module from before the contract has no marks, so
        serving it would keep the defect while this bridge looked like the repair. An owner
        registered programmatically is nobody's other half and is served as it always was.
        """
        if not isinstance(name, str):
            return None
        with self._lock:
            tool = self._tools.get(name)
            owner = self._owners.get(tool.owner_id) if tool is not None else None
        if owner is None or owner.metadata.get("module") is None:
            return None
        declared = owner.metadata.get("admission")
        if (isinstance(declared, int) and not isinstance(declared, bool)
                and 1 <= declared <= ADMISSION_CONTRACT):
            return None
        module = owner.metadata["module"]
        self._counts["refused_undeclared"] += 1
        return protocol.error(
            f"{UNDECLARED}: {name!r} is served by tool module {module!r}, which declares "
            f"{ADMISSION_DECLARATION}={declared!r}; this doorway speaks admission contract "
            f"{ADMISSION_CONTRACT} and serves only a module written against it, because a module "
            f"without the contract carries no exclusive marks and its frame-pumping verbs would "
            f"destroy other callers' requests. The doorway and its tool modules reach a box as "
            f"one footprint: this box holds the doorway's half and an older half of {module!r}",
            error_type="DoorwayAdmissionUndeclared", refused=True, refusal=UNDECLARED,
            tool=name, module=module, declared=declared, contract=ADMISSION_CONTRACT)

    def _new_request_locked(self, conn, command: Dict[str, Any]) -> _Request:
        self._sequence += 1
        stated = command.get("queue_s")
        return _Request(seq=self._sequence, conn=conn, command=command,
                        name=command.get("type"), exclusive=self._is_exclusive(command),
                        arrived=time.monotonic(),
                        queue_s=0.0 if stated is None else min(float(stated), self._queue_cap_s),
                        queue_stated=None if stated is None else float(stated))

    def _may_start_locked(self, request: _Request) -> bool:
        return self._exclusive is None and not (request.exclusive and self._started)

    def _claim_start_locked(self, request: _Request) -> None:
        request.state = "started"
        request.started = time.monotonic()
        self._started[request.seq] = request
        if request.exclusive:
            self._exclusive = request
        self._counts["started"] += 1

    def _submit(self, conn, command: Dict[str, Any]) -> None:
        """One decoded request: refuse it by name, start it now, or keep it until it may start."""
        name = command.get("type")
        stated = command.get("queue_s")
        if not isinstance(name, str):
            self._refuse_frame(conn, command, protocol.error(
                f"malformed request: \"type\" must be a tool name, not {type(name).__name__}"))
            return
        if stated is not None and (isinstance(stated, bool)
                                   or not isinstance(stated, (int, float)) or stated < 0):
            self._refuse_frame(conn, command, protocol.error(
                f"malformed request: \"queue_s\" must be a non-negative number of seconds "
                f"{name!r} may wait to start, not {stated!r}", tool=name))
            return
        if name in (HOLD_VERB, RELEASE_VERB):
            self._hold_frame(conn, command)
            return
        refusal, queued, starting = None, False, []
        with self._admit_lock:
            owed = conn.pending
            held = self._held_refusal_locked(name, command) if owed is None else None
            if held is not None:
                refusal = held
            elif owed is None:
                undeclared = self._admission_refusal(name)
                if undeclared is not None:
                    refusal = undeclared
                else:
                    request = self._new_request_locked(conn, command)
                    conn.pending = request
                    queued = True
                    self._queue.append(request)
                    self._counts["arrived"] += 1
                    starting = self._admit_locked()
                    if request.state == "queued":
                        waiting = sum(1 for r in self._queue if r is not request)
                        reason = ("no_queue_budget" if request.queue_s <= 0
                                  else "queue_full" if waiting >= self._queue_limit else None)
                        if reason is not None:
                            self._queue.remove(request)
                            request.state = "refused"
                            self._counts["refused_busy"] += 1
                            refusal = self._busy_locked(request, reason)
                        else:
                            ahead = self._exclusive or next(iter(self._started.values()), None)
                            _log_info(
                                f"[omni.kit.mcp] admission: #{request.seq} {name!r} "
                                f"({'exclusive' if request.exclusive else 'ordinary'}) waits at "
                                f"position {len(self._queue)} behind "
                                f"{ahead.name if ahead else 'the queue'!r}; may wait "
                                f"{request.queue_s:.1f} s")
                            self._ensure_watch_locked()
                            self._watch_wake.notify_all()
        if owed is not None:
            self._counts["refused_pipelined"] += 1
            self._refuse_frame(conn, command, protocol.error(
                f"{PIPELINED}: {name!r} arrived on connection {conn.connection_id} while that "
                f"connection's {owed.name!r} was still unanswered. The protocol carries no "
                f"request ids, so a connection has one request in flight; this frame was NOT "
                f"queued and did not run. Wait for the answer, or use a second connection",
                error_type="DoorwayPipelined", refused=True, refusal=PIPELINED, tool=name,
                unanswered=owed.name))
            return
        if refusal is not None:
            _log_warn(f"[omni.kit.mcp] {refusal['message']}")
            if queued:
                self._reply(request, refusal)      # it holds the connection's one slot: clear it
            else:
                self._refuse_frame(conn, command, refusal)
        self._start(starting)

    # ---------------------------------------------------------------- the hold (`HOLD_VERB`)

    def _held_refusal(self, name: str, command: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """`_held_refusal_locked` under the admission lock, for a caller that holds no lock."""
        with self._admit_lock:
            return self._held_refusal_locked(name, command)

    @staticmethod
    def _carries(command: Optional[Dict[str, Any]], token: str) -> bool:
        presented = command.get(HOLD_FIELD) if command is not None else None
        return isinstance(presented, str) and hmac.compare_digest(presented, token)

    def _held_refusal_locked(self, name: str,
                             command: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """`DOORWAY_HELD` for a frame (or an in-process call, `command=None`) that does not carry
        the standing hold's token, counted by tool name; None when it may proceed.

        DECIDED UNDER THE ADMISSION LOCK, in the same critical section that queues or starts the
        request: a frame that passed a check made outside it could be admitted after a
        hold was placed and drained in between, and start under the hold.

        AN IN-PROCESS CALLER HAS NO TOKEN ROUTE and is refused under every hold. The only
        in-process dispatcher is the panel; a Kit-side service that must dispatch through the
        bridge while a hold stands would be refused like any other in-process code — give it a
        token route before relying on it.
        Platform services in the control container carry the token from a container-private file
        (`kit_mcp.client.HOLD_TOKEN_FILE_ENV`)."""
        hold = self._hold
        if hold is None or name in HOLD_EXEMPT or self._carries(command, hold.token):
            return None
        hold.refused[name] = hold.refused.get(name, 0) + 1
        self._counts["refused_held"] += 1
        described = hold.describe(time.monotonic())
        _log_warn(f"[omni.kit.mcp] {HELD}: {name!r} refused — the doorway is held by "
                  f"{hold.holder!r} ({hold.reason})")
        return protocol.error(
            f"{HELD}: the doorway is held by {hold.holder} ({hold.reason}) for "
            f"{described['held_s']:.0f} s so far; {name!r} was refused and did NOT run. It is "
            f"released when that work ends; ask again then",
            error_type="DoorwayHeld", refused=True, refusal=HELD, tool=name,
            holder=hold.holder, reason=hold.reason, held_s=described["held_s"])

    def _hold_frame(self, conn, command: Dict[str, Any]) -> None:
        """Place or release THE hold; answered in order like every frame that is not a request."""
        name = command["type"]
        params = command.get("params") if isinstance(command.get("params"), dict) else {}
        token, holder, reason = params.get("token"), params.get("holder"), params.get("reason")
        drained: List[_Request] = []
        starting: List[_Request] = []
        placed: Optional[_Hold] = None
        with self._admit_lock:
            now = time.monotonic()
            standing = self._hold
            if name == HOLD_VERB:
                why = None
                if not isinstance(conn, _ClientConnection):
                    why = ("a hold belongs to a socket connection and is released when it closes; "
                           "an in-process caller has none")
                elif not isinstance(token, str) or len(token) < HOLD_TOKEN_MIN_CHARS:
                    why = f"the token must be a string of at least {HOLD_TOKEN_MIN_CHARS} characters"
                elif not (isinstance(holder, str) and holder.strip()
                          and isinstance(reason, str) and reason.strip()):
                    why = "a hold names its holder and its reason"
                elif standing is not None:
                    why = (f"the doorway is already held by {standing.holder} "
                           f"({standing.reason}) for {now - standing.placed_mono:.0f} s")
                if why is None:
                    placed = self._hold = _Hold(
                        token=token, holder=holder.strip(), reason=reason.strip(), conn=conn,
                        placed_at=time.time(), placed_mono=now)
                    # WHAT WAS WAITING WITHOUT THE TOKEN WILL NEVER START: refused now, by name,
                    # rather than left to start the moment the hold is released.
                    drained = [r for r in self._queue if not self._carries(r.command, token)]
                    for request in drained:
                        self._queue.remove(request)
                        request.state = "refused"
                        self._hold.refused[request.name] = self._hold.refused.get(request.name, 0) + 1
                        self._counts["refused_held"] += 1
                    starting = self._admit_locked()
                    running = [{"tool": r.name, "exclusive": r.exclusive,
                                "running_s": round(now - (r.started or now), 3)}
                               for r in sorted(self._started.values(), key=lambda r: r.seq)]
                    answer = protocol.success({
                        **self._hold.describe(now), "capability": HOLD_CAPABILITY,
                        # STARTED BEFORE THE HOLD, so it runs to its end; the holder waits on
                        # `status` until this list is empty rather than racing it.
                        "running_at_placement": running,
                        "drained": [r.name for r in drained]})
                    self._counts["holds"] += 1
                else:
                    answer = protocol.error(f"{HOLD_REFUSED}: {why}", error_type="DoorwayHoldRefused",
                                            refused=True, refusal=HOLD_REFUSED, tool=name)
            else:
                if standing is None or not isinstance(token, str) \
                        or not hmac.compare_digest(token, standing.token):
                    answer = protocol.error(
                        f"{HOLD_REFUSED}: no hold with this token stands", refused=True,
                        error_type="DoorwayHoldRefused", refusal=HOLD_REFUSED, tool=name)
                else:
                    answer = protocol.success({**standing.describe(now), "held": False,
                                               "released_by": "release"})
                    self._hold = None
        if name == HOLD_VERB and answer.get("status") == "success":
            _log_warn(f"[omni.kit.mcp] HOLD placed by {holder!r} ({reason}); "
                      f"{len(drained)} waiting request(s) refused")
        elif answer.get("status") == "success":
            _log_warn(f"[omni.kit.mcp] HOLD released by {standing.holder!r}: "
                      f"{answer['result']['refused']} request(s) refused while held")
        for request in drained:
            self._reply(request, self._held_envelope(request.name, placed))
        self._refuse_frame(conn, command, answer)
        self._start(starting)

    @staticmethod
    def _held_envelope(name: Optional[str], hold: "_Hold") -> Dict[str, Any]:
        holder, reason = hold.holder, hold.reason
        return protocol.error(
            f"{HELD}: the doorway was taken by {holder} ({reason}) while {name!r} waited to start; "
            f"it did NOT run. Ask again when that work ends",
            error_type="DoorwayHeld", refused=True, refusal=HELD, tool=name, holder=holder,
            reason=reason)

    def _release_hold_of(self, conn, how: str) -> None:
        """Release the hold `conn` placed, if it placed one; said by name in the log."""
        with self._admit_lock:
            hold = self._hold
            if hold is None or hold.conn is not conn:
                return
            self._hold = None
            described = hold.describe(time.monotonic())
            self._counts["holds_released_by_disconnect"] += 1
        _log_warn(f"[omni.kit.mcp] HOLD by {hold.holder!r} released: {how} after "
                  f"{described['held_s']:.1f} s; {described['refused']} request(s) refused "
                  f"while held ({described['refused_tools']})")

    def hold_report(self) -> Dict[str, Any]:
        """The hold as anyone may read it (status): never the token."""
        with self._admit_lock:
            if self._hold is None:
                return {"held": False}
            return self._hold.describe(time.monotonic())

    def _refuse_frame(self, conn, command: Any, envelope: Dict[str, Any]) -> None:
        """Answer a frame that never became a request, IN ORDER.

        While the connection's earlier request is unanswered the refusal is held and sent right
        after that answer: the send lock stops bytes interleaving, but only this stops a refusal
        for frame 2 being read as the answer to frame 1. A connection that keeps sending into an
        unanswered request is closed after `MAX_DEFERRED` held frames.
        """
        envelope = self._identified(command, envelope)
        with conn.send_lock:
            with self._admit_lock:
                owed = conn.pending
                if owed is not None and not owed.replied:
                    conn.deferred.append(envelope)
                    overrun = len(conn.deferred) > MAX_DEFERRED
                    if not overrun:
                        return
                else:
                    overrun = False
            if overrun:
                _log_warn(f"[omni.kit.mcp] client {conn.connection_id} sent more than "
                          f"{MAX_DEFERRED} frames into an unanswered request; closing it")
                conn.hang_up()
                return
            self._transmit(conn, envelope)

    def _transmit(self, conn, envelope: Any) -> None:
        """One frame out. The caller holds `conn.send_lock` and NO admission lock."""
        try:
            conn.transmit(envelope)
        except Exception as error:                                            # noqa: BLE001
            # A send that failed or timed out may have left half a frame on the wire, so the
            # connection is unusable either way: close it and let the read loop forget it.
            _log_warn(f"[omni.kit.mcp] client {conn.connection_id} could not be answered "
                      f"({type(error).__name__}: {error}); closing it")
            conn.hang_up()

    def submit_local(self, name: str, params: Optional[Dict[str, Any]],
                     on_reply: Callable[[Dict[str, Any]], None],
                     queue_s: Optional[float] = None) -> _LocalCaller:
        """Admission for a caller inside this process — the panel, a host extension.

        The request is queued as DATA exactly as a socket request is — for at most `queue_s`
        seconds; with none stated it starts at once or is refused at once — and `on_reply`
        receives the one terminal envelope: the handler's answer, or a typed refusal. `on_reply` is called on
        the main thread for an answer and on the WATCH THREAD for a `DOORWAY_BUSY`, so a caller
        that touches UI must hand the envelope to its own thread.

        Never `run_coroutine(bridge.dispatch(...))`: a click that lands inside another verb's
        pump would create a task inside the pump, which is the defect itself.

        Returns the caller object; set its `closed = True` to withdraw a request still waiting.
        """
        with self._admit_lock:
            self._sequence += 1
            caller = _LocalCaller(f"local-{self._sequence}", on_reply)
        command: Dict[str, Any] = {"type": name, "params": params or {}}
        if queue_s is not None:
            command["queue_s"] = queue_s
        self._submit(caller, command)
        return caller

    def _admit_locked(self) -> List[_Request]:
        """The requests that may start now, in arrival order, already claimed.

        Head only. A head whose caller has gone is dropped and the next one is considered; a
        head that may not start yet stops the walk, and nothing behind it is looked at."""
        starting: List[_Request] = []
        while self._queue:
            head = self._queue[0]
            if head.conn.closed:
                self._queue.popleft()
                head.state = "dropped"
                self._counts["dropped"] += 1
                _log_info(f"[omni.kit.mcp] admission: #{head.seq} {head.name!r} dropped unstarted "
                          f"— its caller {head.conn.connection_id} is gone")
                continue
            if not self._may_start_locked(head):
                break
            self._queue.popleft()
            self._claim_start_locked(head)
            starting.append(head)
            if head.exclusive:
                break
        return starting

    def _start(self, requests: List[_Request]) -> None:
        """Schedule claimed requests on the main loop, and watch how each one ends."""
        pending = list(requests)
        while pending:
            request = pending.pop(0)
            coro = self._execute_and_reply(request)
            try:
                outcome = self._schedule(coro)
                # THE SCHEDULER'S RETURN IS THE GUARANTEE. Kit's `run_coroutine` returns a task
                # (main thread) or a concurrent future (any other); either can be observed, and
                # work that cannot be observed must not be counted as running.
                if not callable(getattr(outcome, "add_done_callback", None)):
                    raise TypeError(
                        f"the scheduler returned {type(outcome).__name__}, which cannot be "
                        f"observed (no add_done_callback); a task or a future is required")
                # A BACKSTOP, NOT THE MECHANISM: a task destroyed while still pending fires no
                # done-callback at all (measured, CPython 3.10 and 3.11), and the coroutine
                # object being collected is the only event left. Never at interpreter exit.
                weakref.finalize(coro, self._note_abandoned, request).atexit = False
            except Exception as error:                                        # noqa: BLE001
                coro.close()
                self._counts["scheduler_failed"] += 1
                _log_error(f"[omni.kit.mcp] admission: #{request.seq} {request.name!r} could not "
                           f"be scheduled: {type(error).__name__}: {error}")
                self._reply(request, protocol.error(
                    f"{SCHEDULER_FAILED}: {request.name!r} was admitted and the main-loop "
                    f"scheduler refused it ({type(error).__name__}: {error}); it did not run",
                    error_type="DoorwaySchedulerFailed", refused=True, refusal=SCHEDULER_FAILED,
                    tool=request.name))
                pending.extend(self._release(request))
                continue
            outcome.add_done_callback(
                lambda done, request=request: self._observed(request, done))

    def _observed(self, request: _Request, done) -> None:
        """The scheduled work has ended. If it ended without ever entering, answer and release.

        Once the coroutine has entered, its own `finally` owns the answer and the release, so
        this only acts for a request that never began — and it FORBIDS it to begin: a scheduler
        may still run a coroutine whose future was cancelled first (`run_coroutine_threadsafe`
        does), and a verb must not run for a caller already told it did not.
        """
        with self._admit_lock:
            if request.released or request.entered:
                return
            try:
                unfinished = done.cancelled() or done.exception() is not None
            except Exception:                                                 # noqa: BLE001
                unfinished = True
            if not unfinished:
                return               # a superseded attempt that returned at once: not an ending
            request.entered = True   # claimed: it may not begin now
        try:
            how = "was cancelled" if done.cancelled() else f"ended with {done.exception()!r}"
        except Exception as error:                                            # noqa: BLE001
            how = f"ended unreadably ({error!r})"
        self._counts["cancelled"] += 1
        _log_warn(f"[omni.kit.mcp] admission: #{request.seq} {request.name!r} {how} before it "
                  f"ran; answered {CANCELLED} and released")
        self._reply(request, self._cancelled(request, f"its scheduled task {how} before it ran"))
        self._start(self._release(request))

    def _cancelled(self, request: _Request, how: str) -> Dict[str, Any]:
        return protocol.error(
            f"{CANCELLED}: {request.name!r} was admitted and {how}. Whether it had any effect "
            f"is not known to the doorway",
            error_type="DoorwayRequestCancelled", refused=True, refusal=CANCELLED,
            tool=request.name)

    def _note_abandoned(self, request: _Request) -> None:
        """The coroutine object is gone. Runs wherever the collector runs, so it only leaves a
        note: the watch thread does the answering."""
        if request.released or request.entered:
            return
        self._abandoned.append(request)
        with self._admit_lock:
            self._ensure_watch_locked()
            self._watch_wake.notify_all()

    def _ensure_watch_locked(self) -> None:
        if self._watch_thread is None or not self._watch_thread.is_alive():
            self._watch_thread = threading.Thread(
                target=self._watch, name="omni.kit.mcp-admission-watch", daemon=True)
            self._watch_thread.start()

    def _watch(self) -> None:
        """The deadline watch. Alive only while something waits; ends itself when nothing does."""
        while True:
            refusals: List[tuple] = []
            lost: List[_Request] = []
            starting: List[_Request] = []
            with self._admit_lock:
                while self._abandoned:
                    lost.append(self._abandoned.popleft())
                now = time.monotonic()
                for request in [r for r in self._queue if r.deadline <= now]:
                    self._queue.remove(request)
                    request.state = "refused"
                    self._counts["refused_busy"] += 1
                    refusals.append((request, self._busy_locked(request, "deadline")))
                if refusals:
                    starting = self._admit_locked()
                if not refusals and not lost:
                    if not self._queue:
                        self._watch_thread = None
                        return
                    nearest = min(r.deadline for r in self._queue) - now
                    self._watch_wake.wait(timeout=max(0.0, min(nearest, 1.0)))
                    continue
            for request, refusal in refusals:
                _log_warn(f"[omni.kit.mcp] {refusal['message']}")
                # Its own thread: a caller that is not reading must not hold up the next
                # deadline. The send itself is bounded by the socket's send timeout.
                threading.Thread(target=self._reply, args=(request, refusal), daemon=True,
                                 name=f"omni.kit.mcp-refusal-{request.seq}").start()
            for request in lost:
                with self._admit_lock:
                    if request.released or request.entered:
                        continue
                    request.entered = True      # claimed: no later attempt may begin
                self._counts["lost"] += 1
                _log_error(f"[omni.kit.mcp] admission: #{request.seq} {request.name!r} was "
                           f"destroyed before it ran; answered {LOST} and released")
                self._reply(request, protocol.error(
                    f"{LOST}: {request.name!r} was admitted and its task was destroyed before "
                    f"it ran, so it did not run. A handler that advances the simulator from "
                    f"inside its own call and is not marked exclusive does this to every request "
                    f"started beside it",
                    error_type="DoorwayRequestLost", refused=True, refusal=LOST,
                    tool=request.name))
                starting.extend(self._release(request))
            self._start(starting)

    def _busy_locked(self, request: _Request, reason: str) -> Dict[str, Any]:
        """The `DOORWAY_BUSY` envelope: what is in the way, and for how long it has been."""
        now = time.monotonic()
        running = [{"tool": r.name, "exclusive": r.exclusive,
                    "running_s": round(now - (r.started or now), 3)}
                   for r in sorted(self._started.values(), key=lambda r: r.seq)]
        ahead = [r.name for r in self._queue if r.seq < request.seq]
        waited = now - request.arrived
        if running:
            head = next((r for r in running if r["exclusive"]), running[0])
            holder = (f"{head['tool']!r} "
                      f"({'exclusive: it has the main loop to itself' if head['exclusive'] else 'in flight'}"
                      f") has been running {head['running_s']:.1f} s"
                      + (f", with {len(running) - 1} other request(s) in flight"
                         if len(running) > 1 else ""))
        else:
            holder = "nothing is running"
        queue = f"; {len(ahead)} request(s) wait ahead of it ({', '.join(map(repr, ahead[:5]))}" \
                f"{', …' if len(ahead) > 5 else ''})" if ahead else ""
        if reason == "queue_full":
            why = (f"{self._queue_limit} requests are already waiting, which is this doorway's "
                   f"declared limit, so it was not queued")
        elif reason == "no_queue_budget":
            why = ("its caller stated no queue budget (\"queue_s\" on the request), so it "
                   "starts at once or not at all — this doorway does not guess how long a "
                   "caller is willing to wait")
        elif reason == "in_process":
            why = ("it was called in-process by a coroutine, which cannot be held as data, so it "
                   "is admitted at once or not at all")
        else:
            capped = (f", capped from the {request.queue_stated:.1f} s it stated"
                      if request.queue_stated is not None
                      and request.queue_stated > request.queue_s else "")
            why = (f"it could not start within its {request.queue_s:.1f} s queue budget{capped} "
                   f"(waited {waited:.1f} s)")
        return protocol.error(
            f"{BUSY}: {request.name!r} did not run: {why}. {holder}{queue}",
            error_type="DoorwayBusy", refused=True, refusal=BUSY, reason=reason,
            tool=request.name, exclusive=request.exclusive, waited_s=round(waited, 3),
            queue_s=request.queue_s, queue_stated=request.queue_stated,
            running=running, ahead=ahead)

    @staticmethod
    def _identified(command: Dict[str, Any], envelope: Dict[str, Any]) -> Dict[str, Any]:
        """Echo the client's own `id`, when it sent one, so an answer can be matched to a frame."""
        request_id = command.get("id") if isinstance(command, dict) else None
        return envelope if request_id is None else dict(envelope, id=request_id)

    def _reply(self, request: _Request, envelope: Dict[str, Any]) -> bool:
        """Send a request's ONE terminal frame, then whatever was held behind it.

        The connection's send lock is taken FIRST and the claim made inside it, so the frame
        that wins the claim is also the next frame on the wire: nothing can be sent on this
        connection between the claim and the answer. The admission lock is released before any
        byte is written.
        """
        conn = request.conn
        with conn.send_lock:
            with self._admit_lock:
                if request.replied:
                    return False
                request.replied = True
                if conn.pending is request:
                    conn.pending = None
                held, conn.deferred = list(conn.deferred), []
            self._transmit(conn, self._identified(request.command, envelope))
            for refusal in held:
                self._transmit(conn, refusal)
        return True

    def _release(self, request: _Request) -> List[_Request]:
        """Give back what a started request held; returns what may start now. Once per request."""
        with self._admit_lock:
            if request.released:
                return []
            request.released = True
            request.state = "finished"
            self._started.pop(request.seq, None)
            if self._exclusive is request:
                self._exclusive = None
            return self._admit_locked()

    def _forget_connection(self, conn) -> List[_Request]:
        """A connection has closed: remove what it left waiting; returns what may start now."""
        with self._admit_lock:
            conn.closed = True
            for request in [r for r in self._queue if r.conn is conn]:
                self._queue.remove(request)
                request.state = "dropped"
                self._counts["dropped"] += 1
                _log_info(f"[omni.kit.mcp] admission: #{request.seq} {request.name!r} dropped "
                          f"unstarted — connection {conn.connection_id} closed after "
                          f"{time.monotonic() - request.arrived:.3f} s in the queue")
            return self._admit_locked()

    def admission(self) -> Dict[str, Any]:
        """What the admission holds right now (for status and tests)."""
        with self._admit_lock:
            return {"running": len(self._started), "exclusive": self._exclusive is not None,
                    "waiting": [request.name for request in self._queue]}

    def admission_report(self) -> Dict[str, Any]:
        """The contract, the declared limits, who declared what, and the counts so far."""
        with self._lock:
            modules = {o.metadata["module"]: o.metadata.get("admission")
                       for o in self._owners.values() if o.metadata.get("module")}
        with self._admit_lock:
            now = time.monotonic()
            return {
                "contract": ADMISSION_CONTRACT,
                "queue_cap_s": self._queue_cap_s,
                "queue_limit": self._queue_limit,
                "unmarked_pumps": dict(self._unmarked_pumps),
                "modules": modules,
                "undeclared_modules": sorted(m for m, declared in modules.items()
                                             if declared is None),
                "running": [{"tool": r.name, "exclusive": r.exclusive,
                             "running_s": round(now - (r.started or now), 3)}
                            for r in sorted(self._started.values(), key=lambda r: r.seq)],
                "waiting": [{"tool": r.name, "exclusive": r.exclusive,
                             "waited_s": round(now - r.arrived, 3)} for r in self._queue],
                "counts": dict(self._counts),
            }

    async def _execute_and_reply(self, request: _Request) -> None:
        """Main-thread half of one request: dispatch, answer that caller, then let the next in.

        The answer is sent BEFORE admission is released, so a pumping verb's reply is on the wire
        before the request behind it starts."""
        with self._admit_lock:
            if request.entered:
                return              # another attempt began it, or it was answered unstarted
            request.entered = True
        token = _CURRENT_REQUEST.set(request)
        try:
            try:
                response = await self._dispatch_admitted(
                    request.name, request.command.get("params") or {})
            except Exception as e:  # dispatch itself is defensive; this is belt+braces
                _log_error(f"[omni.kit.mcp] dispatch crashed: {e}")
                response = protocol.error(str(e), traceback=traceback.format_exc())
            self._reply(request, response)
        except BaseException:
            # Cancelled, or closed by the collector, after it entered. The caller is still owed
            # its one frame, and what the handler had done by then is not ours to know.
            self._counts["cancelled"] += 1
            self._reply(request, self._cancelled(request, "its task was cancelled while it ran"))
            raise
        finally:
            _CURRENT_REQUEST.reset(token)
            self._start(self._release(request))


def _bound_sends(sock: socket.socket) -> None:
    """Give a client socket a SEND-ONLY timeout (`SO_SNDTIMEO`); `recv` stays blocking.

    `settimeout` would bound the read loop too and close an idle client. Best-effort: a platform
    without the option keeps unbounded sends, which is what every send was before this.
    """
    try:
        seconds = int(SEND_TIMEOUT_S)
        micros = int((SEND_TIMEOUT_S - seconds) * 1_000_000)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO, struct.pack("ll", seconds, micros))
    except (AttributeError, OSError, struct.error) as error:
        _log_warn(f"[omni.kit.mcp] could not bound sends on a client socket: {error}")


# ==================== Owner UI teardown ====================

def _destroy_ui_window(title: str) -> bool:
    """Best-effort destroy of an omni.ui window by workspace title.

    Called on Kit's main thread (registration and unregistration both happen
    inside dispatched tool handlers or extension lifecycle). Off-Kit there is
    no omni.ui and no windows — a clean no-op keeps the bridge testable.
    """
    try:
        import omni.ui as ui
    except ImportError:
        return False
    try:
        window = ui.Workspace.get_window(title)
        if window is None:
            return False
        window.visible = False
        window.destroy()
        return True
    except Exception as e:
        _log_warn(f"[omni.kit.mcp] could not destroy window {title!r}: {e}")
        return False


# ==================== Port discovery (runtime portfiles) ====================
# The bridge advertises itself in a per-user runtime dir so clients that were
# given no port can DISCOVER a running bridge (kit_mcp.client mirrors this
# derivation — a cross-process contract pinned by test, not by import).

def _runtime_dir() -> str:
    base = os.getenv("XDG_RUNTIME_DIR")
    if base:
        return os.path.join(base, "omni-kit-mcp")
    return os.path.join("/tmp" if os.name != "nt" else os.getenv("TEMP", "/tmp"),
                        f"omni-kit-mcp-{os.getuid() if hasattr(os, 'getuid') else 'u'}")


def _portfile_path(port: int) -> str:
    return os.path.join(_runtime_dir(), f"{os.getpid()}-{port}.json")


#: Where a failing call's frames are written, beside the portfiles this process already owns.
#: `OMNI_KIT_MCP_LOG_DIR` lets a cell point it at that run's own state directory, so a run's
#: failures land with the rest of its evidence; unset, the doorway's runtime dir is used, because
#: a traceback nobody can find is the condition this exists to end.
LOG_DIR_ENV = "OMNI_KIT_MCP_LOG_DIR"

#: Params are a caller's payload — `run_python` puts an entire script in one — so the record keeps
#: the SHAPE (names and sizes) rather than the contents, and only these keys by value. They are the
#: labels a caller may attach to say which step it is on; none is required and none is invented.
CONTEXT_KEYS = ("label", "step", "stage", "job", "session_id")

#: Enough to identify the failing call without turning the log into the payload.
MAX_LOGGED_TRACEBACK = 20000


def _failure_log_path() -> str:
    return os.path.join(os.getenv(LOG_DIR_ENV) or _runtime_dir(),
                        f"tool-failures-{os.getpid()}.log")


def _call_context(params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """What the request says about itself, by shape — never its payload."""
    params = params or {}
    context: Dict[str, Any] = {}
    for key in CONTEXT_KEYS:
        if params.get(key) not in (None, ""):
            context[key] = params[key]
    context["params"] = {k: f"{type(v).__name__}[{len(v)}]"
                         if isinstance(v, (str, bytes, list, dict)) else type(v).__name__
                         for k, v in sorted(params.items())}
    return context


def _record_tool_failure(name: str, error: BaseException,
                         params: Optional[Dict[str, Any]],
                         formatted: Optional[str]) -> None:
    """Write a failing call's frames where a human will find them, then return.

    Best-effort in the portfile's sense: a doorway that cannot write its diary must still answer
    the call it is failing. The reply envelope is not touched — refusal semantics are what callers
    branch on, and this is only about the record.
    """
    frames = (formatted or "").strip() or "<no traceback captured>"
    if len(frames) > MAX_LOGGED_TRACEBACK:
        frames = frames[:MAX_LOGGED_TRACEBACK] + "\n… truncated"
    context = _call_context(params)
    headline = (f"[omni.kit.mcp] {name} failed: {type(error).__name__}: {error}"
                + (f" | {json.dumps(context, default=str, sort_keys=True)}" if context else ""))
    _log_error(headline)
    _log_error(f"[omni.kit.mcp] {name} traceback:\n{frames}")
    try:
        path = _failure_log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as handle:
            handle.write(json.dumps({
                "at": time.time(), "tool": name, "pid": os.getpid(),
                "error_type": type(error).__name__, "message": str(error),
                "context": context, "traceback": frames,
            }, default=str, sort_keys=True) + "\n")
    except Exception as e:                                                    # noqa: BLE001
        _log_warn(f"[omni.kit.mcp] could not write the failure log: {e}")


def _app_identity() -> Dict[str, Any]:
    """Best-effort Kit app identity — the WHO of this instance.

    One source of truth for both identity surfaces: the runtime portfile
    (filesystem discovery, same-box) and the ``status`` builtin (wire-level,
    what a remote client verifies against). Degrades to Nones off-Kit so the
    same surface serves tests and real instances.

    ``headless`` is inferred from signals, not one API: the
    ``/app/window/enabled`` carb setting (set false by ``--no-window``) and
    the launch argv. Neither present -> None (unknown), never a guess.
    """
    app = version = None
    headless = None
    try:
        import carb
        s = carb.settings.get_settings()
        app = s.get("/app/name")
        version = s.get("/app/version")
        window_enabled = s.get("/app/window/enabled")
        if window_enabled is not None:
            headless = not bool(window_enabled)
    except Exception:
        pass
    if headless is None and any(
            a in ("--no-window", "--headless") for a in (sys.argv or [])):
        headless = True
    return {"app": app or os.path.basename(sys.argv[0] or "python"),
            "app_version": version, "headless": headless}


def _write_portfile(host: str, port: int) -> None:
    """Best-effort advertisement; never fatal."""
    try:
        os.makedirs(_runtime_dir(), exist_ok=True)
        _sweep_stale_portfiles()
        dial_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
        payload = {"pid": os.getpid(), "port": port, "host": dial_host,
                   "started": time.time(), **_app_identity()}
        with open(_portfile_path(port), "w") as f:
            json.dump(payload, f)
        # Cleanup is layered — no single hook covers every exit path:
        #   stop()   -> removes on graceful extension shutdown;
        #   atexit   -> covers plain-interpreter exits (verified live that
        #               Kit's fast shutdown SKIPS atexit, so it is not enough);
        #   sweep    -> _sweep_stale_portfiles() above: every bridge start
        #               garbage-collects dead-pid leftovers on this box;
        #   clients  -> discovery prunes dead-pid/dead-socket files on read.
        import atexit
        atexit.register(_remove_portfile, port)
    except Exception as e:
        _log_warn(f"[omni.kit.mcp] could not write portfile: {e}")


def _remove_portfile(port: int) -> None:
    try:
        os.unlink(_portfile_path(port))
    except OSError:
        pass


def _sweep_stale_portfiles() -> None:
    """Unlink advertisements whose process is gone (best-effort).

    Kit's fast shutdown (/app/fastShutdown, the SimulationApp default) skips
    both extension on_shutdown AND atexit — verified live on Isaac 5.1 — so a
    headless instance can never guarantee its own cleanup. Each bridge START
    sweeps the box's runtime dir instead: dead files are safe to unlink from
    any process, and a box that launches bridges keeps itself clean."""
    try:
        names = os.listdir(_runtime_dir())
    except OSError:
        return
    for name in names:
        path = os.path.join(_runtime_dir(), name)
        try:
            with open(path) as f:
                pid = int(json.load(f)["pid"])
            os.kill(pid, 0)
        except (OSError, ValueError, KeyError):
            try:
                os.unlink(path)
            except OSError:
                pass


# ==================== Process-global singleton ====================

_BRIDGE: Optional[McpBridge] = None


#: The refusal that ended this process's doorway at boot, when one did (`autoload.refuse_boot`).
#: Kept so that whoever asks for the bridge afterwards is told WHY there is none: a standalone
#: carrier asks in-process, and "bridge not initialized, declare a dependency" sent it to the
#: wrong place for a doorway that had started and then refused.
_BOOT_REFUSAL: Optional[BaseException] = None


def _hold_boot_refusal(refusal: BaseException) -> None:
    """Remember why the doorway did not come up; `get_mcp_bridge` raises it from then on."""
    global _BOOT_REFUSAL
    _BOOT_REFUSAL = refusal


def _create_bridge() -> McpBridge:
    """Build the singleton (called by the bridge extension's on_startup)."""
    global _BRIDGE, _BOOT_REFUSAL
    if _BRIDGE is None:
        _BOOT_REFUSAL = None     # a new startup is a new answer
        _BRIDGE = McpBridge()
    return _BRIDGE


def get_mcp_bridge() -> McpBridge:
    """Return the bridge singleton.

    Raises if the bridge extension hasn't started yet — declare
    ``"omni.kit.mcp" = {}`` in your extension's ``[dependencies]`` so it loads
    first. If it started and REFUSED TO BOOT, raises that refusal instead.
    """
    if _BRIDGE is None:
        if _BOOT_REFUSAL is not None:
            # The extension DID start, and refused. Its own refusal, by its own name.
            raise _BOOT_REFUSAL
        raise RuntimeError(
            "omni.kit.mcp bridge not initialized. Declare 'omni.kit.mcp' as a "
            "dependency in your extension.toml so it starts first.")
    return _BRIDGE


def _destroy_bridge() -> None:
    """Stop and clear the singleton (called by the bridge extension's on_shutdown)."""
    global _BRIDGE
    if _BRIDGE is not None:
        try:
            _BRIDGE.stop()
        except Exception:
            pass
        _BRIDGE = None
