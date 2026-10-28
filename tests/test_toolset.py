"""Declared toolsets — a module's public functions ARE its tool surface."""

import asyncio
import sys
import textwrap
import types

import pytest

from omni_kit_mcp.autoload import load_tool_modules, reload_tool_module
from omni_kit_mcp.bridge import McpBridge
from omni_kit_mcp.toolset import derive_parameters, register_toolset, source_hash


def make_bridge():
    return McpBridge(schedule_coroutine=lambda coro: None)


def dispatch(bridge, name, params=None):
    return asyncio.run(bridge.dispatch(name, params or {}))


def module_from_source(name, source):
    mod = types.ModuleType(name)
    exec(textwrap.dedent(source), mod.__dict__)
    return mod


LIB = '''
    """Demo robotics library."""
    import asyncio
    from os.path import join   # imported name: must NOT become a tool

    MCP_NAMESPACE = "demo"
    DOORWAY_ADMISSION = 1

    def move_to(x: float, y: float, speed: float = 0.5, relative: bool = False):
        """Move the effector to (x, y)."""
        return {"x": x, "y": y, "speed": speed, "relative": relative}

    async def settle(frames: int = 3):
        """Advance N frames and report (stepping-style: async, awaited on the loop)."""
        await asyncio.sleep(0)
        return {"settled": frames}

    def fail(reason: str = "bad pose"):
        """Always raises — typed-error probe."""
        raise ValueError(reason)

    def _helper():
        return "private"
'''


def test_schema_derivation_from_signature():
    mod = module_from_source("demo_lib", LIB)
    params = derive_parameters(mod.move_to)
    assert params["x"] == {"type": "number", "required": True}
    assert params["speed"] == {"type": "number", "default": 0.5}
    # bool must map to boolean, never integer (bool is an int subclass)
    assert params["relative"] == {"type": "boolean", "default": False}


def test_optional_hint_unwraps_and_untyped_stays_open():
    from typing import Optional

    def fn(q: Optional[float] = None, anything=None):
        return q, anything

    params = derive_parameters(fn)
    assert params["q"] == {"type": "number", "default": None}
    assert params["anything"] == {"default": None}   # no guessed type


def test_pydantic_style_model_advertises_schema():
    class Pose:                                      # pydantic shape, no dep
        @classmethod
        def model_json_schema(cls):
            return {"properties": {"x": {"type": "number"}}}

    def fn(pose: Pose):
        return pose

    spec = derive_parameters(fn)["pose"]
    assert spec["type"] == "object" and spec["schema"]["properties"]
    assert spec["required"] is True


def test_register_toolset_sync_async_and_exclusions():
    bridge = make_bridge()
    info = register_toolset(bridge, module_from_source("demo_lib", LIB))
    assert info["namespace"] == "demo"               # MCP_NAMESPACE honored
    # public functions only: no imported join, no _helper
    assert info["tools"] == ["demo.fail", "demo.move_to", "demo.settle"]
    assert info["source_hash"] is None               # in-memory module: no file

    r = dispatch(bridge, "demo.move_to", {"x": 1.0, "y": 2.0})
    assert r["status"] == "success" and r["result"]["speed"] == 0.5
    r = dispatch(bridge, "demo.settle", {"frames": 7})   # async def: awaited
    assert r["result"] == {"settled": 7}

    desc = bridge.get_registered_tools()["demo.move_to"]["description"]
    assert desc == "Move the effector to (x, y)."


def test_all_declares_the_surface_exactly():
    bridge = make_bridge()
    mod = module_from_source(
        "cherry_lib", textwrap.dedent(LIB) + '\n__all__ = ["move_to"]\n')
    info = register_toolset(bridge, mod, namespace="cherry")
    assert info["tools"] == ["cherry.move_to"]


def test_typed_error_propagation():
    """A library exception surfaces as a typed error envelope — class name,
    message, traceback — never a success-shaped payload."""
    bridge = make_bridge()
    register_toolset(bridge, module_from_source("err_lib", LIB))
    r = dispatch(bridge, "demo.fail", {})
    assert r["status"] == "error"
    assert r["error_type"] == "ValueError"
    assert r["message"] == "bad pose"
    assert r["traceback"].rstrip().splitlines()[-1] == "ValueError: bad pose"


# ---- the zero-boilerplate autoload path (no register() entrypoint) ----

PKG_V1 = '''
    """Scene library v1."""
    MCP_NAMESPACE = "scene"
    DOORWAY_ADMISSION = 1

    def spawn(name: str = "cube"):
        """Spawn an object."""
        return {"spawned": name}
'''


@pytest.fixture()
def lib_pkg(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    d = tmp_path / "scene_lib"
    d.mkdir()
    yield d
    for name in [n for n in list(sys.modules)
                 if n == "scene_lib" or n.startswith("scene_lib.")]:
        del sys.modules[name]


def test_autoload_fallback_reload_and_status_hash(lib_pkg):
    """The dispatch requirement end-to-end: declare by having no register(),
    add a verb to the library == publish it, and the status hash moves so a
    client can detect the box serving older code than it expects."""
    bridge = make_bridge()
    (lib_pkg / "__init__.py").write_text(textwrap.dedent(PKG_V1))
    res = load_tool_modules(bridge, modules=["scene_lib"], paths=[])
    assert res["scene_lib"]["tools"] == ["scene.spawn"]
    assert dispatch(bridge, "scene.spawn", {})["result"] == {"spawned": "cube"}

    h1 = dispatch(bridge, "status")["result"]["toolsets"]["scene"]
    assert h1                                        # real file -> real hash

    # add a verb to the library — that IS publishing it
    (lib_pkg / "__init__.py").write_text(
        textwrap.dedent(PKG_V1) + textwrap.dedent('''
        async def settle(frames: int = 2):
            """Wait N frames."""
            return {"ok": frames}
        '''))
    info = reload_tool_module(bridge, "scene_lib")
    assert "scene.settle" in info["tools"]
    assert dispatch(bridge, "scene.settle", {})["result"] == {"ok": 2}

    h2 = dispatch(bridge, "status")["result"]["toolsets"]["scene"]
    assert h2 and h2 != h1                           # staleness is detectable


def test_the_loaders_own_entrypoints_are_not_derived_as_tools():
    """A package is ASKED for these by name, so they are not part of its agent-facing surface.

    `install_policy` is the one that matters: derived as a tool it would hand an agent a way to
    re-arm — or re-install — the very gate that refuses that agent's calls.
    """
    import types

    mod = types.ModuleType("surface_pkg")
    mod.MCP_NAMESPACE = "surface"
    for name in ("a_real_verb", "register", "register_tools", "install_policy"):
        fn = (lambda: {"ok": True})
        fn.__module__ = "surface_pkg"
        fn.__name__ = name
        setattr(mod, name, fn)

    from omni_kit_mcp.toolset import derive_tools

    derived = {tool.name for tool in derive_tools(mod)}
    assert derived == {"a_real_verb"}, derived
