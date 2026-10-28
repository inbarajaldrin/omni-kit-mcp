"""gate.py — the pure deny-gate primitive, and every condition form it admits.

The gate is a pure function so that this is its test surface: no server, no bridge, no
socket. What a declared policy file means is decided here.
"""

import json

import pytest

from kit_mcp import gate


def _hooks(*entries, event="PreToolUse"):
    return {"hooks": {event: list(entries)}}


# ---------------------------------------------------------------- matching
def test_no_hooks_denies_nothing():
    assert gate.evaluate({}, "anything", {}) is None
    assert gate.evaluate(_hooks(), "anything", {}) is None


def test_a_matcher_hit_with_no_condition_is_a_blanket_deny():
    hooks = _hooks({"matcher": "run_python", "reason": "no arbitrary code here"})
    assert gate.evaluate(hooks, "run_python", {}) == "no arbitrary code here"
    assert gate.evaluate(hooks, "lab.resolve", {}) is None


def test_a_blanket_deny_without_a_reason_still_says_which_tool():
    hooks = _hooks({"matcher": "run_python"})
    assert "run_python" in gate.evaluate(hooks, "run_python", {})


@pytest.mark.parametrize("matcher", ["*", "", None])
def test_a_wildcard_matcher_hits_every_tool(matcher):
    hooks = _hooks({"matcher": matcher, "reason": "sealed"})
    assert gate.evaluate(hooks, "whatever", {}) == "sealed"


def test_a_matcher_may_be_a_list_of_tools():
    """The deny set a health gate needs is several named calls under one condition, and
    writing the condition once is what keeps the set readable as a set."""
    hooks = _hooks({"matcher": ["lab.set", "lab.decouple"], "reason": "no robot writes"})
    assert gate.evaluate(hooks, "lab.set", {}) == "no robot writes"
    assert gate.evaluate(hooks, "lab.decouple", {}) == "no robot writes"
    assert gate.evaluate(hooks, "lab.resolve", {}) is None


def test_only_the_named_event_is_consulted():
    hooks = _hooks({"matcher": "*", "reason": "after"}, event="PostToolUse")
    assert gate.evaluate(hooks, "anything", {}) is None
    assert gate.evaluate(hooks, "anything", {}, event="PostToolUse") == "after"


# ---------------------------------------------------------------- deny_if: the arg condition
def test_deny_if_fires_only_on_the_matching_argument():
    hooks = _hooks({"matcher": "lab.set", "deny_if": {"arg": "mode", "equals": "raw"},
                    "reason": "raw writes are refused"})
    assert gate.evaluate(hooks, "lab.set", {"mode": "raw"}) == "raw writes are refused"
    assert gate.evaluate(hooks, "lab.set", {"mode": "declared"}) is None
    assert gate.evaluate(hooks, "lab.set", {}) is None


def test_deny_if_can_test_for_inequality():
    hooks = _hooks({"matcher": "lab.set", "deny_if": {"arg": "mode", "not_equals": "declared"},
                    "reason": "only declared writes"})
    assert gate.evaluate(hooks, "lab.set", {"mode": "raw"}) == "only declared writes"
    assert gate.evaluate(hooks, "lab.set", {"mode": "declared"}) is None


# ---------------------------------------------------------------- deny_while: the state condition
def test_deny_while_holds_the_gate_open_only_while_the_state_says_so():
    hooks = _hooks({"matcher": "lab.set",
                    "deny_while": {"state": "robot_health", "not_equals": "ok"},
                    "reason": "the rig is not healthy"})
    assert gate.evaluate(hooks, "lab.set", {}, {"robot_health": "ok"}) is None
    assert gate.evaluate(hooks, "lab.set", {}, {"robot_health": "wedged"}) == "the rig is not healthy"


def test_a_state_the_caller_did_not_supply_denies_rather_than_passes():
    """UNKNOWN is never SAFE. A gate that cannot read the condition it exists to enforce has
    to refuse — a rig whose health nobody can see is exactly the rig not to drive."""
    hooks = _hooks({"matcher": "lab.set",
                    "deny_while": {"state": "robot_health", "not_equals": "ok"},
                    "reason": "the rig is not healthy"})
    denied = gate.evaluate(hooks, "lab.set", {}, {})
    assert denied is not None
    assert "robot_health" in denied and "unknown" in denied.lower()


def test_a_state_condition_and_an_arg_condition_must_both_hold():
    hooks = _hooks({"matcher": "lab.set",
                    "deny_while": {"state": "robot_health", "not_equals": "ok"},
                    "deny_if": {"arg": "target", "equals": "arm"},
                    "reason": "no arm writes on a bad rig"})
    bad = {"robot_health": "wedged"}
    assert gate.evaluate(hooks, "lab.set", {"target": "arm"}, bad) is not None
    assert gate.evaluate(hooks, "lab.set", {"target": "objects"}, bad) is None
    assert gate.evaluate(hooks, "lab.set", {"target": "arm"}, {"robot_health": "ok"}) is None


# ---------------------------------------------------------------- unless: the exception
def test_unless_carves_the_one_move_that_stays_reachable():
    """The health gate's whole shape: robot writes are refused while the rig is bad, EXCEPT
    the write that fixes it. Without the exception the agent is gated into a corner with no
    move that clears the gate."""
    hooks = _hooks({"matcher": "lab.set",
                    "deny_while": {"state": "robot_health", "not_equals": "ok"},
                    "unless": {"arg": "robot", "equals": "home"},
                    "reason": "robot writes are refused while the rig is unhealthy"})
    bad = {"robot_health": "wedged"}
    assert gate.evaluate(hooks, "lab.set", {"robot": [0.1, 0.2]}, bad) is not None
    assert gate.evaluate(hooks, "lab.set", {"robot": "home"}, bad) is None
    assert gate.evaluate(hooks, "lab.set", {"robot": "home"}, {"robot_health": "ok"}) is None


def test_deny_if_not_equals_null_means_deny_when_the_argument_was_sent_at_all():
    """How a gate covers a whole target rather than one value of it: an absent argument does
    not satisfy an argument condition, so this fires exactly when the caller sent the thing."""
    hooks = _hooks({"matcher": "lab.set", "deny_if": {"arg": "gripper", "not_equals": None},
                    "reason": "no gripper writes"})
    assert gate.evaluate(hooks, "lab.set", {"gripper": 0.04}) == "no gripper writes"
    assert gate.evaluate(hooks, "lab.set", {"gripper": 0.0}) == "no gripper writes"
    assert gate.evaluate(hooks, "lab.set", {"objects": {}}) is None


def test_unless_applies_to_a_blanket_deny_too():
    hooks = _hooks({"matcher": "lab.set", "unless": {"arg": "robot", "equals": "home"},
                    "reason": "sealed"})
    assert gate.evaluate(hooks, "lab.set", {"robot": "home"}) is None
    assert gate.evaluate(hooks, "lab.set", {"robot": "q"}) == "sealed"


def test_unless_can_test_for_inequality():
    hooks = _hooks({"matcher": "lab.set", "unless": {"arg": "scope", "not_equals": "robot"},
                    "reason": "robot scope only"})
    assert gate.evaluate(hooks, "lab.set", {"scope": "objects"}) is None
    assert gate.evaluate(hooks, "lab.set", {"scope": "robot"}) == "robot scope only"


# ---------------------------------------------------------------- ordering
def test_the_first_entry_that_denies_is_the_answer():
    hooks = _hooks(
        {"matcher": "lab.set", "deny_if": {"arg": "target", "equals": "arm"}, "reason": "first"},
        {"matcher": "lab.set", "reason": "second"})
    assert gate.evaluate(hooks, "lab.set", {"target": "arm"}) == "first"
    assert gate.evaluate(hooks, "lab.set", {"target": "objects"}) == "second"


# ---------------------------------------------------------------- loading
def test_an_absent_file_means_no_gates(tmp_path):
    assert gate.load_hooks(str(tmp_path / "nothing.json")) == {}


def test_a_declared_file_loads(tmp_path):
    path = tmp_path / "hooks.json"
    path.write_text(json.dumps(_hooks({"matcher": "run_python", "reason": "no"})))
    assert gate.evaluate(gate.load_hooks(str(path)), "run_python", {}) == "no"


# ---------------------------------------------------------------- malformed policy
@pytest.mark.parametrize("entry,fragment", [
    ({"matcher": "t", "deny_while": {"not_equals": "ok"}}, "state"),
    ({"matcher": "t", "deny_while": {"state": "s"}}, "equals"),
    ({"matcher": "t", "deny_if": {"equals": "x"}}, "arg"),
    ({"matcher": "t", "deny_if": {"arg": "a"}}, "equals"),
    ({"matcher": "t", "unless": {"arg": "a", "same_as": "b"}}, "equals"),
    ({"matcher": "t", "deny_while": {"state": "s", "equals": 1, "not_equals": 2}}, "one of"),
    ({"matcher": 7}, "matcher"),
    ("not an entry", "object"),
])
def test_a_malformed_policy_is_refused_when_it_is_loaded(tmp_path, entry, fragment):
    """At LOAD, not at the one call it would have blocked. A policy file discovered to be
    malformed mid-run has already been silently letting calls through."""
    path = tmp_path / "hooks.json"
    path.write_text(json.dumps(_hooks(entry)))
    with pytest.raises(gate.GatePolicyError) as caught:
        gate.load_hooks(str(path))
    assert fragment in str(caught.value)


def test_a_file_that_is_not_json_is_refused_by_name(tmp_path):
    path = tmp_path / "hooks.json"
    path.write_text("{not json")
    with pytest.raises(gate.GatePolicyError) as caught:
        gate.load_hooks(str(path))
    assert str(path) in str(caught.value)


def test_a_well_formed_policy_validates_clean(tmp_path):
    path = tmp_path / "hooks.json"
    path.write_text(json.dumps(_hooks(
        {"matcher": ["a", "b"],
         "deny_while": {"state": "robot_health", "not_equals": "ok"},
         "unless": {"arg": "robot", "equals": "home"},
         "reason": "r"})))
    assert gate.load_hooks(str(path))["hooks"]["PreToolUse"]


# ---------------------------------------------------------------- the roster check
def test_matchers_lists_every_named_tool_once():
    hooks = _hooks({"matcher": ["b.two", "a.one"]}, {"matcher": "a.one"}, {"matcher": "*"})
    assert gate.matchers(hooks) == ("a.one", "b.two")


def test_matchers_of_an_empty_policy_is_empty():
    assert gate.matchers({}) == ()


def test_unknown_matchers_names_the_gates_that_gate_nothing():
    """The quiet failure: a matcher naming a tool that does not exist denies nothing and tells
    nobody, so the policy loads and every call passes while the gate reports itself as working.
    Renaming a tool is enough to do it."""
    hooks = _hooks({"matcher": ["lab.set", "lab.renamed_away"]}, {"matcher": "run_python"})
    roster = ["lab.set", "run_python", "lab.resolve"]
    assert gate.unknown_matchers(hooks, roster) == ("lab.renamed_away",)


def test_a_policy_whose_names_all_exist_has_nothing_unknown():
    hooks = _hooks({"matcher": "lab.set"}, {"matcher": "*"})
    assert gate.unknown_matchers(hooks, ["lab.set"]) == ()


def test_an_empty_roster_makes_every_named_gate_unknown():
    """Checking a policy before discovery has filled the roster would otherwise clear every
    name in it — the answer has to be loud, not empty."""
    hooks = _hooks({"matcher": "lab.set"})
    assert gate.unknown_matchers(hooks, []) == ("lab.set",)
