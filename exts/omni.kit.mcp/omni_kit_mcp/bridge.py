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

import inspect
import json
import os
import socket
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

from . import protocol
from .protocol import ToolError

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

    def send(self, obj: Any) -> None:
        with self.send_lock:
            self.sock.sendall(protocol.encode_frame(obj))


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
        self._in_flight: Dict[str, int] = {}           # owner_id -> live dispatch count
        self._draining: Set[str] = set()               # owners awaiting final cleanup

        # Persistent run_python sessions (in-memory, keyed by session_id).
        self._python_sessions: Dict[str, Dict[str, Any]] = {}

        # Socket server state.
        self._socket: Optional[socket.socket] = None
        self._server_thread: Optional[threading.Thread] = None
        self._running = False
        self._connections: Dict[str, _ClientConnection] = {}
        self._port: Optional[int] = None

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

    def unregister_owner(self, owner_id: str) -> None:
        """Remove an owner. Tools leave resolution immediately; final cleanup
        defers until the owner's in-flight dispatches drain (see _dispatch)."""
        with self._lock:
            if owner_id not in self._owners:
                return
            for canonical in self._owner_tools.get(owner_id, set()):
                self._tools.pop(canonical, None)
            self._draining.add(owner_id)
            if self._in_flight.get(owner_id, 0) == 0:
                self._finalize_owner_locked(owner_id)

    def _finalize_owner_locked(self, owner_id: str) -> None:
        """Complete a drained owner's removal. Caller holds the lock."""
        owner = self._owners.pop(owner_id, None)
        if owner and owner.namespace is not None:
            self._namespaces.pop(owner.namespace, None)
        self._owner_tools.pop(owner_id, None)
        self._in_flight.pop(owner_id, None)
        self._draining.discard(owner_id)

    def get_registered_tools(self, include_internal: bool = False) -> Dict[str, Dict[str, Any]]:
        """Discovery view: canonical name -> metadata. Canonical names only —
        the advertised surface never changes with co-load state."""
        with self._lock:
            return {
                t.canonical_name: {
                    "description": t.definition.description,
                    "parameters": t.definition.parameters,
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
        """Drop all persistent run_python sessions (e.g. on a new stage, when
        saved vars may hold now-dead prim/handle references)."""
        with self._lock:
            self._python_sessions.clear()

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

    async def dispatch(self, name: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Resolve + execute one tool call; returns the response envelope.

        The single entrance to tool logic: socket requests, the panel's buttons,
        and any in-process caller all come through here, so a click and an agent
        call are the same code path by construction.
        """
        params = params or {}

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
                result = handler(**params)
                if inspect.isawaitable(result):
                    result = await result
            return protocol.success(result)
        except ToolError as e:
            return protocol.error(str(e), **e.details)
        except TypeError as e:
            # Most common authoring/caller mismatch: bad kwargs. Name the tool's
            # declared parameters in the reply so the caller can self-correct.
            return protocol.error(
                f"{name}: {e}",
                expected_parameters=list(tool.definition.parameters.keys()),
                traceback=traceback.format_exc())
        except Exception as e:
            _log_error(f"[omni.kit.mcp] {name} handler failed: {e}")
            return protocol.error(str(e), traceback=traceback.format_exc())
        finally:
            with self._lock:
                self._in_flight[owner_id] = max(0, self._in_flight.get(owner_id, 1) - 1)
                if owner_id in self._draining and self._in_flight[owner_id] == 0:
                    self._finalize_owner_locked(owner_id)

    def _schedule(self, coro) -> None:
        """Hop a coroutine onto the main loop (Kit's run_coroutine by default)."""
        if self._schedule_coroutine is None:
            from omni.kit.async_engine import run_coroutine
            self._schedule_coroutine = run_coroutine
        self._schedule_coroutine(coro)

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
                        self._try_send(conn, protocol.error(decode_err))
                        continue
                    if not isinstance(command, dict) or "type" not in command:
                        self._try_send(conn, protocol.error(
                            'malformed request: expected {"type": ..., "params": {...}}'))
                        continue
                    self._schedule(self._execute_and_reply(conn, command))
        finally:
            with self._lock:
                conn.closed = True
                self._connections.pop(conn.connection_id, None)
            try:
                conn.sock.close()
            except Exception:
                pass
            _log_info(f"[omni.kit.mcp] client {conn.connection_id} disconnected")

    async def _execute_and_reply(self, conn: _ClientConnection, command: Dict[str, Any]) -> None:
        """Main-thread half of one request: dispatch, then answer that client."""
        try:
            response = await self.dispatch(command.get("type"), command.get("params") or {})
        except Exception as e:  # dispatch itself is defensive; this is belt+braces
            _log_error(f"[omni.kit.mcp] dispatch crashed: {e}")
            response = protocol.error(str(e), traceback=traceback.format_exc())
        self._try_send(conn, response)

    def _try_send(self, conn: _ClientConnection, obj: Any) -> None:
        try:
            conn.send(obj)
        except Exception:
            _log_warn(f"[omni.kit.mcp] client {conn.connection_id} went away mid-reply")


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


def _write_portfile(host: str, port: int) -> None:
    """Best-effort advertisement; never fatal."""
    try:
        os.makedirs(_runtime_dir(), exist_ok=True)
        app = None
        try:
            import carb
            app = carb.settings.get_settings().get("/app/name")
        except Exception:
            pass
        dial_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
        payload = {"pid": os.getpid(), "port": port, "host": dial_host,
                   "app": app or os.path.basename(sys.argv[0] or "python"),
                   "started": time.time()}
        with open(_portfile_path(port), "w") as f:
            json.dump(payload, f)
    except Exception as e:
        _log_warn(f"[omni.kit.mcp] could not write portfile: {e}")


def _remove_portfile(port: int) -> None:
    try:
        os.unlink(_portfile_path(port))
    except OSError:
        pass


# ==================== Process-global singleton ====================

_BRIDGE: Optional[McpBridge] = None


def _create_bridge() -> McpBridge:
    """Build the singleton (called by the bridge extension's on_startup)."""
    global _BRIDGE
    if _BRIDGE is None:
        _BRIDGE = McpBridge()
    return _BRIDGE


def get_mcp_bridge() -> McpBridge:
    """Return the bridge singleton.

    Raises if the bridge extension hasn't started yet — declare
    ``"omni.kit.mcp" = {}`` in your extension's ``[dependencies]`` so it loads
    first.
    """
    if _BRIDGE is None:
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
