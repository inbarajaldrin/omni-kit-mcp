"""The optional registered-tool census is durable, domain-blind and fail-open."""

import asyncio
import json
import os
import subprocess
import sys

from omni_kit_mcp import bridge as bridge_module
from omni_kit_mcp.bridge import McpBridge, ToolDefinition
from omni_kit_mcp import telemetry


def _bridge():
    return McpBridge(schedule_coroutine=lambda coro: asyncio.get_event_loop())


def _dispatch(bridge, name, params=None):
    return asyncio.run(bridge.dispatch(name, params or {}))


def _tool(handler):
    return ToolDefinition("run_python", "test", {}, handler)


def test_no_declaration_takes_the_original_dispatch_path(monkeypatch):
    monkeypatch.delenv(telemetry.SINK_ENV, raising=False)
    bridge = _bridge()
    seen = []
    original = bridge._dispatch_unobserved

    async def same_path(name, params=None):
        seen.append((name, params))
        return await original(name, params)

    bridge._dispatch_unobserved = same_path
    monkeypatch.setattr(telemetry.time, "time", lambda: (_ for _ in ()).throw(
        AssertionError("the nil telemetry path sampled a clock")))
    reg = bridge.register_owner("test", "lab")
    reg.add(_tool(lambda: {"ok": True}))

    assert _dispatch(bridge, "lab.run_python") == {"status": "success", "result": {"ok": True}}
    assert seen == [("lab.run_python", {})]
    assert bridge._telemetry is None


def test_a_completed_counter_survives_immediate_process_death(tmp_path):
    path = tmp_path / "authoring-telemetry.jsonl"
    code = (
        "import os\n"
        "from omni_kit_mcp.telemetry import Sink\n"
        f"Sink({str(path)!r}).record('run_python', {{}}, 1.0, {{'status':'success'}})\n"
        "os._exit(17)\n"
    )
    died = subprocess.run([sys.executable, "-c", code], env=os.environ.copy())

    assert died.returncode == 17
    assert json.loads(path.read_text())["tool"] == "run_python"


def test_every_call_is_appended_without_payload_and_sequence_survives_restart(tmp_path, monkeypatch):
    path = tmp_path / "authoring-telemetry.jsonl"
    monkeypatch.setenv(telemetry.SINK_ENV, str(path))
    for secret in ("first-secret", "second-secret"):
        bridge = _bridge()
        reg = bridge.register_owner("test", "lab")
        reg.add(_tool(lambda **_: {"ok": True}))
        assert _dispatch(bridge, "lab.run_python", {"code": secret})["status"] == "success"

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["sequence"] for row in rows] == [1, 2]
    assert all(row["schema"] == "pa.authoring-telemetry/1" for row in rows)
    assert all(row["event"] == "tool_call" for row in rows)
    assert "first-secret" not in path.read_text() and "second-secret" not in path.read_text()
    assert all(row["arguments_sha256"].startswith("sha256:") for row in rows)


def test_a_dead_sink_counts_the_drop_and_never_changes_the_answer(monkeypatch):
    monkeypatch.setenv(telemetry.SINK_ENV, "/proc/no-such-directory/telemetry.jsonl")
    warnings = []
    monkeypatch.setattr(bridge_module, "_log_warn", warnings.append)
    bridge = _bridge()
    reg = bridge.register_owner("test", "lab")
    reg.add(_tool(lambda: {"ok": True}))

    answer = _dispatch(bridge, "lab.run_python")

    assert answer == {"status": "success", "result": {"ok": True}}
    assert bridge._telemetry._dropped == 1
    assert any("telemetry dropped" in line for line in warnings)
