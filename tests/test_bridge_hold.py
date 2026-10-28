"""The doorway hold, over REAL sockets: one caller has the doorway, every other frame is refused
by name and did not run, and the hold cannot outlive the connection that placed it.

The holder here is the reference client's `DoorwayHold` — the class the platform's in-cell
evaluation holds the doorway with — so what these tests prove is the pair, not the bridge alone.
"""
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from kit_mcp import client as reference_client
from omni_kit_mcp import bridge as bridge_module
from omni_kit_mcp.bridge import McpBridge
from test_bridge_admission import Served, pumping_verb, until


@pytest.fixture
def served():
    cell = Served()
    cell.add("plain", lambda: cell.order.append("plain") or {"ok": True})
    yield cell
    cell.close()


def _hold(served, holder="evaluate-planner", reason="evaluating ticket 1"):
    return reference_client.DoorwayHold(holder, reason, host="127.0.0.1", port=served.port)


def test_the_names_the_client_sends_are_the_ones_the_bridge_reads():
    """A cross-process contract pinned by test, never by import (the client is copyable)."""
    for name in ("HOLD_VERB", "RELEASE_VERB", "HOLD_FIELD", "HOLD_CAPABILITY", "HELD"):
        assert getattr(reference_client, name) == getattr(bridge_module, name), name


def test_a_held_doorway_refuses_every_other_frame_by_name_and_runs_nothing(served):
    with _hold(served) as hold:
        assert hold.placed["held"] is True and hold.placed["holder"] == "evaluate-planner"
        assert "token" not in hold.placed, "the token is never echoed"
        agent = served.wire()
        agent.send("t.plain")
        refused = agent.frame()
        assert refused["status"] == "error" and refused["refusal"] == "DOORWAY_HELD"
        assert refused["message"].startswith("DOORWAY_HELD: the doorway is held by "
                                             "evaluate-planner (evaluating ticket 1)")
        assert refused["tool"] == "t.plain" and refused["refused"] is True
        agent.send("run_python", {"code": "result = 1"})
        assert agent.frame()["refusal"] == "DOORWAY_HELD"
        assert served.order == [], "a refused frame did not run"
    assert hold.released["held"] is False and hold.released["released_by"] == "release"
    assert hold.released["refused"] == 2
    assert hold.released["refused_tools"] == {"t.plain": 1, "run_python": 1}
    # RELEASED: the same caller is served again.
    after = served.wire()
    after.send("t.plain")
    assert after.frame() == {"status": "success", "result": {"ok": True}}


def test_the_holders_own_frames_carry_the_token_and_are_served(served, monkeypatch):
    with _hold(served) as hold:
        monkeypatch.setenv(reference_client.HOLD_TOKEN_ENV, hold.token)
        assert reference_client.call("t.plain", port=served.port, host="127.0.0.1",
                                     timeout=10) == {"ok": True}
        monkeypatch.delenv(reference_client.HOLD_TOKEN_ENV)
        with pytest.raises(reference_client.BridgeError) as refused:
            reference_client.call("t.plain", port=served.port, host="127.0.0.1", timeout=10)
        assert refused.value.details["refusal"] == "DOORWAY_HELD"
    assert served.order == ["plain"]


def test_a_wrong_token_is_refused_like_none(served):
    with _hold(served):
        wire = served.wire()
        wire.send("t.plain", hold="0" * 64)
        assert wire.frame()["refusal"] == "DOORWAY_HELD"


def test_status_is_answered_while_held_and_shows_the_hold_but_never_its_token(served):
    with _hold(served) as hold:
        wire = served.wire()
        wire.send("t.plain")
        wire.frame()
        wire.send("status")
        answer = wire.frame()
        assert answer["status"] == "success"
        status = answer["result"]
        assert status["doorway_hold"] == "hold-v1"
        assert status["hold"]["held"] is True and status["hold"]["refused_tools"] == {"t.plain": 1}
        assert hold.token not in repr(status)
    wire = served.wire()
    wire.send("status")
    assert wire.frame()["result"]["hold"] == {"held": False}


def test_an_in_process_caller_cannot_carry_a_token_and_is_refused(served):
    import asyncio

    with _hold(served):
        answer = asyncio.run_coroutine_threadsafe(
            served.bridge.dispatch("t.plain"), served.loop).result(5)
        assert answer["refusal"] == "DOORWAY_HELD"
        replies = []
        served.bridge.submit_local("t.plain", {}, replies.append)
        assert until(lambda: replies)
        assert replies[0]["refusal"] == "DOORWAY_HELD"
    assert served.order == []


def test_a_second_hold_is_refused_while_one_stands(served):
    with _hold(served):
        second = _hold(served, holder="someone-else", reason="also evaluating")
        with pytest.raises(reference_client.BridgeError) as refused:
            second.place()
        assert refused.value.details["refusal"] == "DOORWAY_HOLD_REFUSED"
        assert "already held by evaluate-planner" in str(refused.value)


@pytest.mark.parametrize("params", [
    {"token": "short", "holder": "h", "reason": "r"},
    {"token": "x" * 64, "holder": "", "reason": "r"},
    {"token": "x" * 64, "holder": "h"},
])
def test_a_malformed_hold_is_refused_and_holds_nothing(served, params):
    wire = served.wire()
    wire.send("doorway.hold", params)
    assert wire.frame()["refusal"] == "DOORWAY_HOLD_REFUSED"
    assert served.bridge.hold_report() == {"held": False}


def test_release_with_the_wrong_token_is_refused_and_the_hold_stands(served):
    with _hold(served):
        wire = served.wire()
        wire.send("doorway.release", {"token": "y" * 64})
        assert wire.frame()["refusal"] == "DOORWAY_HOLD_REFUSED"
        assert served.bridge.hold_report()["held"] is True


# ---------------------------------------------------------------- a hold cannot strand
def test_closing_the_holding_connection_releases_the_hold(served):
    hold = _hold(served)
    hold.place()
    assert served.bridge.hold_report()["held"] is True
    hold._client.close()          # the holder vanishes without asking for a release
    assert until(lambda: served.bridge.hold_report() == {"held": False})
    wire = served.wire()
    wire.send("t.plain")
    assert wire.frame()["status"] == "success"


def test_an_exception_inside_the_hold_releases_it_and_is_not_hidden(served):
    with pytest.raises(RuntimeError, match="the work failed"):
        with _hold(served):
            raise RuntimeError("the work failed")
    assert served.bridge.hold_report() == {"held": False}


def test_a_holder_killed_outright_releases_the_hold(served, tmp_path):
    """SIGKILL: no `finally` runs in the holder, and the kernel closing its socket is enough."""
    ready = tmp_path / "placed"
    script = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {os.path.dirname(os.path.dirname(reference_client.__file__))!r})
        from kit_mcp.client import DoorwayHold
        hold = DoorwayHold("doomed", "killed mid-hold", host="127.0.0.1", port={served.port})
        hold.place()
        open({str(ready)!r}, "w").write("1")
        time.sleep(600)
    """)
    holder = subprocess.Popen([sys.executable, "-c", script])
    try:
        assert until(ready.exists, seconds=20)
        assert served.bridge.hold_report()["holder"] == "doomed"
        holder.send_signal(signal.SIGKILL)
        holder.wait(10)
        assert until(lambda: served.bridge.hold_report() == {"held": False}, seconds=10)
    finally:
        if holder.poll() is None:
            holder.kill()


def test_stopping_the_bridge_drops_the_hold():
    bridge = McpBridge(schedule_coroutine=lambda coro: coro.close())
    bridge.start(0, host="127.0.0.1")
    hold = reference_client.DoorwayHold("h", "r", host="127.0.0.1", port=bridge._port)
    hold.place()
    bridge.stop()
    assert bridge.hold_report() == {"held": False}
    # THE CONTEXT EXIT, answered by nobody: recorded by name, never raised over the work's result.
    hold.__exit__(None, None, None)
    assert hold.released is None and hold.release_error
    assert hold._client._sock is None, "the connection is closed whatever the release answered"


# ---------------------------------------------------------------- what was in flight
def test_a_request_waiting_when_the_hold_is_placed_is_refused_not_started_later(served):
    entered, release = pumping_verb(served)
    first, second = served.wire(), served.wire()
    first.send("t.pumps")
    assert entered.wait(5)
    second.send("t.plain", queue_s=30)
    assert until(lambda: served.bridge.admission()["waiting"] == ["t.plain"])
    hold = _hold(served)
    try:
        placed = hold.place()
        # WHAT WAS RUNNING runs on (nothing interrupts a started verb) and is named for the holder
        # to wait out; WHAT WAS WAITING will never start and is told so now.
        assert [row["tool"] for row in placed["running_at_placement"]] == ["t.pumps"]
        assert placed["drained"] == ["t.plain"]
        assert second.frame()["refusal"] == "DOORWAY_HELD"
        release.set()
        assert first.frame()["status"] == "success"
    finally:
        hold.release()
    assert "plain" not in served.order
    assert hold.released["refused_tools"] == {"t.plain": 1}


def test_no_tokenless_frame_starts_under_a_hold_placed_while_frames_race_in(served):
    """The hold is decided in the admission critical section, so a frame that arrives
    as the hold is placed is either served BEFORE it or refused — never started under it."""
    ran_under_hold = []

    def probe():
        if served.bridge.hold_report().get("held"):
            ran_under_hold.append(1)
        return {"ok": True}

    served.add("probe", probe)
    stop = threading.Event()
    answers = []

    def hammer():
        while not stop.is_set():
            try:
                answers.append(reference_client.call("t.probe", host="127.0.0.1",
                                                     port=served.port, timeout=5))
            except reference_client.BridgeError as error:
                answers.append(error.details.get("refusal"))

    threads = [threading.Thread(target=hammer) for _ in range(6)]
    for thread in threads:
        thread.start()
    started_before = 0
    try:
        for _ in range(5):
            with _hold(served) as hold:
                # ALREADY STARTED when the hold was placed: runs on, and is named for the holder
                # to wait out (`running_at_placement`). Nothing else may run under the hold.
                started_before += sum(1 for row in hold.placed["running_at_placement"]
                                      if row["tool"] == "t.probe")
                time.sleep(0.05)
            time.sleep(0.02)
    finally:
        stop.set()
        for thread in threads:
            thread.join(10)
    assert len(ran_under_hold) <= started_before, (
        f"{len(ran_under_hold)} tokenless frame(s) ran under a hold that named only "
        f"{started_before} as already running")
    assert "DOORWAY_HELD" in answers and {"ok": True} in answers


# ---------------------------------------------------------------- the platform's own services
def test_a_long_lived_platform_client_reads_the_token_file_and_is_served_under_the_hold(
        served, tmp_path, monkeypatch):
    """A long-lived process started before any hold has no token in its environment. A process
    naming the token FILE reads it on every call, so the holder can let it through."""
    token_file = tmp_path / "control" / "doorway-hold.token"
    monkeypatch.setenv(reference_client.HOLD_TOKEN_FILE_ENV, str(token_file))
    monkeypatch.delenv(reference_client.HOLD_TOKEN_ENV, raising=False)
    platform = reference_client.BridgeClient(host="127.0.0.1", port=served.port, timeout=10)
    platform.connect()
    try:
        assert platform.call("t.plain") == {"ok": True}, "no hold, no file: served as ever"
        with _hold(served) as hold:
            # THE HOLDER HAS NOT WRITTEN THE FILE YET: the platform service is refused like anyone.
            with pytest.raises(reference_client.BridgeError) as refused:
                platform.call("t.plain")
            assert refused.value.details["refusal"] == "DOORWAY_HELD"
            token_file.parent.mkdir(parents=True)
            token_file.write_text(hold.token + "\n")
            assert platform.call("t.plain") == {"ok": True}, "the same connection, now served"
            # A process WITHOUT the file variable — the agent's — is still refused.
            monkeypatch.delenv(reference_client.HOLD_TOKEN_FILE_ENV)
            with pytest.raises(reference_client.BridgeError) as agent:
                reference_client.call("t.plain", host="127.0.0.1", port=served.port, timeout=10)
            assert agent.value.details["refusal"] == "DOORWAY_HELD"
            monkeypatch.setenv(reference_client.HOLD_TOKEN_FILE_ENV, str(token_file))
            token_file.unlink()
        assert platform.call("t.plain") == {"ok": True}, "released, file gone: served"
    finally:
        platform.close()
    assert served.order.count("plain") == 3


def test_a_stale_token_file_after_release_is_a_wrong_token_and_is_not_trusted(
        served, tmp_path, monkeypatch):
    """A file left behind from an earlier hold names a token the bridge no longer holds: under a
    NEW hold it is refused like any wrong token."""
    token_file = tmp_path / "doorway-hold.token"
    with _hold(served) as first:
        token_file.write_text(first.token)
    monkeypatch.setenv(reference_client.HOLD_TOKEN_FILE_ENV, str(token_file))
    with _hold(served):
        with pytest.raises(reference_client.BridgeError) as refused:
            reference_client.call("t.plain", host="127.0.0.1", port=served.port, timeout=10)
        assert refused.value.details["refusal"] == "DOORWAY_HELD"


def test_the_token_source_order_and_an_unreadable_file_is_loud(tmp_path):
    present = tmp_path / "t"
    present.write_text("filetoken\n")
    assert reference_client.hold_token({}) is None
    assert reference_client.hold_token({reference_client.HOLD_TOKEN_FILE_ENV: str(present)}) \
        == "filetoken"
    assert reference_client.hold_token({reference_client.HOLD_TOKEN_ENV: "env",
                                        reference_client.HOLD_TOKEN_FILE_ENV: str(present)}) \
        == "env", "the holder's own process keeps its environment token"
    assert reference_client.hold_token(
        {reference_client.HOLD_TOKEN_FILE_ENV: str(tmp_path / "absent")}) is None
    with pytest.raises(reference_client.HoldTokenUnreadable) as directory:
        reference_client.hold_token({reference_client.HOLD_TOKEN_FILE_ENV: str(tmp_path)})
    assert directory.value.code == reference_client.HOLD_TOKEN_UNREADABLE
    empty = tmp_path / "empty"
    empty.write_text("\n")
    with pytest.raises(reference_client.HoldTokenUnreadable, match="is empty"):
        reference_client.hold_token({reference_client.HOLD_TOKEN_FILE_ENV: str(empty)})
