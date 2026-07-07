"""autoload.py — plain-package loading, namespace declaration, hot reload."""

import asyncio
import os
import sys
import textwrap

import pytest

from omni_kit_mcp.autoload import load_tool_modules, reload_tool_module
from omni_kit_mcp.bridge import McpBridge


@pytest.fixture()
def bridge():
    return McpBridge(schedule_coroutine=lambda coro: None)


@pytest.fixture()
def tool_dir(tmp_path, monkeypatch):
    """A temp dir on sys.path to drop fake tool packages into."""
    monkeypatch.syspath_prepend(str(tmp_path))
    created = []

    def make(name, source, submodules=None):
        pkg = tmp_path / name
        pkg.mkdir()
        (pkg / "__init__.py").write_text(textwrap.dedent(source))
        for sub, src in (submodules or {}).items():
            (pkg / f"{sub}.py").write_text(textwrap.dedent(src))
        created.append(name)
        return pkg

    yield make
    for name in created:  # keep sys.modules clean across tests
        for mod in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
            del sys.modules[mod]


def dispatch(bridge, name, params=None):
    return asyncio.run(bridge.dispatch(name, params or {}))


def test_load_registers_namespace_and_tools(bridge, tool_dir):
    tool_dir("pkg_a", """
        MCP_NAMESPACE = "pa"
        def register(registrar):
            @registrar.tool("hello tool", {})
            def hello():
                return {"hi": True}
    """)
    results = load_tool_modules(bridge, modules=["pkg_a"], paths=[])
    assert results["pkg_a"] == {"namespace": "pa", "tools": ["pa.hello"]}
    assert dispatch(bridge, "pa.hello")["result"] == {"hi": True}


def test_namespace_defaults_to_module_name(bridge, tool_dir):
    tool_dir("plainpkg", """
        def register(registrar):
            @registrar.tool("t", {})
            def t():
                return {}
    """)
    results = load_tool_modules(bridge, modules=["plainpkg"], paths=[])
    assert results["plainpkg"]["namespace"] == "plainpkg"
    assert "plainpkg.t" in bridge.get_registered_tools()


def test_bad_module_is_isolated_and_rolled_back(bridge, tool_dir):
    tool_dir("good_pkg", """
        MCP_NAMESPACE = "good"
        def register(registrar):
            @registrar.tool("t", {})
            def t():
                return {}
    """)
    tool_dir("bad_pkg", """
        MCP_NAMESPACE = "bad"
        def register(registrar):
            @registrar.tool("t", {})
            def t():
                return {}
            raise RuntimeError("entrypoint exploded")
    """)
    tool_dir("no_entry", """
        x = 1
    """)
    results = load_tool_modules(
        bridge, modules=["bad_pkg", "no_entry", "good_pkg"], paths=[])
    assert str(results["bad_pkg"]).startswith("ERROR")
    assert "entrypoint" in str(results["no_entry"])
    assert results["good_pkg"]["tools"] == ["good.t"]
    # bad_pkg's half-registration was rolled back: namespace + owner freed
    assert "bad.t" not in bridge.get_registered_tools()
    assert "bad_pkg" not in bridge.get_owners()


def test_reload_picks_up_new_code_and_preserves_state(bridge, tool_dir):
    pkg = tool_dir("re_pkg", """
        MCP_NAMESPACE = "re"
        from . import live_state
        def register(registrar):
            @registrar.tool("version", {})
            def version():
                return {"v": 1, "counter": live_state.COUNTER}
    """, submodules={"live_state": "COUNTER = 0\n"})

    load_tool_modules(bridge, modules=["re_pkg"], paths=[])
    assert dispatch(bridge, "re.version")["result"] == {"v": 1, "counter": 0}

    # Simulate live state accumulating, then edit the tool code.
    sys.modules["re_pkg.live_state"].COUNTER = 99
    (pkg / "__init__.py").write_text(textwrap.dedent("""
        MCP_NAMESPACE = "re"
        from . import live_state
        def register(registrar):
            @registrar.tool("version", {})
            def version():
                return {"v": 2, "counter": live_state.COUNTER}
    """))

    info = reload_tool_module(bridge, "re_pkg")
    assert "re_pkg" in info["reloaded"]
    assert "re_pkg.live_state" not in info["reloaded"]  # *_state preserved
    # New code is live; the state module (and its live value) survived.
    assert dispatch(bridge, "re.version")["result"] == {"v": 2, "counter": 99}


def test_reload_of_never_loaded_module_loads_fresh(bridge, tool_dir):
    tool_dir("late_pkg", """
        MCP_NAMESPACE = "late"
        def register(registrar):
            @registrar.tool("t", {})
            def t():
                return {"late": True}
    """)
    info = reload_tool_module(bridge, "late_pkg")
    assert info["namespace"] == "late"
    assert dispatch(bridge, "late.t")["result"] == {"late": True}
