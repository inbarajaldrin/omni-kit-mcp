"""Gateway rediscovery — schema changes on the bridge must propagate.

The staleness contract: refresh adds new tools AND re-registers tools whose
bridge meta changed (the bridge-side reload_tools case); an unchanged registry
is a no-op. Curated-view filtering still applies during rediscovery.
"""

import pytest

pytest.importorskip("mcp")

from mcp.server.fastmcp import FastMCP

import kit_mcp.server as gw


@pytest.fixture()
def fresh_gateway(monkeypatch):
    server = FastMCP("test")
    monkeypatch.setattr(gw, "_NAME_MAP", {})
    monkeypatch.setattr(gw, "_REGISTERED_META", {})
    return server


def _fake_registry(monkeypatch, tools):
    class FakeClient:
        def call(self, tool, params=None, **kw):
            assert tool == "list_tools"
            return {"tools": tools}
    monkeypatch.setattr(gw, "get_client", lambda: FakeClient())


def _served(server, name):
    return server._tool_manager.get_tool(name)


def test_new_then_unchanged_then_changed(fresh_gateway, monkeypatch):
    server = fresh_gateway
    meta_v1 = {"description": "ping v1",
               "parameters": {"message": {"type": "string"}},
               "namespace": "demo", "owner_id": "A"}
    _fake_registry(monkeypatch, {"demo.ping": meta_v1})

    assert gw.discover_and_register_tools(server) == ["demo.ping"]
    assert _served(server, "demo__ping").description == "ping v1"

    # unchanged registry -> no-op
    assert gw.discover_and_register_tools(server) == []

    # bridge-side reload changed description AND params -> re-registered
    meta_v2 = {"description": "ping v2",
               "parameters": {"message": {"type": "string"},
                              "count": {"type": "integer"}},
               "namespace": "demo", "owner_id": "A"}
    _fake_registry(monkeypatch, {"demo.ping": meta_v2})
    assert gw.discover_and_register_tools(server) == ["demo.ping (updated)"]
    tool = _served(server, "demo__ping")
    assert tool.description == "ping v2"
    assert "count" in tool.parameters["properties"]


def test_curated_view_filter_holds_on_refresh(fresh_gateway, monkeypatch):
    server = fresh_gateway
    monkeypatch.setenv("KIT_MCP_NAMESPACE", "pm")
    monkeypatch.setenv("KIT_MCP_BUILTINS", "0")
    _fake_registry(monkeypatch, {
        "pm.read_state": {"description": "d", "parameters": {},
                          "namespace": "pm", "owner_id": "P"},
        "demo.ping": {"description": "d", "parameters": {},
                      "namespace": "demo", "owner_id": "A"},
        "run_python": {"description": "d", "parameters": {},
                       "namespace": None, "owner_id": "omni.kit.mcp"},
    })
    added = gw.discover_and_register_tools(server)
    assert added == ["pm.read_state"]           # namespace + builtins gates hold
    assert _served(server, "pm__read_state")
    served_names = set(server._tool_manager._tools)
    assert "run_python" not in served_names
    assert "demo__ping" not in served_names
