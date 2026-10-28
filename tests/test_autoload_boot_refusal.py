"""A configured tool module that fails to load refuses the doorway at boot.

A doorway that skipped a configured module would come up looking healthy and answer
`unknown tool` later, one layer away from the cause, with a one-line log and no traceback. So
the tests below hold boot to four things: the refusal is typed and names the module, it
carries the traceback, nothing configured is served, and the refusal reaches a waiter as a
record plus a quit request rather than as a doorway that looks healthy.
"""

import json
import os
import sys
import textwrap
import types

import pytest

from omni_kit_mcp import autoload
from omni_kit_mcp.autoload import (
    BOOT_REFUSAL_EXIT, TOOL_MODULE_LOAD_REFUSED, ToolModuleLoadRefusal,
    load_configured_tool_modules, refuse_boot)
from omni_kit_mcp.bridge import LOG_DIR_ENV, McpBridge


@pytest.fixture()
def bridge():
    return McpBridge(schedule_coroutine=lambda coro: None)


@pytest.fixture(autouse=True)
def no_held_refusal_leaks():
    """`refuse_boot` holds the refusal on the bridge module; no test leaves one behind."""
    from omni_kit_mcp import bridge as bridge_module
    yield
    bridge_module._BOOT_REFUSAL = None


@pytest.fixture()
def tool_dir(tmp_path, monkeypatch):
    root = tmp_path / "tools"
    root.mkdir()
    monkeypatch.syspath_prepend(str(root))
    created = []

    def make(name, source):
        pkg = root / name
        pkg.mkdir()
        (pkg / "__init__.py").write_text(textwrap.dedent(source))
        created.append(name)
        return pkg

    yield make
    for name in created:
        for mod in [m for m in list(sys.modules) if m == name or m.startswith(name + ".")]:
            del sys.modules[mod]


GOOD = """
    MCP_NAMESPACE = "{ns}"
    DOORWAY_ADMISSION = 1
    def register(registrar):
        @registrar.tool("t", {{}})
        def t():
            return {{}}
"""

RAISES_AT_IMPORT = """
    MCP_NAMESPACE = "grasp"
    def _the_line_that_breaks():
        raise RuntimeError("dictionary changed size during iteration")
    _the_line_that_breaks()
"""


def test_a_healthy_set_loads_exactly_as_the_reporting_loader_does(bridge, tool_dir):
    tool_dir("fine_a", GOOD.format(ns="fa"))
    tool_dir("fine_b", GOOD.format(ns="fb"))
    results = load_configured_tool_modules(bridge, modules=["fine_a", "fine_b"], paths=[])
    assert results == {"fine_a": {"namespace": "fa", "tools": ["fa.t"]},
                       "fine_b": {"namespace": "fb", "tools": ["fb.t"]}}
    assert {"fa.t", "fb.t"} <= set(bridge.get_registered_tools())


def test_a_module_that_raises_at_import_refuses_by_name_with_its_traceback(bridge, tool_dir):
    tool_dir("fine_a", GOOD.format(ns="fa"))
    tool_dir("breaks_at_import", RAISES_AT_IMPORT)
    with pytest.raises(ToolModuleLoadRefusal) as caught:
        load_configured_tool_modules(
            bridge, modules=["fine_a", "breaks_at_import"], paths=[])
    refusal = caught.value
    assert refusal.code == TOOL_MODULE_LOAD_REFUSED
    assert str(refusal).startswith(TOOL_MODULE_LOAD_REFUSED)
    assert list(refusal.failures) == ["breaks_at_import"]
    assert refusal.configured == ["fine_a", "breaks_at_import"]
    frames = refusal.failures["breaks_at_import"]
    # The traceback, not the message: the function and the file the message never named.
    assert "Traceback (most recent call last)" in frames
    assert "_the_line_that_breaks" in frames
    assert os.path.join("breaks_at_import", "__init__.py") in frames
    assert "dictionary changed size during iteration" in frames
    assert "_the_line_that_breaks" in str(refusal)


def test_a_refusal_serves_no_configured_tool_not_even_the_modules_that_loaded(bridge, tool_dir):
    before = set(bridge.get_registered_tools(include_internal=True))
    owners_before = bridge.get_owners()
    tool_dir("fine_a", GOOD.format(ns="fa"))
    tool_dir("breaks_at_import", RAISES_AT_IMPORT)
    tool_dir("fine_b", GOOD.format(ns="fb"))
    with pytest.raises(ToolModuleLoadRefusal):
        load_configured_tool_modules(
            bridge, modules=["fine_a", "breaks_at_import", "fine_b"], paths=[])
    # Only what the doorway itself owns is left: no configured owner, no configured tool.
    assert bridge.get_owners() == owners_before
    assert not {"fine_a", "fine_b", "breaks_at_import"} & set(bridge.get_owners())
    assert set(bridge.get_registered_tools(include_internal=True)) == before


def test_every_failed_module_is_named_in_one_refusal(bridge, tool_dir):
    tool_dir("breaks_at_import", RAISES_AT_IMPORT)
    tool_dir("breaks_in_register", """
        MCP_NAMESPACE = "br"
        def register(registrar):
            raise ValueError("entrypoint exploded")
    """)
    with pytest.raises(ToolModuleLoadRefusal) as caught:
        load_configured_tool_modules(
            bridge, modules=["breaks_at_import", "breaks_in_register", "not_a_module_at_all"],
            paths=[])
    failures = caught.value.failures
    assert sorted(failures) == ["breaks_at_import", "breaks_in_register", "not_a_module_at_all"]
    assert "entrypoint exploded" in failures["breaks_in_register"]
    assert "ModuleNotFoundError" in failures["not_a_module_at_all"]


def test_the_failure_log_line_carries_the_traceback(bridge, tool_dir, capsys):
    tool_dir("breaks_at_import", RAISES_AT_IMPORT)
    with pytest.raises(ToolModuleLoadRefusal):
        load_configured_tool_modules(bridge, modules=["breaks_at_import"], paths=[])
    logged = capsys.readouterr().err
    assert "failed to load breaks_at_import: RuntimeError" in logged
    assert "_the_line_that_breaks" in logged


def test_refuse_boot_writes_the_record_before_it_asks_kit_to_quit(tmp_path, monkeypatch):
    log_dir = tmp_path / "doorway"
    monkeypatch.setenv(LOG_DIR_ENV, str(log_dir))
    refusal = ToolModuleLoadRefusal(
        {"arm_tools": "Traceback (most recent call last):\n  File x, line 1\nRuntimeError: y\n"},
        ["lab_tools", "arm_tools"])
    seen = []

    def quit_app(code):
        # The record is already whole when the quit is asked for.
        seen.append((code, sorted(os.listdir(log_dir))))

    path = refuse_boot(refusal, quit_app=quit_app)
    name = os.path.basename(path)
    assert os.path.dirname(path) == str(log_dir)
    assert name.startswith(f"{autoload.BOOT_REFUSAL_PREFIX}{os.getpid()}-")
    assert name.endswith(".json")
    assert seen == [(BOOT_REFUSAL_EXIT, [name])]
    record = json.loads((log_dir / name).read_text())
    assert record["schema"] == autoload.BOOT_REFUSAL_SCHEMA
    assert record["code"] == TOOL_MODULE_LOAD_REFUSED
    assert record["failed"] == ["arm_tools"]
    assert record["configured"] == ["lab_tools", "arm_tools"]
    assert "RuntimeError: y" in record["tracebacks"]["arm_tools"]
    assert record["pid"] == os.getpid()
    assert record["exit_code"] == BOOT_REFUSAL_EXIT


def _refusal():
    return ToolModuleLoadRefusal(
        {"arm_tools": "Traceback (most recent call last):\nRuntimeError: y\n"},
        ["lab_tools", "arm_tools"])


def test_a_second_refusal_from_the_same_pid_is_a_second_record(tmp_path, monkeypatch):
    """A container's Kit pid repeats from one launch to the next inside one allocation, and a
    bringup tells this launch's record from the last one's BY NAME. Same pid, two names."""
    monkeypatch.setenv(LOG_DIR_ENV, str(tmp_path))
    first = refuse_boot(_refusal(), quit_app=lambda code: None)
    second = refuse_boot(_refusal(), quit_app=lambda code: None)
    assert first != second
    assert sorted(os.listdir(tmp_path)) == sorted(
        [os.path.basename(first), os.path.basename(second)])


def test_a_record_that_cannot_be_written_does_not_skip_the_quit(tmp_path, monkeypatch, capsys):
    blocked = tmp_path / "a-file-where-the-directory-should-be"
    blocked.write_text("")
    monkeypatch.setenv(LOG_DIR_ENV, str(blocked / "doorway"))
    quits = []
    assert refuse_boot(_refusal(), quit_app=quits.append) is None
    assert quits == [BOOT_REFUSAL_EXIT]
    logged = capsys.readouterr().err
    assert "BOOT_REFUSAL_RECORD_UNWRITTEN" in logged
    assert TOOL_MODULE_LOAD_REFUSED in logged          # the refusal itself was still said


def test_the_refusal_says_a_hand_launched_windowed_isaac_quits_too():
    refusal = _refusal()
    assert "hand-launched windowed Isaac included" in str(refusal)
    assert str(refusal) == f"{TOOL_MODULE_LOAD_REFUSED}: {refusal.detail}"


def test_asking_for_the_bridge_after_a_boot_refusal_raises_that_refusal(tmp_path, monkeypatch):
    """What a standalone carrier does: it asks in-process. Both measured incidents were standalone
    runs, and "bridge not initialized, declare a dependency" would have named the wrong thing."""
    from omni_kit_mcp import bridge as bridge_module
    monkeypatch.setenv(LOG_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(bridge_module, "_BRIDGE", None)
    monkeypatch.setattr(bridge_module, "_BOOT_REFUSAL", None)
    with pytest.raises(RuntimeError, match="bridge not initialized"):
        bridge_module.get_mcp_bridge()

    refusal = _refusal()
    refuse_boot(refusal, quit_app=lambda code: None)
    with pytest.raises(ToolModuleLoadRefusal) as caught:
        bridge_module.get_mcp_bridge()
    assert caught.value is refusal
    assert caught.value.code == TOOL_MODULE_LOAD_REFUSED
    assert "RuntimeError: y" in caught.value.detail

    try:                                   # a new startup is a new answer
        assert bridge_module._create_bridge() is bridge_module.get_mcp_bridge()
    finally:
        bridge_module._destroy_bridge()


class _App:
    def __init__(self, uncancellable):
        self.calls = []
        if uncancellable:
            self.post_uncancellable_quit = lambda code: self.calls.append(("uncancellable", code))

    def post_quit(self, code=0):
        self.calls.append(("cancellable", code))


def _kit_app(monkeypatch, app):
    omni = types.ModuleType("omni")
    kit = types.ModuleType("omni.kit")
    kit_app = types.ModuleType("omni.kit.app")
    kit_app.get_app = lambda: app
    omni.kit, kit.app = kit, kit_app
    for name, module in (("omni", omni), ("omni.kit", kit), ("omni.kit.app", kit_app)):
        monkeypatch.setitem(sys.modules, name, module)


def test_the_quit_is_the_one_nothing_can_cancel_when_kit_has_it(monkeypatch, capsys):
    app = _App(uncancellable=True)
    _kit_app(monkeypatch, app)
    autoload._post_quit(BOOT_REFUSAL_EXIT)
    assert app.calls == [("uncancellable", BOOT_REFUSAL_EXIT)]
    assert "BOOT_REFUSAL_QUIT_CANCELLABLE" not in "".join(capsys.readouterr())


def test_a_kit_without_it_gets_the_cancellable_quit_and_the_log_says_so(monkeypatch, capsys):
    app = _App(uncancellable=False)
    _kit_app(monkeypatch, app)
    autoload._post_quit(BOOT_REFUSAL_EXIT)
    assert app.calls == [("cancellable", BOOT_REFUSAL_EXIT)]
    assert "BOOT_REFUSAL_QUIT_CANCELLABLE" in "".join(capsys.readouterr())


def test_the_record_name_a_cells_bringup_reads_is_pinned_on_both_sides():
    """A cross-process contract, pinned by test and not by import: sim-instrumentation's
    `simcell.DOORWAY_BOOT_REFUSAL_PREFIX` spells the same literal and asserts it in
    `tests/test_simcell_doorway_boot_refusal.py`. Move one and the other must move with it."""
    assert autoload.BOOT_REFUSAL_PREFIX == "boot-refusal-"
    assert LOG_DIR_ENV == "OMNI_KIT_MCP_LOG_DIR"


# ------------------------------------------------------------------ the extension's startup
@pytest.fixture()
def extension(monkeypatch):
    """`omni_kit_mcp.extension` imported against stand-ins for the two Kit modules it names."""
    logged = []
    carb = types.ModuleType("carb")
    for level in ("info", "warn", "error"):
        setattr(carb, f"log_{level}",
                lambda msg, _level=level: logged.append((_level, msg)))
    omni = types.ModuleType("omni")
    omni_ext = types.ModuleType("omni.ext")
    omni_ext.IExt = object
    omni.ext = omni_ext
    monkeypatch.setitem(sys.modules, "carb", carb)
    monkeypatch.setitem(sys.modules, "omni", omni)
    monkeypatch.setitem(sys.modules, "omni.ext", omni_ext)
    monkeypatch.delitem(sys.modules, "omni_kit_mcp.extension", raising=False)
    import importlib
    module = importlib.import_module("omni_kit_mcp.extension")
    module.logged = logged
    yield module
    sys.modules.pop("omni_kit_mcp.extension", None)
    module._destroy_bridge()
    from omni_kit_mcp import bridge as bridge_module
    bridge_module._BOOT_REFUSAL = None


def test_startup_refuses_and_binds_no_socket_when_a_configured_module_fails(
        extension, tool_dir, tmp_path, monkeypatch):
    from omni_kit_mcp import bridge as bridge_module, knobs
    tool_dir("fine_a", GOOD.format(ns="fa"))
    tool_dir("breaks_at_import", RAISES_AT_IMPORT)
    log_dir = tmp_path / "doorway"
    monkeypatch.setenv(LOG_DIR_ENV, str(log_dir))
    monkeypatch.setenv(knobs.TOOL_MODULES.env, "fine_a,breaks_at_import")
    monkeypatch.setenv(knobs.PORT.env, "0")
    started, quits = [], []
    monkeypatch.setattr(bridge_module.McpBridge, "start",
                        lambda self, *a, **k: started.append((a, k)))
    monkeypatch.setattr(extension, "refuse_boot",
                        lambda refusal: refuse_boot(refusal, quit_app=quits.append))

    with pytest.raises(ToolModuleLoadRefusal) as caught:
        extension.McpBridgeExtension().on_startup("omni.kit.mcp-test")

    assert list(caught.value.failures) == ["breaks_at_import"]
    assert started == []                                   # no socket: nothing to dial
    assert quits == [BOOT_REFUSAL_EXIT]
    assert bridge_module._BRIDGE is None                   # the half-built bridge is gone
    with pytest.raises(ToolModuleLoadRefusal) as asked:    # and asking for it says why
        bridge_module.get_mcp_bridge()
    assert asked.value is caught.value
    records = [n for n in os.listdir(log_dir) if n.startswith(autoload.BOOT_REFUSAL_PREFIX)]
    assert len(records) == 1
    assert json.loads((log_dir / records[0]).read_text())["failed"] == ["breaks_at_import"]


def test_startup_with_a_healthy_set_binds_and_refuses_nothing(
        extension, tool_dir, tmp_path, monkeypatch):
    from omni_kit_mcp import bridge as bridge_module, knobs
    tool_dir("fine_a", GOOD.format(ns="fa"))
    log_dir = tmp_path / "doorway"
    monkeypatch.setenv(LOG_DIR_ENV, str(log_dir))
    monkeypatch.setenv(knobs.TOOL_MODULES.env, "fine_a")
    monkeypatch.setenv(knobs.PORT.env, "49999")
    started, quits = [], []
    monkeypatch.setattr(bridge_module.McpBridge, "start",
                        lambda self, port, host="localhost": started.append((port, host)))
    monkeypatch.setattr(extension, "refuse_boot",
                        lambda refusal: refuse_boot(refusal, quit_app=quits.append))

    extension.McpBridgeExtension().on_startup("omni.kit.mcp-test")

    assert [port for port, _host in started] == [49999]
    assert quits == []
    assert "fa.t" in bridge_module._BRIDGE.get_registered_tools()
    assert not log_dir.exists() or not os.listdir(log_dir)
