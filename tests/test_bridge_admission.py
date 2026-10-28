"""Request admission over REAL sockets, with a pump that re-enters the loop as Kit's does.

THE SEAM. Kit steps its asyncio loop from an observer on the app's update event, so a handler
that calls `app.update()` from inside its own task re-enters `loop._run_once()` while a task step
is on the stack. asyncio then refuses to enter any other TASK ("Cannot enter into task … while
another task … is being executed") and the refused task is destroyed pending. Here the bridge runs
on a real asyncio loop on a thread, clients are real TCP connections, and `pump` is that same
re-entry — so the failure these tests guard is the measured one, not a model of it. (The test this
file replaces stood a pump in with `threading.Event.wait()`, which never re-entered anything.)

What a real Kit adds and this cannot show — the update observer's ordering, PhysX and USD work
inside the nested update, the real tool modules' marks — is the box proof's job.
"""
import asyncio
import functools
import json
import os
import socket
import threading
import time

import pytest

from kit_mcp import client as reference_client
from omni_kit_mcp import bridge as bridge_module
from omni_kit_mcp.bridge import EXCLUSIVE_ATTRIBUTE, McpBridge, ToolDefinition

#: The acceptance pump. 30 s is the ruled length; the suite runs it scaled, and the full length
#: runs when asked for by name (`DOORWAY_ACCEPTANCE_PUMP_S=30`).
ACCEPTANCE_PUMP_S = float(os.environ.get("DOORWAY_ACCEPTANCE_PUMP_S", "3"))

#: The stated bound on how long an exclusive request waits behind SUSTAINED ordinary traffic:
#: it is delayed only by the ordinaries that arrived before it, each 100 ms here.
EXCLUSIVE_START_BOUND_S = 1.0


def exclusive(handler):
    setattr(handler, EXCLUSIVE_ATTRIBUTE, True)
    return handler


def pump(release, limit_s=120.0):
    """Advance the loop from INSIDE the current task until `release` is set — Kit's nested update."""
    loop = asyncio.get_running_loop()
    deadline = time.monotonic() + limit_s
    while not release.is_set() and time.monotonic() < deadline:
        loop.call_soon(lambda: None)        # as Kit's run_once does: never block in select()
        loop._run_once()
        time.sleep(0.002)


class Wire:
    """One raw TCP client. Records every frame it is sent, with its arrival time."""

    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.buffer = b""

    def send(self, tool, params=None, **top_level):
        frame = dict({"type": tool, "params": params or {}}, **top_level)
        self.sock.sendall(json.dumps(frame).encode() + b"\n")
        return self

    def frame(self, timeout=10.0):
        """The next frame, or None when none arrives in `timeout`."""
        self.sock.settimeout(timeout)
        try:
            while b"\n" not in self.buffer:
                chunk = self.sock.recv(65536)
                if not chunk:
                    return None
                self.buffer += chunk
        except socket.timeout:
            return None
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def close(self):
        self.sock.close()


class Served:
    def __init__(self, scheduler=None):
        self.loop = asyncio.new_event_loop()
        self.loop_errors = []
        self.loop.set_exception_handler(
            lambda _loop, context: self.loop_errors.append(context.get("message", "")
                                                           + " " + repr(context.get("exception"))))
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.scheduled = []

        def schedule(coro):
            outcome = asyncio.run_coroutine_threadsafe(coro, self.loop)
            self.scheduled.append(outcome)
            return outcome

        self.bridge = McpBridge(schedule_coroutine=scheduler or schedule)
        self.registrar = self.bridge.register_owner("proj", "t")
        self.order = []
        self.wires = []
        self.bridge.start(0, host="127.0.0.1")
        self.port = self.bridge._port

    def add(self, name, handler):
        self.registrar.add(ToolDefinition(name=name, description=name, parameters={},
                                          handler=handler))

    def wire(self):
        wire = Wire(self.port)
        self.wires.append(wire)
        return wire

    def close(self):
        for wire in self.wires:
            wire.close()
        self.bridge.stop()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)


@pytest.fixture
def served():
    cell = Served()
    yield cell
    cell.close()


def until(predicate, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def pumping_verb(served, name="pumps"):
    """An exclusive verb that pumps until released. Returns (entered, release)."""
    entered, release = threading.Event(), threading.Event()

    @exclusive
    def pumps():
        entered.set()
        pump(release)
        served.order.append(name)
        return {"ok": name}

    served.add(name, pumps)
    return entered, release


def idle(bridge):
    return bridge.admission() == {"running": 0, "exclusive": False, "waiting": []}


# ------------------------------------------------------------------ the seam is real
def test_THE_DEFECT_an_unmarked_pump_destroys_the_request_beside_it_and_names_itself(served):
    """Without the mark, the second request is created as a task inside the pump and destroyed —
    the measured loss, reproduced. The bridge's witness then NAMES the verb on its own reply,
    counts it in `status`, treats it as exclusive from then on, and starts the stranded request
    again (it never entered, so nothing ran twice): even the FIRST unmarked pump loses nothing."""
    entered, release = threading.Event(), threading.Event()

    def unmarked():
        entered.set()
        pump(release)
        return {"ok": 1}

    served.add("unmarked", unmarked)
    served.add("plain", lambda: served.order.append("plain") or {"ok": 2})
    first, second = served.wire(), served.wire()
    first.send("t.unmarked")
    assert entered.wait(5)
    second.send("t.plain")
    assert until(lambda: any("Cannot enter into task" in e for e in served.loop_errors)), \
        "the pump model must reproduce asyncio's refusal, or these tests guard nothing"
    release.set()

    answer = first.frame()
    assert answer["status"] == "success"
    assert answer["doorway_notice"].startswith("DOORWAY_UNMARKED_PUMP: 't.unmarked'")
    assert "1 request(s) started beside it had their tasks destroyed and were started again " \
           "('t.plain')" in answer["doorway_notice"]
    assert second.frame() == {"status": "success", "result": {"ok": 2}}
    assert served.order == ["plain"], "started again exactly once"
    report = served.bridge.admission_report()
    assert report["unmarked_pumps"] == {"t.unmarked": 1} and report["counts"]["rescued"] == 1
    assert served.bridge._is_exclusive({"type": "t.unmarked"}) is True
    assert until(lambda: idle(served.bridge)), "a destroyed request must not wedge admission"


def test_a_request_arriving_inside_a_marked_pump_is_kept_as_data_and_answered(served):
    entered, release = pumping_verb(served)
    served.add("plain", lambda: served.order.append("plain") or {"ok": 2})
    first, second = served.wire(), served.wire()
    first.send("t.pumps")
    assert entered.wait(5)
    second.send("t.plain", queue_s=10)
    assert until(lambda: served.bridge.admission()
                 == {"running": 1, "exclusive": True, "waiting": ["t.plain"]})
    assert second.frame(timeout=0.3) is None, "nothing is answered while the pump holds the loop"
    release.set()
    assert first.frame()["status"] == "success"
    assert second.frame() == {"status": "success", "result": {"ok": 2}}
    assert served.order == ["pumps", "plain"]
    assert not any("Cannot enter into task" in e or "destroyed" in e for e in served.loop_errors)
    assert until(lambda: idle(served.bridge))


# ------------------------------------------------------------------ order
def test_requests_are_served_in_arrival_order_and_neither_class_jumps_the_other(served):
    """E (exclusive, pumping) is running. A (ordinary), B (exclusive), C (ordinary) arrive in
    that order. They run A, B, C: the exclusive B is not jumped by the later ordinary C, and the
    earlier ordinary A is not jumped by B."""
    entered, release = pumping_verb(served, "E")
    served.add("A", lambda: served.order.append("A") or {})
    served.add("B", exclusive(lambda: served.order.append("B") or {}))
    served.add("C", lambda: served.order.append("C") or {})
    holder = served.wire().send("t.E")
    assert entered.wait(5)
    wires = []
    for name in ("A", "B", "C"):
        wires.append(served.wire().send(f"t.{name}", queue_s=10))
        assert until(lambda: served.bridge.admission()["waiting"][-1:] == [f"t.{name}"])
    release.set()
    assert holder.frame()["status"] == "success"
    assert [w.frame()["status"] for w in wires] == ["success"] * 3
    assert served.order == ["E", "A", "B", "C"]


def test_a_waiting_exclusive_is_not_bypassed_by_a_later_ordinary_request(served):
    gate = {}

    async def slow():
        gate["event"] = asyncio.Event()
        await gate["event"].wait()
        served.order.append("slow")
        return {}

    served.add("slow", slow)
    served.add("X", exclusive(lambda: served.order.append("X") or {}))
    served.add("later", lambda: served.order.append("later") or {})
    a = served.wire().send("t.slow")
    assert until(lambda: "event" in gate)
    x = served.wire().send("t.X", queue_s=10)
    assert until(lambda: served.bridge.admission()["waiting"] == ["t.X"])
    later = served.wire().send("t.later", queue_s=10)
    assert until(lambda: served.bridge.admission()["waiting"] == ["t.X", "t.later"])
    assert later.frame(timeout=0.3) is None and served.order == [], \
        "the later ordinary request waits behind the exclusive one; it does not run beside slow"
    served.loop.call_soon_threadsafe(gate["event"].set)
    assert [w.frame()["status"] for w in (a, x, later)] == ["success"] * 3
    assert served.order == ["slow", "X", "later"]


def test_ordinary_requests_that_overlap_no_exclusive_one_still_run_side_by_side(served):
    gate = {}

    async def slow():
        gate["event"] = asyncio.Event()
        await gate["event"].wait()
        return {}

    served.add("slow", slow)
    served.add("plain", lambda: {"ok": 2})
    a = served.wire().send("t.slow")
    assert until(lambda: "event" in gate)
    assert served.wire().send("t.plain").frame() == {"status": "success", "result": {"ok": 2}}, \
        "answered while the first is still in flight, with no queue budget stated at all"
    served.loop.call_soon_threadsafe(gate["event"].set)
    assert a.frame()["status"] == "success"


def test_sustained_ordinary_traffic_cannot_starve_a_waiting_exclusive(served):
    """Three clients poll at 10 Hz and each poll takes 100 ms, so an ordinary request is ALWAYS
    in flight — the shape that starved the exclusive request for ever under the old bypass. It
    starts within EXCLUSIVE_START_BOUND_S, because only what arrived before it can delay it."""
    async def poll():
        await asyncio.sleep(0.1)
        return {}

    started = {}

    @exclusive
    def pumps():
        started["at"] = time.monotonic()
        return {}

    served.add("poll", poll)
    served.add("pumps", pumps)
    stop, outcomes = threading.Event(), []

    def poller(offset):
        time.sleep(offset)
        wire = Wire(served.port)
        while not stop.is_set():
            wire.send("t.poll", queue_s=5)
            outcomes.append(wire.frame())
        wire.close()

    threads = [threading.Thread(target=poller, args=(i * 0.033,), daemon=True) for i in range(3)]
    for thread in threads:
        thread.start()
    assert until(lambda: len(outcomes) >= 6)
    asked = time.monotonic()
    answer = served.wire().send("t.pumps", queue_s=5).frame()
    stop.set()
    for thread in threads:
        thread.join(timeout=5)
    assert answer["status"] == "success"
    assert started["at"] - asked < EXCLUSIVE_START_BOUND_S, started["at"] - asked
    assert all(o is not None and (o["status"] == "success" or o.get("refusal")) for o in outcomes)


# ------------------------------------------------------------------ the queue budget
def test_a_frame_that_states_no_queue_budget_starts_now_or_is_refused_now(served):
    """A caller this doorway knows nothing about (a raw socket, an older client) is never queued
    on a patience the server invented: within a second it has a typed refusal naming the verb in
    the way and how long it has run."""
    entered, release = pumping_verb(served)
    served.add("plain", lambda: served.order.append("plain") or {})
    served.wire().send("t.pumps")
    assert entered.wait(5)
    time.sleep(0.2)
    asked = time.monotonic()
    refusal = served.wire().send("t.plain").frame(timeout=2)
    assert time.monotonic() - asked < 1.0
    assert refusal["status"] == "error" and refusal["refusal"] == "DOORWAY_BUSY"
    assert refusal["reason"] == "no_queue_budget" and refusal["refused"] is True
    assert refusal["running"][0]["tool"] == "t.pumps" and refusal["running"][0]["exclusive"]
    assert refusal["running"][0]["running_s"] >= 0.2
    assert "'t.pumps'" in refusal["message"] and "has been running" in refusal["message"]
    release.set()
    assert until(lambda: idle(served.bridge)) and served.order == ["pumps"], "it never ran"


def test_a_queue_budget_that_runs_out_is_refused_WHILE_the_pump_still_holds_the_loop(served):
    entered, release = pumping_verb(served)
    served.add("plain", lambda: served.order.append("plain") or {})
    served.wire().send("t.pumps")
    assert entered.wait(5)
    asked = time.monotonic()
    refusal = served.wire().send("t.plain", queue_s=0.3).frame(timeout=3)
    waited = time.monotonic() - asked
    assert not release.is_set() and 0.3 <= waited < 1.3, waited
    assert refusal["refusal"] == "DOORWAY_BUSY" and refusal["reason"] == "deadline"
    assert refusal["queue_s"] == 0.3 and refusal["waited_s"] >= 0.3
    assert served.bridge.admission()["waiting"] == []
    release.set()
    assert until(lambda: idle(served.bridge)) and served.order == ["pumps"]


def test_a_stated_budget_is_capped_by_the_server_and_says_so(served, monkeypatch):
    monkeypatch.setenv("OMNI_KIT_MCP_QUEUE_CAP_S", "0.2")
    capped = Served()
    try:
        entered, release = pumping_verb(capped)
        capped.add("plain", lambda: {})
        capped.wire().send("t.pumps")
        assert entered.wait(5)
        refusal = capped.wire().send("t.plain", queue_s=600).frame(timeout=3)
        assert refusal["reason"] == "deadline"
        assert refusal["queue_s"] == 0.2 and refusal["queue_stated"] == 600
        assert "capped from the 600.0 s it stated" in refusal["message"]
        release.set()
    finally:
        capped.close()


def test_the_queue_holds_exactly_its_limit_and_the_next_is_refused_at_once(served, monkeypatch):
    monkeypatch.setenv("OMNI_KIT_MCP_QUEUE_LIMIT", "3")
    small = Served()
    try:
        entered, release = pumping_verb(small)
        small.add("plain", lambda: {})
        small.wire().send("t.pumps")
        assert entered.wait(5)
        waiting = [small.wire().send("t.plain", queue_s=10) for _ in range(3)]
        assert until(lambda: len(small.bridge.admission()["waiting"]) == 3)
        refusal = small.wire().send("t.plain", queue_s=10).frame(timeout=2)
        assert refusal["refusal"] == "DOORWAY_BUSY" and refusal["reason"] == "queue_full"
        assert len(small.bridge.admission()["waiting"]) == 3, "running requests are not counted"
        release.set()
        assert [w.frame()["status"] for w in waiting] == ["success"] * 3
    finally:
        small.close()


@pytest.mark.parametrize("bad", ["soon", -1, True, [1]])
def test_a_queue_budget_that_is_not_a_number_of_seconds_is_refused_by_name(served, bad):
    served.add("plain", lambda: served.order.append("plain") or {})
    refusal = served.wire().send("t.plain", queue_s=bad).frame()
    assert refusal["status"] == "error" and '"queue_s"' in refusal["message"]
    assert served.order == []


def test_one_caller_that_is_not_reading_cannot_hold_up_another_callers_refusal(served):
    """Each deadline refusal leaves on its own thread, so the watch never blocks on a socket."""
    entered, release = pumping_verb(served)
    served.add("plain", lambda: {})
    served.wire().send("t.pumps")
    assert entered.wait(5)
    stuck = served.wire().send("t.plain", queue_s=0.2)
    assert until(lambda: len(served.bridge.admission()["waiting"]) == 1)
    connection = served.bridge._queue[0].conn
    unblock, original = threading.Event(), connection.transmit
    connection.transmit = lambda obj: (unblock.wait(10), original(obj))[1]
    asked = time.monotonic()
    refusal = served.wire().send("t.plain", queue_s=0.4).frame(timeout=3)
    assert refusal["refusal"] == "DOORWAY_BUSY" and time.monotonic() - asked < 1.4
    unblock.set()
    assert stuck.frame()["refusal"] == "DOORWAY_BUSY"
    release.set()


def test_accepted_sockets_carry_a_send_only_timeout(served):
    wire = served.wire().send("status")
    assert wire.frame()["status"] == "success"
    connection = next(iter(served.bridge._connections.values()))
    seconds, _micros = __import__("struct").unpack(
        "ll", connection.sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO, 16))
    assert seconds == int(bridge_module.SEND_TIMEOUT_S)
    assert connection.sock.gettimeout() is None, "recv stays blocking: an idle client is not closed"


# ------------------------------------------------------------------ a caller that has gone
class _HostlessSocket:
    def __init__(self):
        self.frames = []

    def sendall(self, frame):
        self.frames.append(frame)


def _hostless_connection(name):
    return bridge_module._ClientConnection(name, _HostlessSocket(), ("hostless", 0))


def test_a_running_handler_sees_its_caller_disconnect():
    async def scenario():
        bridge = McpBridge(schedule_coroutine=asyncio.create_task)
        registrar = bridge.register_owner("proj", "t")
        entered, proceed = asyncio.Event(), asyncio.Event()
        seen = []

        async def waits():
            seen.append(bridge_module.request_caller_disconnected())
            entered.set()
            await proceed.wait()
            seen.append(bridge_module.request_caller_disconnected())
            return {}

        registrar.add(ToolDefinition("waits", "waits", {}, waits))
        caller = _hostless_connection("caller")
        bridge._submit(caller, {"type": "t.waits", "params": {}})
        await entered.wait()
        assert seen == [False] and caller.closed is False
        bridge._forget_connection(caller)
        proceed.set()
        while bridge.admission()["running"]:
            await asyncio.sleep(0)
        assert seen == [False, True]

    asyncio.run(scenario())


def test_concurrent_handlers_only_see_their_own_callers_disconnect():
    async def scenario():
        bridge = McpBridge(schedule_coroutine=asyncio.create_task)
        registrar = bridge.register_owner("proj", "t")
        entered = {name: asyncio.Event() for name in ("gone", "live")}
        proceed = asyncio.Event()
        seen = {}

        async def waits(name):
            entered[name].set()
            await proceed.wait()
            seen[name] = bridge_module.request_caller_disconnected()
            return {"name": name}

        registrar.add(ToolDefinition("waits", "waits", {}, waits))
        gone, live = _hostless_connection("gone"), _hostless_connection("live")
        bridge._submit(gone, {"type": "t.waits", "params": {"name": "gone"}})
        bridge._submit(live, {"type": "t.waits", "params": {"name": "live"}})
        await asyncio.gather(*(event.wait() for event in entered.values()))
        bridge._forget_connection(gone)
        proceed.set()
        while bridge.admission()["running"]:
            await asyncio.sleep(0)
        assert seen == {"gone": True, "live": False}
        assert live.closed is False

    asyncio.run(scenario())


def test_caller_disconnected_is_false_outside_a_handler():
    assert bridge_module.request_caller_disconnected() is False


def test_a_queued_request_whose_caller_disconnected_NEVER_RUNS(served):
    """A recorder that timed out must not start the next episode's recording."""
    entered, release = pumping_verb(served)
    served.add("record", lambda: served.order.append("record") or {})
    served.add("plain", lambda: served.order.append("plain") or {})
    served.wire().send("t.pumps")
    assert entered.wait(5)
    gone = served.wire().send("t.record", queue_s=30)
    stays = served.wire().send("t.plain", queue_s=30)
    assert until(lambda: served.bridge.admission()["waiting"] == ["t.record", "t.plain"])
    gone.close()
    assert until(lambda: served.bridge.admission()["waiting"] == ["t.plain"]), \
        "removed when the close is observed, not when it would have reached the head"
    release.set()
    assert stays.frame()["status"] == "success"
    assert served.order == ["pumps", "plain"]
    assert served.bridge.admission_report()["counts"]["dropped"] == 1


def test_a_caller_that_gave_up_and_reconnected_is_served_once(served):
    entered, release = pumping_verb(served)
    served.add("record", lambda: served.order.append("record") or {"n": len(served.order)})
    served.wire().send("t.pumps")
    assert entered.wait(5)
    first = served.wire().send("t.record", queue_s=30)
    assert until(lambda: served.bridge.admission()["waiting"] == ["t.record"])
    first.close()                                   # its own timeout: it hangs up
    assert until(lambda: served.bridge.admission()["waiting"] == [])
    again = served.wire().send("t.record", queue_s=30)
    assert until(lambda: served.bridge.admission()["waiting"] == ["t.record"])
    release.set()
    assert again.frame()["status"] == "success"
    assert served.order == ["pumps", "record"], "the abandoned request did not also run"


def test_a_withdrawn_in_process_request_at_the_head_is_dropped_and_the_next_one_starts(served):
    entered, release = pumping_verb(served)
    served.add("side", lambda: served.order.append("side") or {})
    served.add("plain", lambda: served.order.append("plain") or {})
    served.wire().send("t.pumps")
    assert entered.wait(5)
    answers = []
    caller = served.bridge.submit_local("t.side", {}, answers.append, queue_s=30)
    behind = served.wire().send("t.plain", queue_s=30)
    assert until(lambda: served.bridge.admission()["waiting"] == ["t.side", "t.plain"])
    caller.closed = True
    release.set()
    assert behind.frame()["status"] == "success"
    assert served.order == ["pumps", "plain"] and answers == []


# ------------------------------------------------------------------ one request per connection
def test_a_second_frame_into_a_QUEUED_request_is_refused_after_that_requests_answer(served):
    entered, release = pumping_verb(served)
    served.add("plain", lambda n=0: served.order.append(n) or {"n": n})
    served.wire().send("t.pumps")
    assert entered.wait(5)
    wire = served.wire()
    wire.send("t.plain", {"n": 1}, queue_s=30, id="first")
    wire.send("t.plain", {"n": 2}, queue_s=30, id="second")
    assert wire.frame(timeout=0.3) is None, "the refusal is held: it must not be read as #1's"
    release.set()
    first, second = wire.frame(), wire.frame()
    assert first == {"status": "success", "result": {"n": 1}, "id": "first"}
    assert second["refusal"] == "DOORWAY_PIPELINED" and second["id"] == "second"
    assert served.order == ["pumps", 1], "the refused frame did not run"
    assert wire.send("t.plain", {"n": 3}).frame()["result"] == {"n": 3}


def test_a_connection_that_keeps_sending_into_an_unanswered_request_is_closed(served):
    entered, release = pumping_verb(served)
    served.add("plain", lambda: served.order.append("plain") or {})
    served.wire().send("t.pumps")
    assert entered.wait(5)
    wire = served.wire()
    for _ in range(bridge_module.MAX_DEFERRED + 2):
        wire.send("t.plain", queue_s=30)
    assert wire.frame(timeout=5) is None, "closed: no frame, end of stream"
    assert until(lambda: served.bridge.admission()["waiting"] == [])
    release.set()
    assert until(lambda: idle(served.bridge)) and served.order == ["pumps"]


# ------------------------------------------------------------------ the scheduler
def test_a_scheduler_that_raises_rolls_admission_back_and_answers(served):
    failing = {"left": 1}
    real = served.bridge._schedule_coroutine

    def scheduler(coro):
        if failing["left"]:
            failing["left"] -= 1
            raise RuntimeError("no loop today")
        return real(coro)

    served.bridge._schedule_coroutine = scheduler
    served.add("X", exclusive(lambda: served.order.append("X") or {}))
    refusal = served.wire().send("t.X").frame()
    assert refusal["refusal"] == "DOORWAY_SCHEDULER_FAILED" and "no loop today" in refusal["message"]
    # the answer is on the wire BEFORE admission is released, so wait for the release
    assert until(lambda: idle(served.bridge)) and served.order == []
    assert served.wire().send("t.X").frame()["status"] == "success", "admission was not wedged"


def test_a_scheduler_whose_work_cannot_be_observed_is_a_scheduler_failure(served):
    """Kit's scheduler returns a task or a future. One that returns nothing leaves the bridge
    counting a request as running with no way to learn that it ended."""
    served.bridge._schedule_coroutine = lambda coro: coro.close()
    served.add("plain", lambda: served.order.append("plain") or {})
    refusal = served.wire().send("t.plain").frame()
    assert refusal["refusal"] == "DOORWAY_SCHEDULER_FAILED"
    assert "cannot be observed" in refusal["message"] and until(lambda: idle(served.bridge))


def test_a_request_cancelled_before_it_ran_is_answered_and_releases_admission(served):
    blocked, unblock = threading.Event(), threading.Event()
    served.loop.call_soon_threadsafe(lambda: (blocked.set(), unblock.wait(10)))
    assert blocked.wait(5)                           # the loop is busy: nothing can start
    served.add("X", exclusive(lambda: served.order.append("X") or {}))
    served.add("plain", lambda: served.order.append("plain") or {})
    wire = served.wire().send("t.X")
    assert until(lambda: len(served.scheduled) == 1)
    behind = served.wire().send("t.plain", queue_s=30)
    assert until(lambda: served.bridge.admission()["waiting"] == ["t.plain"])
    assert served.scheduled[0].cancel()
    unblock.set()
    assert wire.frame()["refusal"] == "DOORWAY_REQUEST_CANCELLED"
    assert behind.frame()["status"] == "success"
    assert served.order == ["plain"] and until(lambda: idle(served.bridge))


def test_a_request_cancelled_while_it_ran_is_answered_and_releases_admission(served):
    entered = threading.Event()

    async def waits():
        entered.set()
        await asyncio.sleep(60)

    served.add("waits", waits)
    served.add("X", exclusive(lambda: served.order.append("X") or {}))
    wire = served.wire().send("t.waits")
    assert entered.wait(5)
    behind = served.wire().send("t.X", queue_s=30)
    assert until(lambda: served.bridge.admission()["waiting"] == ["t.X"])
    served.scheduled[0].cancel()
    assert wire.frame()["refusal"] == "DOORWAY_REQUEST_CANCELLED"
    assert behind.frame()["status"] == "success" and until(lambda: idle(served.bridge))


def test_an_exclusive_request_that_raises_still_gives_the_loop_back(served):
    @exclusive
    def breaks():
        raise RuntimeError("boom")

    served.add("breaks", breaks)
    served.add("plain", lambda: {"ok": 2})
    assert served.wire().send("t.breaks").frame()["status"] == "error"
    assert served.wire().send("t.plain").frame()["status"] == "success"
    assert until(lambda: idle(served.bridge))


# ------------------------------------------------------------------ in-process callers
def test_an_in_process_caller_queues_through_the_same_admission(served):
    entered, release = pumping_verb(served)
    served.add("plain", lambda: served.order.append("plain") or {"ok": 2})
    served.wire().send("t.pumps")
    assert entered.wait(5)
    answers = []
    served.bridge.submit_local("t.plain", {}, answers.append, queue_s=30)
    assert served.bridge.admission()["waiting"] == ["t.plain"] and answers == []
    refused = []
    served.bridge.submit_local("t.plain", {}, refused.append)          # states no budget
    assert refused and refused[0]["refusal"] == "DOORWAY_BUSY"
    release.set()
    assert until(lambda: answers == [{"status": "success", "result": {"ok": 2}}])
    assert served.order == ["pumps", "plain"]


def test_a_body_that_calls_back_into_the_doorway_from_its_own_exclusive_call_is_refused(served):
    """`run_python` is exclusive. A body that awaits a doorway call in-process would be waiting
    on itself; it gets a typed refusal at once, never a hang."""
    seen = {}

    @exclusive
    async def body():
        seen["inner"] = await served.bridge.dispatch("t.plain", {})
        return {}

    served.add("body", body)
    served.add("plain", lambda: served.order.append("plain") or {})
    assert served.wire().send("t.body").frame()["status"] == "success"
    assert seen["inner"]["refusal"] == "DOORWAY_BUSY" and seen["inner"]["reason"] == "in_process"
    assert seen["inner"]["running"][0]["tool"] == "t.body" and served.order == []


def test_direct_dispatch_on_an_idle_bridge_runs_and_releases(served):
    served.add("plain", lambda: {"ok": 2})
    done = asyncio.run_coroutine_threadsafe(served.bridge.dispatch("t.plain", {}), served.loop)
    assert done.result(5) == {"status": "success", "result": {"ok": 2}}
    assert idle(served.bridge)


# ------------------------------------------------------------------ the marks
def test_run_python_is_exclusive_and_a_mark_survives_a_wrapper(served):
    @exclusive
    def pumps():
        return {}

    @functools.wraps(pumps)
    def scoped(**parameters):
        return pumps(**parameters)

    served.add("pumps", scoped)
    assert served.bridge._is_exclusive({"type": "t.pumps"}) is True
    assert served.bridge._is_exclusive({"type": "run_python"}) is True, \
        "its body is arbitrary and may pump: it always has the loop to itself"
    for ordinary in ("status", "list_tools", "stat_file", "t.unknown"):
        assert served.bridge._is_exclusive({"type": ordinary}) is False


# ------------------------------------------------------------------ the two halves
def _module_owner(bridge, owner, namespace, declared, tool="read"):
    metadata = {"module": owner} if declared is None else {"module": owner, "admission": declared}
    bridge.register_owner(owner, namespace, metadata=metadata).add(
        ToolDefinition(name=tool, description=tool, parameters={}, handler=lambda: {"ok": owner}))


def test_a_tool_module_that_declares_no_admission_contract_is_refused_by_name(served):
    """New doorway, older tool module (a box holding one half of the footprint). Its tools refuse
    naming the module; a module that DID declare, and the builtins, are untouched — so `status`
    still answers and says which module is the old half."""
    _module_owner(served.bridge, "old_lab_tools", "lab", None)
    _module_owner(served.bridge, "new_cell_tools", "cell", bridge_module.ADMISSION_CONTRACT)
    _module_owner(served.bridge, "future_tools", "grasp", bridge_module.ADMISSION_CONTRACT + 1)
    refusal = served.wire().send("lab.read").frame()
    assert refusal["refusal"] == "DOORWAY_ADMISSION_UNDECLARED" and refusal["refused"] is True
    assert refusal["module"] == "old_lab_tools" and "'old_lab_tools'" in refusal["message"]
    assert served.wire().send("cell.read").frame()["result"] == {"ok": "new_cell_tools"}
    assert served.wire().send("grasp.read").frame()["refusal"] == "DOORWAY_ADMISSION_UNDECLARED", \
        "a contract newer than this doorway speaks is not one it can honour either"
    answered = served.wire().send("status").frame()["result"]
    assert answered["doorway_admission"] == bridge_module.ADMISSION_CAPABILITY == "exclusive-v1"
    status = answered["admission"]
    assert status["contract"] == bridge_module.ADMISSION_CONTRACT == 1
    assert status["undeclared_modules"] == ["old_lab_tools"]
    assert status["modules"]["new_cell_tools"] == 1
    done = asyncio.run_coroutine_threadsafe(served.bridge.dispatch("lab.read", {}), served.loop)
    assert done.result(5)["refusal"] == "DOORWAY_ADMISSION_UNDECLARED", "in-process too"


# ------------------------------------------------------------------ the reference client
def test_the_reference_client_states_a_queue_budget_that_keeps_an_execution_reserve():
    assert reference_client.queue_budget(1230.0, execution_reserve_s=990.0) == 239.0
    assert reference_client.queue_budget(300.0) == 149.0      # half reserved, one second margin
    assert reference_client.queue_budget(5.0) == 1.5
    assert reference_client.queue_budget(1.0) == 0.0          # too short to wait at all
    assert reference_client.queue_budget(10.0, execution_reserve_s=60.0) == 0.0


def test_through_the_reference_client_busy_is_a_typed_error_and_a_wait_is_an_answer(served):
    entered, release = pumping_verb(served)
    served.add("plain", lambda: {"ok": 2})
    served.wire().send("t.pumps")
    assert entered.wait(5)
    asked = time.monotonic()
    with pytest.raises(reference_client.BridgeError) as busy:
        reference_client.call("t.plain", port=served.port, timeout=2.4)   # budget 0.2 s
    assert busy.value.details["refusal"] == reference_client.BUSY == "DOORWAY_BUSY"
    assert time.monotonic() - asked < 2.0, "refused well inside the caller's own timeout"
    answers = []
    waiter = threading.Thread(target=lambda: answers.append(
        reference_client.call("t.plain", port=served.port, timeout=30)))
    waiter.start()
    assert until(lambda: served.bridge.admission()["waiting"] == ["t.plain"])
    release.set()
    waiter.join(timeout=10)
    assert answers == [{"ok": 2}]


# ------------------------------------------------------------------ acceptance
def test_ACCEPTANCE_every_request_fired_during_a_long_exclusive_pump_gets_one_terminal_frame(served):
    """Admission under load: N ordinary requests fired during an exclusive pump of
    ACCEPTANCE_PUMP_S (30 s at full length; scaled here, full length by DOORWAY_ACCEPTANCE_PUMP_S=30).
    Each gets EXACTLY ONE terminal frame — the answer, or a typed refusal — and the ones that ran
    ran in arrival order. Odd requests can outwait the pump; even ones state a budget shorter
    than it and are refused while it still holds the loop."""
    count = 24
    entered, release = pumping_verb(served)
    served.add("plain", lambda n: served.order.append(n) or {"n": n})
    holder = served.wire().send("t.pumps")
    assert entered.wait(5)
    began = time.monotonic()
    wires = []
    for n in range(count):
        budget = ACCEPTANCE_PUMP_S * 4 if n % 2 else ACCEPTANCE_PUMP_S / 4
        wires.append(served.wire().send("t.plain", {"n": n}, queue_s=budget, id=n))
        assert until(lambda: len(served.bridge.admission()["waiting"]) >= 1)
        time.sleep(ACCEPTANCE_PUMP_S / 8 / count)
    timer = threading.Timer(max(0.0, ACCEPTANCE_PUMP_S - (time.monotonic() - began)), release.set)
    timer.start()

    refused = [wires[n].frame(timeout=ACCEPTANCE_PUMP_S) for n in range(0, count, 2)]
    assert not release.is_set(), "the refusals arrive DURING the pump, not after it"
    assert [r["refusal"] for r in refused] == ["DOORWAY_BUSY"] * (count // 2)
    assert all(r["running"][0]["tool"] == "t.pumps" for r in refused)
    assert holder.frame(timeout=ACCEPTANCE_PUMP_S * 2 + 10)["status"] == "success"
    answered = [wires[n].frame(timeout=10) for n in range(1, count, 2)]
    assert [a["result"]["n"] for a in answered] == list(range(1, count, 2))
    assert [a["id"] for a in answered] == list(range(1, count, 2))
    assert served.order == ["pumps"] + list(range(1, count, 2)), "arrival order, recorded"
    for wire in wires:
        assert wire.frame(timeout=0.05) is None, "exactly one terminal frame each"
    assert not any("Cannot enter into task" in e or "destroyed" in e for e in served.loop_errors)
    counts = served.bridge.admission_report()["counts"]
    assert counts["refused_busy"] == count // 2 and counts.get("lost", 0) == 0
    assert until(lambda: idle(served.bridge))


def test_NOTHING_WAITS_BY_DEFAULT_every_entrance_without_a_stated_budget_is_refused_at_once(served):
    """The default is fail-fast `DOORWAY_BUSY`, never an unbounded wait: a socket frame with no
    `queue_s`, an in-process `submit_local` with none, and a direct `dispatch` are each answered
    while the pump still holds the loop, and nothing is left in the queue."""
    entered, release = pumping_verb(served)
    served.add("plain", lambda: served.order.append("plain") or {})
    served.wire().send("t.pumps")
    assert entered.wait(5)
    began = time.monotonic()
    by_socket = served.wire().send("t.plain").frame(timeout=2)
    by_local = []
    served.bridge.submit_local("t.plain", {}, by_local.append)
    by_dispatch = asyncio.run(served.bridge.dispatch("t.plain", {}))
    assert time.monotonic() - began < 1.0 and not release.is_set()
    assert [r["refusal"] for r in (by_socket, by_local[0], by_dispatch)] == ["DOORWAY_BUSY"] * 3
    assert [r["reason"] for r in (by_socket, by_local[0], by_dispatch)] == [
        "no_queue_budget", "no_queue_budget", "in_process"]
    assert served.bridge.admission()["waiting"] == []
    release.set()
    assert until(lambda: idle(served.bridge)) and served.order == ["pumps"]
