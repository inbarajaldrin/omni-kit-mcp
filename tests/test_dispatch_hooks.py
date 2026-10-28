"""The GATEWAY's two hook lists — every call through THIS front-end crosses both.

Scope, because it is easy to read more into these than they cover: this module is the
LLM-facing FastMCP server, a TCP client of the Kit-side bridge. One bridge socket serves many
of these concurrently, so a hook installed here governs the calls of the agent this front-end
serves — and nothing else. Anything that dials the bridge port directly does not pass through
here at all.

Policy that must govern EVERY caller belongs at the bridge's own dispatch, which is the single
entrance all paths land on; `tests/test_bridge_pre_dispatch.py` covers that seam. These check
the MECHANISM of this one: that both lists run, in order, on every outcome, and that a hook
that raises degrades to a reported error rather than taking the dispatch down with it.
"""

import pytest

from kit_mcp import server


@pytest.fixture(autouse=True)
def empty_hook_lists(monkeypatch):
    monkeypatch.setattr(server, "_PRE_DISPATCH_HOOKS", [])
    monkeypatch.setattr(server, "_POST_DISPATCH_HOOKS", [])


class _FakeBridge:
    """Stands in for the Kit-side bridge: records what it was asked, answers or raises."""

    def __init__(self, answer=None, raises=None):
        self.answer = answer if answer is not None else {"ok": True}
        self.raises = raises
        self.calls = []

    def call(self, canonical, params=None):
        self.calls.append((canonical, dict(params or {})))
        if self.raises is not None:
            raise self.raises
        return self.answer


def _bridge(monkeypatch, **kwargs):
    fake = _FakeBridge(**kwargs)
    monkeypatch.setattr(server, "get_client", lambda: fake)
    return fake


# ---------------------------------------------------------------- pre-dispatch
def test_a_pre_hook_refusal_blocks_the_call_before_the_bridge_sees_it(monkeypatch):
    fake = _bridge(monkeypatch)
    server.register_pre_dispatch_hook(lambda name, params: "denied: the rig is unhealthy")
    result = server._tool_implementation("lab.set", {"robot": "q"})
    assert result == "denied: the rig is unhealthy"
    assert fake.calls == []


def test_a_pre_hook_may_mutate_the_params_it_lets_through(monkeypatch):
    fake = _bridge(monkeypatch)

    def stamp(name, params):
        params["audited"] = True
        return None

    server.register_pre_dispatch_hook(stamp)
    server._tool_implementation("lab.resolve", {"ref": "world"})
    assert fake.calls == [("lab.resolve", {"ref": "world", "audited": True})]


def test_a_raising_pre_hook_is_reported_and_the_call_does_not_proceed(monkeypatch):
    fake = _bridge(monkeypatch)

    def broken(name, params):
        raise RuntimeError("policy file went missing")

    server.register_pre_dispatch_hook(broken)
    result = server._tool_implementation("lab.resolve", {})
    assert "broken" in result and "policy file went missing" in result
    assert fake.calls == []


# ---------------------------------------------------------------- post-dispatch
def test_a_post_hook_sees_the_call_and_may_annotate_the_result(monkeypatch):
    _bridge(monkeypatch, answer={"pose": [0, 0, 1]})
    seen = []

    def annotate(name, params, result):
        seen.append((name, params, result))
        return result + "\n\nNOTE: the rig is unhealthy; recover before driving it."

    server.register_post_dispatch_hook(annotate)
    result = server._tool_implementation("lab.resolve", {"ref": "world"})
    assert seen[0][0] == "lab.resolve"
    assert seen[0][1] == {"ref": "world"}
    assert "pose" in result
    assert result.endswith("recover before driving it.")


def test_a_post_hook_returning_nothing_leaves_the_result_alone(monkeypatch):
    _bridge(monkeypatch, answer={"pose": [0, 0, 1]})
    server.register_post_dispatch_hook(lambda name, params, result: None)
    assert "pose" in server._tool_implementation("lab.resolve", {})


def test_post_hooks_run_in_registration_order_and_each_sees_the_last_one_s_result(monkeypatch):
    _bridge(monkeypatch, answer={})
    server.register_post_dispatch_hook(lambda n, p, r: r + " |first")
    server.register_post_dispatch_hook(lambda n, p, r: r + " |second")
    result = server._tool_implementation("lab.resolve", {})
    assert result.endswith(" |first |second")


def test_a_post_hook_runs_when_the_bridge_errored(monkeypatch):
    """The annotation matters MOST here. A call that just failed on a wedged rig is exactly
    when the agent needs to be told the rig is wedged."""
    _bridge(monkeypatch, raises=server.BridgeError("the articulation is in a bad state"))
    server.register_post_dispatch_hook(lambda n, p, r: r + " [health: bad]")
    result = server._tool_implementation("lab.set", {"robot": "q"})
    assert result.startswith("Error:")
    assert result.endswith(" [health: bad]")


def test_a_post_hook_runs_when_a_pre_hook_refused(monkeypatch):
    """A refusal is an outcome. Post hooks see every outcome, which is what lets one of them
    be the single place the agent is told about health."""
    _bridge(monkeypatch)
    server.register_pre_dispatch_hook(lambda n, p: "denied")
    server.register_post_dispatch_hook(lambda n, p, r: r + " [health: bad]")
    assert server._tool_implementation("lab.set", {}) == "denied [health: bad]"


def test_a_raising_post_hook_does_not_lose_the_result(monkeypatch):
    """A broken annotation must not cost the caller the answer the bridge already gave."""
    _bridge(monkeypatch, answer={"pose": [1, 2, 3]})

    def broken(name, params, result):
        raise RuntimeError("health topic not up")

    server.register_post_dispatch_hook(broken)
    server.register_post_dispatch_hook(lambda n, p, r: r + " |survived")
    result = server._tool_implementation("lab.resolve", {})
    assert "pose" in result
    assert result.endswith(" |survived")


def test_both_lists_run_on_one_call_in_order(monkeypatch):
    fake = _bridge(monkeypatch, answer={"ok": True})
    order = []
    server.register_pre_dispatch_hook(lambda n, p: order.append("pre") or None)
    server.register_post_dispatch_hook(lambda n, p, r: order.append("post") or None)
    server._tool_implementation("lab.resolve", {})
    assert order == ["pre", "post"]
    assert len(fake.calls) == 1


def test_unset_optional_params_are_dropped_before_either_hook_sees_them(monkeypatch):
    """The hooks judge the call the bridge will actually receive, not the signature's
    placeholders — a gate conditioned on an argument nobody passed would fire on a default."""
    _bridge(monkeypatch)
    seen = {}
    server.register_pre_dispatch_hook(lambda n, p: seen.update(pre=dict(p)) or None)
    server.register_post_dispatch_hook(lambda n, p, r: seen.update(post=dict(p)) or None)
    server._tool_implementation("lab.set", {"robot": "home", "gripper": None})
    assert seen["pre"] == {"robot": "home"}
    assert seen["post"] == {"robot": "home"}


def test_the_registered_roster_is_what_a_policy_can_be_checked_against(monkeypatch):
    monkeypatch.setattr(server, "_NAME_MAP", {"lab__set": "lab.set", "run_python": "run_python"})
    assert server.registered_tools() == ["lab.set", "run_python"]


def test_the_roster_is_empty_before_anything_is_discovered(monkeypatch):
    monkeypatch.setattr(server, "_NAME_MAP", {})
    assert server.registered_tools() == []
