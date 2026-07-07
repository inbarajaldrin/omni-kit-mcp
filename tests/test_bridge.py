"""bridge.py — owner registry, dispatch, draining teardown, live socket e2e.

The bridge's coroutine scheduler is injectable, so the *entire* server —
socket accept, NDJSON framing, main-loop dispatch, response frames — runs here
under a plain asyncio loop on a background thread, no Kit required.
"""

import asyncio
import json
import socket
import threading
import time

import pytest

from omni_kit_mcp.bridge import BUILTIN_OWNER_ID, McpBridge, ToolDefinition
from omni_kit_mcp.protocol import ToolError


def make_bridge(loop=None):
    if loop is None:
        return McpBridge(schedule_coroutine=lambda coro: asyncio.get_event_loop())
    return McpBridge(
        schedule_coroutine=lambda coro: asyncio.run_coroutine_threadsafe(coro, loop))


def dispatch(bridge, name, params=None):
    return asyncio.run(bridge.dispatch(name, params or {}))


def td(name, handler, params=None, **kw):
    return ToolDefinition(name=name, description=f"{name} desc",
                          parameters=params or {}, handler=handler, **kw)


# ==================== registry ====================

def test_builtins_present_and_bare_named():
    bridge = make_bridge()
    tools = bridge.get_registered_tools()
    assert "run_python" in tools and tools["run_python"]["namespace"] is None
    assert "reload_tools" in tools
    assert "list_tools" not in tools  # internal: served, not advertised
    assert "list_tools" in bridge.get_registered_tools(include_internal=True)


def test_register_owner_canonical_names():
    bridge = make_bridge()
    reg = bridge.register_owner("projA", "pa")
    assert reg.add(td("hello", lambda: {"hi": 1})) == "pa.hello"
    assert "pa.hello" in bridge.get_registered_tools()


def test_registrar_decorator():
    bridge = make_bridge()
    reg = bridge.register_owner("projA", "pa")

    @reg.tool("greets", {"who": {"type": "string"}})
    def greet(who="world"):
        return {"msg": f"hi {who}"}

    assert "pa.greet" in bridge.get_registered_tools()


def test_duplicate_owner_namespace_and_tool_rejected():
    bridge = make_bridge()
    reg = bridge.register_owner("projA", "pa")
    reg.add(td("t", lambda: None))
    with pytest.raises(ValueError):
        bridge.register_owner("projA", "other")
    with pytest.raises(ValueError):
        bridge.register_owner("projB", "pa")
    with pytest.raises(ValueError):
        reg.add(td("t", lambda: None))
    with pytest.raises(ValueError):
        bridge.register_owner("projC", "has.dot")


def test_two_owners_same_local_name_coexist():
    bridge = make_bridge()
    bridge.register_owner("A", "arm").add(td("play", lambda: {"who": "arm"}))
    bridge.register_owner("B", "rover").add(td("play", lambda: {"who": "rover"}))
    assert dispatch(bridge, "arm.play")["result"] == {"who": "arm"}
    assert dispatch(bridge, "rover.play")["result"] == {"who": "rover"}


def test_unregister_owner_removes_only_its_tools():
    bridge = make_bridge()
    bridge.register_owner("A", "a").add(td("t", lambda: None))
    bridge.register_owner("B", "b").add(td("t", lambda: None))
    bridge.unregister_owner("A")
    tools = bridge.get_registered_tools()
    assert "a.t" not in tools and "b.t" in tools
    # namespace is freed for re-registration
    bridge.register_owner("A2", "a")


# ==================== dispatch semantics ====================

def test_dispatch_success_none_and_async():
    bridge = make_bridge()
    reg = bridge.register_owner("A", "a")
    reg.add(td("value", lambda: {"v": 42}))
    reg.add(td("nothing", lambda: None))

    async def async_tool():
        return {"async": True}
    reg.add(td("later", async_tool))

    assert dispatch(bridge, "a.value") == {"status": "success", "result": {"v": 42}}
    assert dispatch(bridge, "a.nothing")["result"] == {}
    assert dispatch(bridge, "a.later")["result"] == {"async": True}


def test_dispatch_kwargs_passed():
    bridge = make_bridge()
    bridge.register_owner("A", "a").add(
        td("add", lambda x, y=1: {"sum": x + y},
           params={"x": {"type": "integer"}, "y": {"type": "integer"}}))
    assert dispatch(bridge, "a.add", {"x": 2, "y": 3})["result"] == {"sum": 5}


def test_dispatch_errors():
    bridge = make_bridge()
    reg = bridge.register_owner("A", "a")

    def boom():
        raise RuntimeError("kaboom")
    reg.add(td("boom", boom))

    def structured():
        raise ToolError("bad input", details={"output": "partial stdout"})
    reg.add(td("structured", structured))

    unknown = dispatch(bridge, "a.nope")
    assert unknown["status"] == "error" and "known_tools" in unknown

    r = dispatch(bridge, "a.boom")
    assert r["status"] == "error" and r["message"] == "kaboom" and "traceback" in r

    r = dispatch(bridge, "a.structured")
    assert r["message"] == "bad input" and r["output"] == "partial stdout"

    r = dispatch(bridge, "a.boom", {"unexpected": 1})  # bad kwargs -> named params
    assert r["status"] == "error" and r["expected_parameters"] == []


def test_unregister_during_inflight_drains():
    """unregister_owner returns immediately, hides the tools, and defers final
    cleanup until the in-flight call completes."""
    bridge = make_bridge()
    started, release = threading.Event(), threading.Event()

    async def slow():
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return {"done": True}

    bridge.register_owner("A", "a", metadata={"module": "modA"}).add(td("slow", slow))

    async def scenario():
        task = asyncio.ensure_future(bridge.dispatch("a.slow", {}))
        while not started.is_set():
            await asyncio.sleep(0.01)
        bridge.unregister_owner("A")
        # gone from resolution immediately, owner still draining
        assert "a.slow" not in bridge.get_registered_tools()
        assert bridge.get_owners()["A"]["draining"] is True
        release.set()
        result = await task
        assert result == {"status": "success", "result": {"done": True}}

    asyncio.run(scenario())
    assert "A" not in bridge.get_owners()  # finalized after drain


# ==================== live socket e2e (no Kit) ====================

class LiveBridge:
    """Bridge + asyncio loop thread + bound socket, torn down cleanly."""

    def __enter__(self):
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.loop_thread.start()
        self.bridge = make_bridge(self.loop)
        with socket.socket() as probe:
            probe.bind(("localhost", 0))
            self.port = probe.getsockname()[1]
        self.bridge.start(self.port)
        return self

    def __exit__(self, *exc):
        self.bridge.stop()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.loop_thread.join(timeout=2)
        self.loop.close()

    def client(self):
        return socket.create_connection(("localhost", self.port), timeout=5)


def roundtrip(sock, obj):
    sock.sendall(json.dumps(obj).encode() + b"\n")
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(65536)
        assert chunk, "connection closed before reply"
        buf += chunk
    line, _ = buf.split(b"\n", 1)
    return json.loads(line.decode())


def test_socket_list_tools_and_call():
    with LiveBridge() as live:
        live.bridge.register_owner("A", "demo").add(
            td("ping", lambda message="pong": {"echo": message},
               params={"message": {"type": "string"}}))
        with live.client() as c:
            resp = roundtrip(c, {"type": "list_tools", "params": {}})
            assert resp["status"] == "success"
            tools = resp["result"]["tools"]
            assert "demo.ping" in tools and tools["demo.ping"]["namespace"] == "demo"
            assert "list_tools" not in tools  # single-wrapped, internal hidden

            resp = roundtrip(c, {"type": "demo.ping", "params": {"message": "hi"}})
            assert resp == {"status": "success", "result": {"echo": "hi"}}


def test_socket_bad_frames_dont_poison_connection():
    with LiveBridge() as live:
        with live.client() as c:
            r = roundtrip(c, "just a string")  # valid JSON, malformed request
            assert r["status"] == "error" and "malformed request" in r["message"]
            c.sendall(b"this is not json\n")
            buf = b""
            while b"\n" not in buf:
                buf += c.recv(65536)
            assert json.loads(buf.split(b"\n")[0])["status"] == "error"
            # connection still serves real requests afterwards
            r = roundtrip(c, {"type": "list_tools", "params": {}})
            assert r["status"] == "success"


def test_socket_two_clients_isolated():
    with LiveBridge() as live:
        live.bridge.register_owner("A", "demo").add(td("ping", lambda: {"ok": 1}))
        c1, c2 = live.client(), live.client()
        try:
            assert roundtrip(c1, {"type": "demo.ping", "params": {}})["status"] == "success"
            c1.close()  # one client dropping...
            time.sleep(0.1)
            # ...must not affect the other
            assert roundtrip(c2, {"type": "demo.ping", "params": {}})["status"] == "success"
        finally:
            c2.close()


def test_socket_pipelined_frames_in_one_packet():
    with LiveBridge() as live:
        live.bridge.register_owner("A", "demo").add(
            td("echo", lambda n=0: {"n": n}, params={"n": {"type": "integer"}}))
        with live.client() as c:
            c.sendall(b'{"type":"demo.echo","params":{"n":1}}\n'
                      b'{"type":"demo.echo","params":{"n":2}}\n')
            buf = b""
            while buf.count(b"\n") < 2:
                buf += c.recv(65536)
            replies = [json.loads(x) for x in buf.strip().split(b"\n")]
            assert sorted(r["result"]["n"] for r in replies) == [1, 2]
