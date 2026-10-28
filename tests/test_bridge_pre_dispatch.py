"""The bridge's own refusal seam — the one place a policy governs EVERY caller.

The gateway front-ends have hook lists of their own, but each is a TCP client of this socket, so
a gate installed in one governs that agent alone and is walked around by anything else that dials
the port. `McpBridge.dispatch` is the single entrance: a socket request, a panel button and an
in-process caller are the same code path by construction. Policy that must not be evadable belongs
here and only here.

These check the MECHANISM. What gets refused, and why, is the host project's.
"""
import asyncio

import pytest

from omni_kit_mcp.bridge import McpBridge, ToolDefinition


def make_bridge():
    return McpBridge(schedule_coroutine=lambda coro: asyncio.get_event_loop())


def dispatch(bridge, name, params=None):
    return asyncio.run(bridge.dispatch(name, params or {}))


def register(bridge, bare_name, handler):
    """One tool under one owner, named BARE — the registrar prefixes the namespace itself, so
    `run` under namespace `a` becomes the canonical `a.run`."""
    registrar = getattr(bridge, "_test_registrar", None)
    if registrar is None:
        registrar = bridge.register_owner("test_owner", "a")
        bridge._test_registrar = registrar
    registrar.add(ToolDefinition(name=bare_name, description="d", parameters={},
                                 handler=handler))
    return registrar


@pytest.fixture
def bridge():
    made = make_bridge()
    register(made, "run", lambda: {"ran": True})
    return made


# ------------------------------------------------------------------ the seam itself
def test_with_no_hooks_a_call_runs(bridge):
    assert dispatch(bridge, "a.run")["result"] == {"ran": True}


def test_a_hook_returning_a_reason_refuses_the_call(bridge):
    bridge.register_pre_dispatch_hook(lambda name, params: "not while the rig is unhealthy")
    answer = dispatch(bridge, "a.run")
    assert answer["status"] == "error"
    assert answer["refused"] is True
    assert "not while the rig is unhealthy" in answer["message"]


def test_a_refused_call_never_reaches_its_handler(bridge):
    """The point of refusing rather than reporting: the work must not happen."""
    ran = []
    register(bridge, "record", lambda: ran.append(1) or {"ok": True})
    bridge.register_pre_dispatch_hook(lambda name, params: "no")
    dispatch(bridge, "a.record")
    assert ran == [], "the handler ran despite the refusal"


def test_a_hook_returning_None_lets_the_call_through(bridge):
    bridge.register_pre_dispatch_hook(lambda name, params: None)
    assert dispatch(bridge, "a.run")["result"] == {"ran": True}


def test_the_hook_sees_the_name_and_the_arguments(bridge):
    seen = {}

    def watch(name, params):
        seen["name"], seen["params"] = name, dict(params)
        return None

    bridge.register_pre_dispatch_hook(watch)
    dispatch(bridge, "a.run", {"x": 1})
    assert seen == {"name": "a.run", "params": {"x": 1}}


# ------------------------------------------------------------------ what it refuses BEFORE
def test_a_name_the_registry_does_not_hold_is_still_gateable(bridge):
    """A gate is about a NAME. If policy only applied to registered tools, asking for one the
    registry had not loaded yet would be a way around it."""
    bridge.register_pre_dispatch_hook(
        lambda name, params: "refused" if name == "a.never_registered" else None)
    answer = dispatch(bridge, "a.never_registered")
    assert answer.get("refused") is True
    assert "unknown tool" not in answer["message"]


def test_a_refused_call_does_not_refcount_an_owner(bridge):
    """It never entered the tool, so it must not be counted as in flight — a stuck count would
    hold an owner's teardown open forever."""
    bridge.register_pre_dispatch_hook(lambda name, params: "no")
    dispatch(bridge, "a.run")
    assert all(count == 0 for count in bridge._in_flight.values()), bridge._in_flight


# ------------------------------------------------------------------ a hook that cannot decide
def test_a_hook_that_raises_REFUSES_rather_than_letting_the_call_through():
    """The rule the gate itself applies to a state it cannot read, held one layer out.

    A hook was installed to decide whether this call may proceed. One that broke did not decide,
    and treating its failure as consent would make the single moment policy was unavailable the
    single moment everything is permitted.
    """
    made = make_bridge()
    register(made, "run", lambda: {"ran": True})

    def broken(name, params):
        raise RuntimeError("the state was unreadable")

    made.register_pre_dispatch_hook(broken)
    answer = dispatch(made, "a.run")
    assert answer["status"] == "error"
    assert answer["refused"] is True
    assert "has not decided" in answer["message"]
    assert "the state was unreadable" in answer["message"]


def test_the_first_refusal_wins_and_later_hooks_are_not_consulted(bridge):
    consulted = []
    bridge.register_pre_dispatch_hook(
        lambda n, p: consulted.append("first") or "the first reason")
    bridge.register_pre_dispatch_hook(lambda n, p: consulted.append("second"))
    answer = dispatch(bridge, "a.run")
    assert "the first reason" in answer["message"]
    assert consulted == ["first"]


def test_how_many_hooks_are_installed_is_readable(bridge):
    """So a host project can assert it armed exactly one rather than none or three."""
    assert bridge.pre_dispatch_hooks() == 0
    bridge.register_pre_dispatch_hook(lambda n, p: None)
    assert bridge.pre_dispatch_hooks() == 1


def test_a_policy_can_be_cleared_when_what_it_was_about_has_ended(bridge):
    """A policy outliving its run would go on refusing against a state nobody maintains — a
    different wrong answer, not a safer one."""
    bridge.register_pre_dispatch_hook(lambda n, p: "no")
    assert dispatch(bridge, "a.run").get("refused") is True

    assert bridge.clear_pre_dispatch_hooks() == 1
    assert bridge.pre_dispatch_hooks() == 0
    assert dispatch(bridge, "a.run")["result"] == {"ran": True}


def test_clearing_when_nothing_is_installed_is_not_an_error(bridge):
    assert bridge.clear_pre_dispatch_hooks() == 0
