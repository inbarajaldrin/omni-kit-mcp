"""Reference client for the omni.kit.mcp bridge — stdlib-only, single-file.

The one implementation of the caller side of the wire protocol (NDJSON over
TCP; see docs/protocol.md): framing, envelope decoding, connection health, and
retry live here and nowhere else. Every consumer — the MCP gateway, tests,
doctors, project scripts — either imports this module or copies this file
(single-file, stdlib-only, no intra-package imports is part of its contract).

Two lifetimes, one roundtrip:

    from kit_mcp.client import call, BridgeClient, BridgeError

    call("demo.ping", {"message": "hi"}, port=9009)   # one-shot: fresh socket,
                                                      # fitted timeout, no shared
                                                      # fate with other calls
    with BridgeClient(port=9009) as c:                # persistent: one socket,
        c.call("list_tools")                          # health-checked, reconnects
        c.call("run_python", {"code": "result = 1"})  # + retries ONCE on stale
                                                      #   connections (never on
                                                      #   timeout — a timed-out
                                                      #   tool may have executed)

Errors: BridgeError = the bridge answered with an error envelope (its
diagnostic keys — traceback, output, expected_parameters — ride on .details).
OSError/TimeoutError = couldn't reach or lost the bridge. RuntimeError =
endpoint resolution failed (no port given and discovery found zero or several
bridges).

CLI (replaces printf|nc recipes):

    python3 -m kit_mcp.client <tool> ['{"json":"params"}'] [--port N] [--host H] [--timeout S]
    python3 -m kit_mcp.client --list-bridges     # bridges advertised on this box

Endpoint resolution — host and port resolve together, from the same portfile
when discovery is the source (a bridge bound to a tailnet IP advertises that
IP; dialing loopback for its port would be wrong):

    port: explicit --port/port= > $OMNI_KIT_MCP_PORT > auto-discovery via the
          runtime portfiles when exactly one bridge is running (loud error
          when several are)
    host: explicit --host/host= > $OMNI_KIT_MCP_HOST > the discovered
          portfile's advertised host (only when the port came from discovery)
          > 127.0.0.1

Exit codes: 0 = success · 1 = bridge answered with an error ·
2 = bridge unreachable / usage error.
"""

import json
import os
import secrets
import socket
from typing import Any, Dict, Optional

# The cross-process public names for the bridge endpoint. PORT_ENV is kept in
# sync with the bridge-side knob table (omni_kit_mcp.knobs.PORT.env) by test,
# not by import — this file must stay dependency-free and copyable. HOST_ENV
# is the caller-side dial override (which host to connect to; distinct from
# the bridge-side bind knob OMNI_KIT_MCP_BIND).
PORT_ENV = "OMNI_KIT_MCP_PORT"
HOST_ENV = "OMNI_KIT_MCP_HOST"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_TIMEOUT = 300.0


#: THE QUEUE BUDGET. The bridge runs one frame-advancing verb at a time and keeps a request that
#: arrives meanwhile in a queue — but only for as long as the request SAYS it may wait
#: (`queue_s` on the frame). A request that states nothing starts at once or is refused at once
#: with `DOORWAY_BUSY`. The budget is this caller's to state because only the caller knows its
#: own patience: its timeout, less the time the verb needs to RUN once started (the execution
#: reserve), less a margin for the refusal itself to arrive while the caller is still listening.
REFUSAL_MARGIN_S = 1.0
#: With no reserve stated, half of the timeout is kept for the verb to run.
DEFAULT_RESERVE_FRACTION = 0.5
#: `BridgeError.details["refusal"]` when the bridge is alive and occupied. NOT unreachable: a
#: health probe that reads this as a dead cell tears down a healthy run.
BUSY = "DOORWAY_BUSY"

#: THE HOLD, caller side (the bridge's `HOLD_VERB`; names pinned to the bridge by test, never by
#: import). While a hold stands, the bridge refuses every frame that does not carry its token as
#: `HELD`. A process that is the holder's own work — the holder and every subprocess it starts —
#: carries the token in this variable, and every frame this client sends from that process
#: carries it at the frame's top level (`HOLD_FIELD`), never inside `params`.
HOLD_TOKEN_ENV = "OMNI_KIT_MCP_HOLD_TOKEN"
#: A FILE holding the token, read on EVERY call, for a LONG-LIVED process started before any hold
#: existed — a service whose environment was fixed at start-up and that the holder means to let
#: through. The holder writes the file while it holds and removes it on release; absent, no token
#: is sent, and that service is refused like any other caller under the hold.
HOLD_TOKEN_FILE_ENV = "OMNI_KIT_MCP_HOLD_TOKEN_FILE"
HOLD_VERB = "doorway.hold"
RELEASE_VERB = "doorway.release"
HOLD_FIELD = "hold"
HOLD_CAPABILITY = "hold-v1"
HELD = "DOORWAY_HELD"


def queue_budget(timeout: float, execution_reserve_s: Optional[float] = None) -> float:
    """The `queue_s` a request states for a call with this timeout and execution reserve."""
    timeout = float(timeout)
    reserve = (timeout * DEFAULT_RESERVE_FRACTION if execution_reserve_s is None
               else float(execution_reserve_s))
    return max(0.0, timeout - reserve - REFUSAL_MARGIN_S)


class BridgeError(Exception):
    """The bridge answered with an error envelope.

    ``details`` carries the envelope's diagnostic keys (traceback, output,
    expected_parameters, known_tools, ...).
    """

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.details = details or {}


# ---- port discovery (runtime portfiles) ----
# Bridges advertise themselves as <pid>-<port>.json in a per-user runtime dir
# (written by omni_kit_mcp.bridge — the derivation below mirrors it; the pair
# is a cross-process contract pinned by test, not by import).

def _runtime_dir() -> str:
    base = os.getenv("XDG_RUNTIME_DIR")
    if base:
        return os.path.join(base, "omni-kit-mcp")
    return os.path.join("/tmp" if os.name != "nt" else os.getenv("TEMP", "/tmp"),
                        f"omni-kit-mcp-{os.getuid() if hasattr(os, 'getuid') else 'u'}")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def discover_bridges() -> list:
    """Live advertised bridges: [{pid, port, host, app, started}]. Stale
    portfiles (dead pid or dead socket) are pruned best-effort."""
    found = []
    try:
        names = os.listdir(_runtime_dir())
    except OSError:
        return found
    for name in names:
        path = os.path.join(_runtime_dir(), name)
        try:
            with open(path) as f:
                info = json.load(f)
            if not _pid_alive(int(info["pid"])):
                raise OSError("stale portfile: process gone")
            probe = socket.create_connection(
                (info.get("host", DEFAULT_HOST), int(info["port"])), timeout=0.5)
            probe.close()
            found.append(info)
        except Exception:
            try:
                os.unlink(path)
            except OSError:
                pass
    return found


def _resolve_endpoint(host, port) -> tuple:
    """(host, port), resolved together so a discovered pair stays a pair.

    port: explicit > $OMNI_KIT_MCP_PORT > discovery-if-unambiguous > loud error.
    host: explicit > $OMNI_KIT_MCP_HOST > the SAME portfile's advertised host
    (only when the port itself came from discovery) > 127.0.0.1.
    """
    host = host or os.getenv(HOST_ENV)
    if port:
        return (host or DEFAULT_HOST, int(port))
    env = os.getenv(PORT_ENV)
    if env:
        return (host or DEFAULT_HOST, int(env))
    bridges = discover_bridges()
    if len(bridges) == 1:
        return (host or bridges[0].get("host") or DEFAULT_HOST,
                int(bridges[0]["port"]))
    if len(bridges) > 1:
        listing = ", ".join(
            f"{b.get('app', '?')} pid={b['pid']} port={b['port']}" for b in bridges)
        raise RuntimeError(
            f"multiple bridges running ({listing}) — pass port= or set {PORT_ENV}")
    raise RuntimeError(
        f"no bridge found: none advertised in {_runtime_dir()}, and no port "
        f"given (pass port= or set {PORT_ENV})")


def _decode(envelope: Dict[str, Any]) -> Any:
    """Envelope -> payload, or raise BridgeError with the diagnostics."""
    if envelope.get("status") == "success":
        return envelope.get("result", {})
    details = {k: v for k, v in envelope.items() if k not in ("status", "message")}
    raise BridgeError(envelope.get("message", "bridge error"), details)


#: A declared token file that exists and cannot be used (empty, unreadable): raised, never read as
#: "no hold" — a frame sent tokenless by mistake would be refused under the hold it meant to pass.
HOLD_TOKEN_UNREADABLE = "DOORWAY_HOLD_TOKEN_UNREADABLE"


class HoldTokenUnreadable(RuntimeError):
    def __init__(self, detail: str) -> None:
        self.code = HOLD_TOKEN_UNREADABLE
        super().__init__(f"{HOLD_TOKEN_UNREADABLE}: {detail}")


def hold_token(environ: Optional[Dict[str, str]] = None) -> Optional[str]:
    """The hold token this process's frames carry, or None.

    PRECEDENCE: `HOLD_TOKEN_ENV` (the holder's own process and its subprocesses) wins; else the
    contents of the file `HOLD_TOKEN_FILE_ENV` names, read NOW, on every call — the route of a
    long-lived platform service whose environment was fixed before any hold. An ABSENT file is no
    token (no hold stands); a file that EXISTS and is empty or unreadable raises
    `HoldTokenUnreadable`. A stale file from an earlier hold is harmless: the bridge ignores a token
    when no hold stands and compares it with the current one when one does."""
    environ = os.environ if environ is None else environ
    token = environ.get(HOLD_TOKEN_ENV)
    if token:
        return token
    path = environ.get(HOLD_TOKEN_FILE_ENV)
    if not path:
        return None
    try:
        with open(path, "r", encoding="ascii") as handle:
            token = handle.read().strip()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as error:
        raise HoldTokenUnreadable(f"{path}: {type(error).__name__}: {error}") from None
    if not token:
        raise HoldTokenUnreadable(f"{path} is empty")
    return token


class BridgeClient:
    """Persistent connection: one socket, health-checked, reconnecting.

    call() retries exactly once when the connection proves stale or breaks
    around the send — never after a timeout, because a timed-out tool may have
    executed (retrying would re-execute a possibly non-idempotent tool).
    """

    def __init__(self, host: Optional[str] = None, port: Optional[int] = None,
                 timeout: float = DEFAULT_TIMEOUT):
        self._host = host          # endpoint resolved lazily (per connect) so
        self._port = port          # env-less import works and a restarted
        self.timeout = timeout     # bridge can be re-discovered
        self._sock: Optional[socket.socket] = None
        self._buffer = b""

    @property
    def host(self) -> str:
        return _resolve_endpoint(self._host, self._port)[0]

    @property
    def port(self) -> int:
        return _resolve_endpoint(self._host, self._port)[1]

    # -- lifecycle --

    def connect(self) -> None:
        if self._sock is None:
            self._sock = socket.create_connection(
                _resolve_endpoint(self._host, self._port), timeout=self.timeout)
            self._buffer = b""

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
                self._buffer = b""

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    # -- health --

    def _healthy(self) -> bool:
        """Detect a remotely-closed socket (e.g. after a Kit restart)."""
        if self._sock is None:
            return False
        try:
            self._sock.getpeername()
        except OSError:
            return False
        try:
            import select
            readable, _, errored = select.select([self._sock], [], [self._sock], 0)
            if errored:
                return False
            if readable:
                # Readable with no request in flight => close notification
                # (0 bytes) or unexpected data; peek without consuming.
                try:
                    self._sock.setblocking(False)
                    if self._sock.recv(1, socket.MSG_PEEK) == b"":
                        return False
                except BlockingIOError:
                    pass
                except OSError:
                    return False
                finally:
                    self._sock.setblocking(True)
            return True
        except Exception:
            return False

    # -- the roundtrip (the only place framing/decoding happens) --

    def _roundtrip(self, tool: str, params: Dict[str, Any], timeout: float,
                   execution_reserve_s: Optional[float] = None) -> Any:
        self._sock.settimeout(timeout)
        request = {"type": tool, "params": params}
        token = hold_token()
        if token:
            request[HOLD_FIELD] = token
        if timeout is not None:          # a caller with no timeout states no budget either
            request["queue_s"] = queue_budget(timeout, execution_reserve_s)
        frame = json.dumps(request).encode("utf-8") + b"\n"
        self._sock.sendall(frame)
        while b"\n" not in self._buffer:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("bridge closed the connection mid-response")
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        return _decode(json.loads(line.decode("utf-8")))

    def call(self, tool: str, params: Optional[Dict[str, Any]] = None, *,
             timeout: Optional[float] = None,
             execution_reserve_s: Optional[float] = None) -> Any:
        """Call one tool; returns the payload or raises (see module docstring).

        `execution_reserve_s` is how long the tool may run once started; the rest of the timeout
        (less a margin) is what the request may spend queued behind another verb. Unstated, half
        the timeout is reserved. A request that cannot start inside its queue budget raises
        `BridgeError` whose ``details["refusal"]`` is ``"DOORWAY_BUSY"`` — the bridge is alive
        and occupied, and the tool did NOT run."""
        params = params or {}
        t = self.timeout if timeout is None else timeout
        if not self._healthy():
            self.close()
        for attempt in (0, 1):
            try:
                self.connect()
                return self._roundtrip(tool, params, t, execution_reserve_s)
            except socket.timeout:
                self.close()   # response may still arrive on the old socket
                raise TimeoutError(
                    f"no response for {tool!r} within {t}s (the tool may still "
                    f"be executing on Kit's main thread)")
            except (ConnectionError, BrokenPipeError, ConnectionResetError, OSError):
                self.close()
                if attempt:
                    raise


class DoorwayHold:
    """Hold the doorway for one piece of work; every other caller is refused `DOORWAY_HELD`.

        with DoorwayHold("evaluate-planner", "evaluating ticket 3", port=9010) as hold:
            env = {**os.environ, HOLD_TOKEN_ENV: hold.token}   # the work's own subprocesses
            ...
        hold.released   # the bridge's account: how long, and what it refused, by tool

    ONE CONNECTION, KEPT OPEN FOR THE WHOLE HOLD, AND THE HOLD LIVES EXACTLY AS LONG AS IT: the
    bridge releases a hold whose connection closes, so a holder that is killed cannot strand the
    doorway. `release()` asks for the release explicitly (and returns the bridge's counts); the
    socket is closed whatever that answer was.
    """

    def __init__(self, holder: str, reason: str, *, host: Optional[str] = None,
                 port: Optional[int] = None, timeout: float = 30.0):
        self.holder, self.reason = holder, reason
        self.token = secrets.token_hex(32)
        self.placed: Optional[Dict[str, Any]] = None
        self.released: Optional[Dict[str, Any]] = None
        #: Why the explicit release went unanswered, when it did. The hold is released anyway (its
        #: connection is closed), so this is evidence lost, never a hold left standing.
        self.release_error: Optional[str] = None
        self._client = BridgeClient(host=host, port=port, timeout=timeout)
        self._timeout = timeout

    def place(self) -> Dict[str, Any]:
        """Place the hold on a fresh connection; raises `BridgeError` when the bridge refuses."""
        self._client.connect()
        try:
            self.placed = self._client._roundtrip(
                HOLD_VERB, {"token": self.token, "holder": self.holder, "reason": self.reason},
                self._timeout)
        except BaseException:
            self._client.close()
            raise
        return self.placed

    def release(self) -> Optional[Dict[str, Any]]:
        """Ask for the release, then close the connection — which releases it regardless."""
        try:
            if self._client._sock is not None and self.released is None:
                self.released = self._client._roundtrip(
                    RELEASE_VERB, {"token": self.token}, self._timeout)
        finally:
            self._client.close()
        return self.released

    def __enter__(self) -> "DoorwayHold":
        self.place()
        return self

    def __exit__(self, *exc) -> None:
        try:
            self.release()
        except Exception as error:                                            # noqa: BLE001
            # THE CLOSE IN `release` RELEASED IT: an explicit release that could not be answered
            # is not a hold that stands. Recorded by name, never raised over the work's own result.
            self.release_error = f"{type(error).__name__}: {error}"


def call(tool: str, params: Optional[Dict[str, Any]] = None, *,
         host: Optional[str] = None, port: Optional[int] = None,
         timeout: float = DEFAULT_TIMEOUT,
         execution_reserve_s: Optional[float] = None) -> Any:
    """One-shot call: fresh socket, fitted timeout, closed afterwards."""
    with BridgeClient(host=host, port=port, timeout=timeout) as client:
        return client.call(tool, params, timeout=timeout,
                           execution_reserve_s=execution_reserve_s)


# ==================== CLI ====================

def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python3 -m kit_mcp.client",
        description="Call one tool on the omni.kit.mcp bridge. "
                    "Exit codes: 0 success, 1 bridge error, 2 unreachable/usage.")
    parser.add_argument("tool", nargs="?",
                        help="canonical tool name, e.g. list_tools or arm.play_scene")
    parser.add_argument("--list-bridges", action="store_true",
                        help="list running bridges advertised in the runtime dir")
    parser.add_argument("params", nargs="?", default="{}",
                        help='JSON object of parameters (default: {})')
    parser.add_argument("--host", default=None,
                        help=f"bridge host (default: ${HOST_ENV}, else the "
                             "discovered bridge's advertised host)")
    parser.add_argument("--port", type=int, default=None,
                        help=f"bridge port (default: ${PORT_ENV})")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    args = parser.parse_args(argv)

    import sys
    if args.list_bridges:
        bridges = discover_bridges()
        print(json.dumps(bridges, indent=2))
        return 0 if bridges else 2
    if not args.tool:
        parser.print_usage(sys.stderr)
        return 2
    try:
        params = json.loads(args.params)
        if not isinstance(params, dict):
            raise ValueError("params must be a JSON object")
    except ValueError as e:
        print(f"params error: {e}", file=sys.stderr)
        return 2
    try:
        payload = call(args.tool, params, host=args.host, port=args.port,
                       timeout=args.timeout)
    except BridgeError as e:
        print(str(e), file=sys.stderr)
        if e.details:
            print(json.dumps(e.details, indent=2, default=repr), file=sys.stderr)
        return 1
    except (OSError, TimeoutError, RuntimeError) as e:
        print(str(e), file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=2, default=repr))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
