"""A failing call's frames are recorded before the envelope is serialised.

`ToolError` is the path every in-Kit exception takes: `run_python` catches whatever the code
raised and re-raises it as `ToolError` with the traceback in `details`. The bridge logs those
frames before serialising the envelope, so they survive even when a caller surfaces only the
message.
"""

import asyncio
import json
import os

import pytest

from omni_kit_mcp import bridge as bridge_module
from omni_kit_mcp.bridge import McpBridge, ToolDefinition
from omni_kit_mcp.protocol import ToolError


def make_bridge():
    return McpBridge(schedule_coroutine=lambda coro: asyncio.get_event_loop())


def dispatch(bridge, name, params=None):
    return asyncio.run(bridge.dispatch(name, params or {}))


def td(name, handler, params=None, **kw):
    return ToolDefinition(name=name, description=f"{name} desc",
                          parameters=params or {}, handler=handler, **kw)


@pytest.fixture
def logged(monkeypatch, tmp_path):
    monkeypatch.setenv(bridge_module.LOG_DIR_ENV, str(tmp_path))
    lines = []
    monkeypatch.setattr(bridge_module, "_log_error", lines.append)
    monkeypatch.setattr(bridge_module, "_log_warn", lines.append)
    return lines, tmp_path


def _records(directory):
    path = os.path.join(directory, f"tool-failures-{os.getpid()}.log")
    if not os.path.exists(path):
        return []
    with open(path) as handle:
        return [json.loads(line) for line in handle if line.strip()]


def test_a_tool_error_traceback_is_logged_and_not_only_returned(logged):
    """The run_python path: its frames reach the log, not only the reply."""
    lines, directory = logged
    bridge = make_bridge()
    reg = bridge.register_owner("projA", "a")

    def boom(**_):
        raise ToolError("dictionary changed size during iteration",
                        details={"output": "partial", "traceback": "Traceback…\n  in _sim_time_node"})
    reg.add(td("run_python", boom, params={"code": {"type": "string"}}))

    reply = dispatch(bridge, "a.run_python", {"code": "x" * 5000, "label": "object_stream"})

    # The envelope is untouched: callers branch on these and refusal semantics must not move.
    assert reply["status"] == "error"
    assert reply["message"] == "dictionary changed size during iteration"
    assert reply["output"] == "partial"

    assert any("_sim_time_node" in line for line in lines), "the frames never reached the log"
    record = _records(directory)[-1]
    assert record["tool"] == "a.run_python"
    assert record["error_type"] == "ToolError"
    assert "_sim_time_node" in record["traceback"]
    assert record["context"]["label"] == "object_stream"


def test_the_record_keeps_the_payload_s_shape_and_not_its_contents(logged):
    """`run_python` ships an entire script; a log that inlined it would be the payload."""
    lines, directory = logged
    bridge = make_bridge()
    reg = bridge.register_owner("projA", "a")

    def boom(**_):
        raise ToolError("nope", details={})
    reg.add(td("t", boom, params={"code": {"type": "string"}}))

    secret = "SUPERSECRET" * 100
    dispatch(bridge, "a.t", {"code": secret})

    blob = json.dumps(_records(directory)[-1]) + "".join(lines)
    assert "SUPERSECRET" not in blob
    assert _records(directory)[-1]["context"]["params"]["code"] == f"str[{len(secret)}]"


def test_every_failure_path_records(logged):
    lines, directory = logged
    bridge = make_bridge()
    reg = bridge.register_owner("projA", "a")

    def raiser(**_):
        raise RuntimeError("plain")
    reg.add(td("plain", raiser))

    def typed(only_this=None):
        return only_this
    reg.add(td("typed", typed, params={"only_this": {"type": "string"}}))

    dispatch(bridge, "a.plain")
    dispatch(bridge, "a.typed", {"wrong_kwarg": 1})

    recorded = {r["error_type"] for r in _records(directory)}
    assert "RuntimeError" in recorded and "TypeError" in recorded


def test_a_log_that_cannot_be_written_never_takes_the_call_down(logged, monkeypatch):
    """Best-effort in the portfile's sense: a doorway that cannot write its diary must still
    answer the call it is failing."""
    lines, _ = logged
    monkeypatch.setenv(bridge_module.LOG_DIR_ENV, "/proc/nonexistent/nowhere")
    bridge = make_bridge()
    reg = bridge.register_owner("projA", "a")

    def boom(**_):
        raise ToolError("still answered", details={})
    reg.add(td("t", boom))

    reply = dispatch(bridge, "a.t")
    assert reply["status"] == "error" and reply["message"] == "still answered"
    assert any("could not write the failure log" in line for line in lines)


def test_a_successful_call_records_nothing(logged):
    _, directory = logged
    bridge = make_bridge()
    reg = bridge.register_owner("projA", "a")
    reg.add(td("ok", lambda **_: {"fine": True}))

    assert dispatch(bridge, "a.ok")["status"] == "success"
    assert _records(directory) == []
