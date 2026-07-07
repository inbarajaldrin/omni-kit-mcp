"""Reference client for the omni.kit.mcp bridge — stdlib-only, single-file.

The one implementation of the caller side of the wire protocol (NDJSON over
TCP; see the repo README): framing, envelope decoding, connection health, and
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
OSError/TimeoutError = couldn't reach or lost the bridge. RuntimeError = port
resolution failed (no port given and discovery found zero or several bridges).

CLI (replaces printf|nc recipes):

    python3 -m kit_mcp.client <tool> ['{"json":"params"}'] [--port N] [--host H] [--timeout S]
    python3 -m kit_mcp.client --list-bridges     # bridges advertised on this box

Port resolution: explicit --port/port= > $OMNI_KIT_MCP_PORT > auto-discovery
via the runtime portfiles when exactly one bridge is running (loud error when
several are). Exit codes: 0 = success · 1 = bridge answered with an error ·
2 = bridge unreachable / usage error.
"""

import json
import os
import socket
from typing import Any, Dict, Optional

# The cross-process public name for the bridge port. Kept in sync with the
# bridge-side knob table (omni_kit_mcp.knobs.PORT.env) by test, not by import —
# this file must stay dependency-free and copyable.
PORT_ENV = "OMNI_KIT_MCP_PORT"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_TIMEOUT = 300.0


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


def _resolve_port(port) -> int:
    """Explicit > $OMNI_KIT_MCP_PORT > discovery-if-unambiguous > loud error."""
    if port:
        return int(port)
    env = os.getenv(PORT_ENV)
    if env:
        return int(env)
    bridges = discover_bridges()
    if len(bridges) == 1:
        return int(bridges[0]["port"])
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


class BridgeClient:
    """Persistent connection: one socket, health-checked, reconnecting.

    call() retries exactly once when the connection proves stale or breaks
    around the send — never after a timeout, because a timed-out tool may have
    executed (retrying would re-execute a possibly non-idempotent tool).
    """

    def __init__(self, host: str = DEFAULT_HOST, port: Optional[int] = None,
                 timeout: float = DEFAULT_TIMEOUT):
        self.host = host
        self._port = port          # resolved lazily so env-less import works
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._buffer = b""

    @property
    def port(self) -> int:
        return _resolve_port(self._port)

    # -- lifecycle --

    def connect(self) -> None:
        if self._sock is None:
            self._sock = socket.create_connection((self.host, self.port),
                                                  timeout=self.timeout)
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

    def _roundtrip(self, tool: str, params: Dict[str, Any], timeout: float) -> Any:
        self._sock.settimeout(timeout)
        frame = json.dumps({"type": tool, "params": params}).encode("utf-8") + b"\n"
        self._sock.sendall(frame)
        while b"\n" not in self._buffer:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("bridge closed the connection mid-response")
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        return _decode(json.loads(line.decode("utf-8")))

    def call(self, tool: str, params: Optional[Dict[str, Any]] = None, *,
             timeout: Optional[float] = None) -> Any:
        """Call one tool; returns the payload or raises (see module docstring)."""
        params = params or {}
        t = self.timeout if timeout is None else timeout
        if not self._healthy():
            self.close()
        for attempt in (0, 1):
            try:
                self.connect()
                return self._roundtrip(tool, params, t)
            except socket.timeout:
                self.close()   # response may still arrive on the old socket
                raise TimeoutError(
                    f"no response for {tool!r} within {t}s (the tool may still "
                    f"be executing on Kit's main thread)")
            except (ConnectionError, BrokenPipeError, ConnectionResetError, OSError):
                self.close()
                if attempt:
                    raise


def call(tool: str, params: Optional[Dict[str, Any]] = None, *,
         host: str = DEFAULT_HOST, port: Optional[int] = None,
         timeout: float = DEFAULT_TIMEOUT) -> Any:
    """One-shot call: fresh socket, fitted timeout, closed afterwards."""
    with BridgeClient(host=host, port=port, timeout=timeout) as client:
        return client.call(tool, params, timeout=timeout)


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
    parser.add_argument("--host", default=DEFAULT_HOST)
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
