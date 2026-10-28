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
    assert reg.add(td("hello", lambda: {"hi": 1},
                      params={"who": {"type": "string", "required": True}})) == "pa.hello"
    meta = bridge.get_registered_tools()["pa.hello"]
    # the registry serves parameters pre-interpreted as field specs (schema.py)
    assert meta["fields"] == [{"name": "who", "kind": "string", "enum": None,
                               "required": True, "default": None,
                               "has_default": False, "description": ""}]


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

    bridge.register_owner("A", "a", metadata={"module": "modA", "admission": 1}).add(
        td("slow", slow))

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


def test_socket_second_frame_before_the_first_answer_is_refused_in_order():
    """The protocol has no request ids, so a connection carries one request at a time. A frame
    sent before the previous one is answered is refused by name — and AFTER that answer, so the
    two replies cannot be read the wrong way round."""
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
            assert replies[0] == {"status": "success", "result": {"n": 1}}
            assert replies[1]["status"] == "error"
            assert replies[1]["refusal"] == "DOORWAY_PIPELINED"
            # the connection is still good for the next request
            assert roundtrip(c, {"type": "demo.echo", "params": {"n": 3}})["result"] == {"n": 3}


# ==================== owner windows (reload-safe UI) ====================

class _FakeUiWindow:
    def __init__(self, title):
        self.title = title
        self.visible = True
        self.destroyed = False

    def destroy(self):
        self.destroyed = True


def _install_fake_omni_ui(monkeypatch, windows):
    """Inject a minimal omni.ui with a Workspace serving `windows` by title."""
    import sys as _sys
    import types

    ui = types.ModuleType("omni.ui")

    class Workspace:
        @staticmethod
        def get_window(title):
            w = windows.get(title)
            return None if (w is None or w.destroyed) else w

    ui.Workspace = Workspace
    omni_pkg = types.ModuleType("omni")
    omni_pkg.ui = ui
    monkeypatch.setitem(_sys.modules, "omni", omni_pkg)
    monkeypatch.setitem(_sys.modules, "omni.ui", ui)


def test_registrar_window_offkit_is_noop():
    """No omni.ui (plain test env) -> declaring windows records + no-ops."""
    bridge = make_bridge()
    reg = bridge.register_owner("A", "a")
    assert reg.window("Panel A") == "Panel A"
    assert bridge.get_owners()["A"]["windows"] == ["Panel A"]
    bridge.unregister_owner("A")   # must not raise without a UI runtime


def test_unregister_destroys_declared_windows(monkeypatch):
    """Reload self-healing: the owner's windows die with the owner, so the
    on-screen UI can't keep driving a retired module instance's closures."""
    windows = {}
    _install_fake_omni_ui(monkeypatch, windows)
    bridge = make_bridge()
    bridge.register_owner("A", "a").window("Panel A")   # declared at register time
    windows["Panel A"] = _FakeUiWindow("Panel A")       # tool builds it later
    bridge.unregister_owner("A")
    assert windows["Panel A"].destroyed


def test_window_declaration_destroys_stale_same_title(monkeypatch):
    """A window left by a previous module instance dies at declaration time."""
    stale = _FakeUiWindow("Panel A")
    _install_fake_omni_ui(monkeypatch, {"Panel A": stale})
    bridge = make_bridge()
    bridge.register_owner("A2", "a").window("Panel A")
    assert stale.destroyed and stale.visible is False


# ==================== file transfer builtins ====================

def test_put_get_file_roundtrip(tmp_path):
    import base64
    bridge = make_bridge()
    payload = b"\x00binary\xff and text"
    dest = str(tmp_path / "nested" / "asset.bin")   # parent doesn't exist yet
    r = dispatch(bridge, "put_file",
                 {"path": dest, "content_b64": base64.b64encode(payload).decode()})
    assert r["status"] == "success" and r["result"]["bytes"] == len(payload)
    with open(dest, "rb") as f:
        assert f.read() == payload
    r = dispatch(bridge, "get_file", {"path": dest})
    assert base64.b64decode(r["result"]["content_b64"]) == payload


def test_put_file_py_purges_pycache(tmp_path):
    """Pushing a .py drops the sibling __pycache__ — the stale-bytecode guard
    (same-second same-size edits pass .pyc's (mtime,size) validation)."""
    import base64
    bridge = make_bridge()
    pkg = tmp_path / "toolpkg"
    cache = pkg / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "mod.cpython-310.pyc").write_bytes(b"stale")
    dispatch(bridge, "put_file",
             {"path": str(pkg / "mod.py"),
              "content_b64": base64.b64encode(b"x = 2\n").decode()})
    assert not cache.exists()


def test_file_builtin_errors_are_envelopes(tmp_path):
    bridge = make_bridge()
    r = dispatch(bridge, "get_file", {"path": str(tmp_path / "absent")})
    assert r["status"] == "error" and "cannot read" in r["message"]
    r = dispatch(bridge, "put_file",
                 {"path": str(tmp_path / "f"), "content_b64": "!!not-base64!!"})
    assert r["status"] == "error" and "base64" in r["message"]


def test_start_sweeps_stale_portfiles(tmp_path, monkeypatch):
    """Kit's fast shutdown skips on_shutdown AND atexit (verified live on
    Isaac 5.1) — so an instance can't guarantee its own cleanup. The next
    bridge START on the box garbage-collects the leftovers."""
    import json
    import os
    import subprocess
    import sys

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    rt = tmp_path / "omni-kit-mcp"
    rt.mkdir()
    dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                          capture_output=True, text=True)
    dead_pid = int(dead.stdout.strip())          # a pid guaranteed exited
    stale = rt / f"{dead_pid}-9999.json"
    stale.write_text(json.dumps({"pid": dead_pid, "port": 9999, "host": "127.0.0.1"}))
    junk = rt / "not-json.json"
    junk.write_text("{broken")                   # unparseable -> also swept

    bridge = make_bridge()
    bridge.start(0, host="127.0.0.1")
    try:
        assert not stale.exists()                # dead advertisement swept
        assert not junk.exists()
        live = list(rt.glob("*.json"))
        assert len(live) == 1                    # exactly our own remains
        assert json.loads(live[0].read_text())["pid"] == os.getpid()
    finally:
        bridge.stop()


def test_portfile_removed_at_exit_even_without_stop(tmp_path):
    """Headless Isaac 5.1's app close()
    skips extension on_shutdown, so stop() never runs — the atexit hook must
    still unadvertise on normal interpreter exit."""
    import glob
    import os
    import subprocess
    import sys

    import omni_kit_mcp

    ext_dir = os.path.dirname(os.path.dirname(omni_kit_mcp.__file__))
    code = (
        "import glob, os\n"
        "from omni_kit_mcp.bridge import McpBridge, _runtime_dir\n"
        "b = McpBridge(schedule_coroutine=lambda c: None)\n"
        "b.start(0, host='127.0.0.1')\n"
        "print(glob.glob(os.path.join(_runtime_dir(), '*.json'))[0])\n"
        # exits WITHOUT b.stop() — the whole point
    )
    env = {**os.environ, "XDG_RUNTIME_DIR": str(tmp_path),
           "PYTHONPATH": ext_dir}
    proc = subprocess.run([sys.executable, "-c", code],
                          capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 0, proc.stderr
    portfile = proc.stdout.strip().splitlines()[-1]
    assert portfile.endswith(".json")        # it WAS advertised while alive
    assert not os.path.exists(portfile)      # and unadvertised at exit


def test_status_reports_instance_identity():
    """The multi-instance verification surface: a client that dialed a port
    can confirm WHO answered — pid/port match this process and this LIVE
    socket, and off-Kit the app fields degrade to honest unknowns, never
    guesses."""
    import os
    bridge = make_bridge()
    bridge.start(0, host="127.0.0.1")   # identity is only real once bound
    try:
        r = dispatch(bridge, "status", {})
        assert r["status"] == "success"
        ident = r["result"]
        assert ident["pid"] == os.getpid()
        assert ident["port"] == bridge._port and ident["port"] > 0
        assert ident["bind_host"] == "127.0.0.1"
        assert ident["headless"] in (None, True, False)  # off-Kit: no signals
        assert ident["app"]                              # falls back to argv[0]
        assert ident["app_version"] is None              # no carb off-Kit
        assert isinstance(ident["namespaces"], list)
        assert ident["tools"] >= 1 and ident["uptime_s"] >= 0
    finally:
        bridge.stop()


def test_stat_file_verifies_content(tmp_path):
    """The ensure loop's primitive: absent -> exists False; present -> the
    exact size + sha256 a driver compares against its local file."""
    import hashlib
    bridge = make_bridge()
    p = tmp_path / "asset.usd"
    r = dispatch(bridge, "stat_file", {"path": str(p)})
    assert r["status"] == "success" and r["result"] == {"path": str(p), "exists": False}
    payload = b"usd bytes \x00\xff" * 100
    p.write_bytes(payload)
    r = dispatch(bridge, "stat_file", {"path": str(p)})["result"]
    assert r["exists"] and r["bytes"] == len(payload)
    assert r["sha256"] == hashlib.sha256(payload).hexdigest()

def test_error_envelope_names_the_exception_class():
    """Typed errors: a handler exception surfaces with its class name, so a
    client can branch on error KIND without parsing the message."""
    bridge = make_bridge()
    reg = bridge.register_owner(owner_id="err_owner", namespace="err")

    @reg.tool("always raises", {})
    def boom():
        raise ValueError("kaput")

    r = dispatch(bridge, "err.boom")
    assert r["status"] == "error"
    assert r["error_type"] == "ValueError"
    assert r["message"] == "kaput"
    assert "ValueError: kaput" in r["traceback"]
