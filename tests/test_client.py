"""kit_mcp.client — the reference client, exercised against a live bridge.

test_bridge.py stays raw-socket on purpose (protocol conformance: literal
bytes in, literal envelope out — a symmetric client/server bug can't hide
there). This suite covers the client every consumer actually ships.
"""

import json
import os
import subprocess
import sys

import pytest

from kit_mcp.client import BridgeClient, BridgeError, call
from test_bridge import LiveBridge, td

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture()
def live():
    with LiveBridge() as lb:
        lb.bridge.register_owner("A", "demo").add(
            td("ping", lambda message="pong": {"echo": message},
               params={"message": {"type": "string"}}))
        yield lb


# ==================== one-shot ====================

def test_oneshot_call(live):
    assert call("demo.ping", {"message": "hi"}, port=live.port) == {"echo": "hi"}


def test_oneshot_bridge_error_carries_details(live):
    with pytest.raises(BridgeError) as ei:
        call("demo.nope", port=live.port)
    assert "unknown tool" in str(ei.value)
    assert "demo.ping" in ei.value.details["known_tools"]


def test_missing_port_is_loud(monkeypatch, tmp_path):
    # No env, no explicit port, and an EMPTY runtime dir (no bridge to discover)
    # -> resolution must fail loudly rather than guess.
    monkeypatch.delenv("OMNI_KIT_MCP_PORT", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    with pytest.raises(RuntimeError):
        call("list_tools")


def test_port_from_env(live, monkeypatch):
    monkeypatch.setenv("OMNI_KIT_MCP_PORT", str(live.port))
    assert "demo.ping" in call("list_tools")["tools"]


# ==================== persistent ====================

def test_persistent_client_reuses_socket(live):
    with BridgeClient(port=live.port) as c:
        c.call("demo.ping")
        sock = c._sock
        c.call("demo.ping")
        assert c._sock is sock  # same connection across calls


def test_reconnects_after_bridge_restart(live):
    c = BridgeClient(port=live.port)
    assert c.call("demo.ping") == {"echo": "pong"}
    # Kit restart: bridge goes away and a new one binds the same port.
    live.bridge.stop()
    import test_bridge
    live.bridge = test_bridge.make_bridge(live.loop)
    live.bridge.register_owner("A", "demo").add(td("ping", lambda: {"back": True}))
    live.bridge.start(live.port)
    # Health check must detect the dead socket and reconnect transparently.
    assert c.call("demo.ping") == {"back": True}
    c.close()


# ==================== CLI ====================

def _cli(args, port):
    env = dict(os.environ, PYTHONPATH=REPO_ROOT, OMNI_KIT_MCP_PORT=str(port))
    return subprocess.run([sys.executable, "-m", "kit_mcp.client", *args],
                          capture_output=True, text=True, env=env, timeout=30)


def test_cli_success_exit_0(live):
    r = _cli(["demo.ping", '{"message": "cli"}'], live.port)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {"echo": "cli"}


def test_cli_bridge_error_exit_1(live):
    r = _cli(["demo.nope"], live.port)
    assert r.returncode == 1
    assert "unknown tool" in r.stderr


def test_cli_unreachable_exit_2(live):
    with __import__("socket").socket() as probe:  # a port nobody serves
        probe.bind(("localhost", 0))
        dead_port = probe.getsockname()[1]
    r = _cli(["list_tools"], dead_port)
    assert r.returncode == 2


def test_cli_bad_params_exit_2(live):
    r = _cli(["demo.ping", "not-json"], live.port)
    assert r.returncode == 2


# ==================== import weight ====================

def test_client_imports_without_mcp_sdk():
    """The client layer must import in an environment with no `mcp` package;
    only touching the server layer requires the SDK."""
    code = (
        "import sys\n"
        "class _Block:\n"
        "    def find_spec(self, name, *a, **k):\n"
        "        if name == 'mcp' or name.startswith('mcp.'):\n"
        "            raise ImportError('mcp SDK blocked by test')\n"
        "sys.meta_path.insert(0, _Block())\n"
        "import kit_mcp, kit_mcp.client\n"                 # must succeed
        "kit_mcp.client.BridgeError('x')\n"
        "try:\n"
        "    kit_mcp.server\n"                              # must require the SDK
        "except ImportError:\n"
        "    print('OK')\n"
        "else:\n"
        "    print('SERVER IMPORTED WITHOUT SDK')\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, env=dict(os.environ, PYTHONPATH=REPO_ROOT),
                       timeout=30)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "OK"


# ==================== port discovery ====================

def test_runtime_dir_derivation_pinned(monkeypatch, tmp_path):
    """Bridge and client derive the SAME runtime dir — the cross-process
    contract is pinned here, not shared by import."""
    from omni_kit_mcp import bridge as bridge_mod
    from kit_mcp import client as client_mod
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert bridge_mod._runtime_dir() == client_mod._runtime_dir()
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert bridge_mod._runtime_dir() == client_mod._runtime_dir()


def test_discovery_single_bridge_zero_config(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("OMNI_KIT_MCP_PORT", raising=False)
    with LiveBridge() as live:
        live.bridge.register_owner("A", "demo").add(td("ping", lambda: {"ok": 1}))
        # portfile advertised
        files = os.listdir(os.path.join(str(tmp_path), "omni-kit-mcp"))
        assert files and files[0].endswith(f"-{live.port}.json")
        # no port, no env -> discovered
        assert call("demo.ping") == {"ok": 1}
    # bridge stopped -> portfile removed -> resolution is loud again
    assert os.listdir(os.path.join(str(tmp_path), "omni-kit-mcp")) == []
    with pytest.raises(RuntimeError, match="no bridge found"):
        call("demo.ping")


def test_discovery_multiple_bridges_is_loud(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("OMNI_KIT_MCP_PORT", raising=False)
    with LiveBridge() as a, LiveBridge() as b:
        assert a.port != b.port
        with pytest.raises(RuntimeError, match="multiple bridges"):
            call("list_tools")
        # explicit port still cuts through the ambiguity
        assert "run_python" in call("list_tools", port=a.port)["tools"]


def test_cli_list_bridges(monkeypatch, tmp_path):
    env_dir = str(tmp_path)
    with LiveBridge() as live:
        # write portfiles under our isolated runtime dir for the subprocess
        monkeypatch.setenv("XDG_RUNTIME_DIR", env_dir)
        from omni_kit_mcp import bridge as bridge_mod
        bridge_mod._write_portfile("localhost", live.port)
        env = dict(os.environ, PYTHONPATH=REPO_ROOT, XDG_RUNTIME_DIR=env_dir)
        env.pop("OMNI_KIT_MCP_PORT", None)
        r = subprocess.run([sys.executable, "-m", "kit_mcp.client", "--list-bridges"],
                           capture_output=True, text=True, env=env, timeout=30)
        assert r.returncode == 0, r.stderr
        listed = json.loads(r.stdout)
        assert listed and listed[0]["port"] == live.port


def test_ephemeral_bind_and_discovery(monkeypatch, tmp_path):
    """port=0 binds an OS-assigned port, learns it, advertises it — the
    same-app-second-instance fallback path."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("OMNI_KIT_MCP_PORT", raising=False)
    import asyncio, threading
    import test_bridge
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    bridge = test_bridge.make_bridge(loop)
    try:
        bridge.start(0)
        assert bridge._port and bridge._port != 0    # real port learned
        bridges = __import__("kit_mcp.client", fromlist=["x"]).discover_bridges()
        assert len(bridges) == 1 and bridges[0]["port"] == bridge._port
        assert "run_python" in call("list_tools")["tools"]   # zero-config reach
    finally:
        bridge.stop()
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=2)
        loop.close()
