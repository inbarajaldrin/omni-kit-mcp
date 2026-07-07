"""knobs.py — one identity per config knob, one precedence rule."""

import sys
import types

import pytest

from omni_kit_mcp import knobs


def test_knob_identity_shape():
    assert knobs.PORT.env == "OMNI_KIT_MCP_PORT"
    assert knobs.PORT.leaf == "autostartPort"
    assert knobs.PORT.settings_keys == (
        "/persistent/exts/omni.kit.mcp/autostartPort",
        "/exts/omni.kit.mcp/autostartPort",
    )
    assert knobs.BIND.env == "OMNI_KIT_MCP_BIND"
    assert knobs.BIND.leaf == "autostartHost"
    assert knobs.TOOL_MODULES.leaf == "toolModules"
    assert knobs.TOOL_PATHS.leaf == "toolPaths"


def test_env_wins_over_settings(monkeypatch):
    monkeypatch.setenv(knobs.PORT.env, "1234")
    _install_fake_carb(monkeypatch, {knobs.PORT.settings_keys[0]: "9999"})
    assert knobs.PORT.read() == "1234"


def test_settings_when_env_unset(monkeypatch):
    monkeypatch.delenv(knobs.PORT.env, raising=False)
    _install_fake_carb(monkeypatch, {knobs.PORT.settings_keys[1]: 4321})
    assert knobs.PORT.read() == 4321


def test_persistent_key_beats_bare_key(monkeypatch):
    monkeypatch.delenv(knobs.PORT.env, raising=False)
    _install_fake_carb(monkeypatch, {
        knobs.PORT.settings_keys[0]: 1111,
        knobs.PORT.settings_keys[1]: 2222,
    })
    assert knobs.PORT.read() == 1111


def test_none_when_nothing_set(monkeypatch):
    monkeypatch.delenv(knobs.PORT.env, raising=False)
    # no carb importable in the test env unless we fake it
    sys.modules.pop("carb", None)
    assert knobs.PORT.read() is None


def test_gateway_env_name_pinned():
    """The gateway shares the env NAME as a cross-process contract — pinned
    here rather than imported (kit_mcp.client must stay dependency-free)."""
    from kit_mcp import client
    assert client.PORT_ENV == knobs.PORT.env


def _install_fake_carb(monkeypatch, values):
    carb = types.ModuleType("carb")
    settings_mod = types.SimpleNamespace(
        get_settings=lambda: types.SimpleNamespace(get=lambda k: values.get(k)))
    carb.settings = settings_mod
    monkeypatch.setitem(sys.modules, "carb", carb)
