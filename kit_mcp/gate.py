"""Deny-gate evaluation: a declared policy file, and a pure function that reads it.

MECHANISM ONLY. This module knows how to say "deny this tool under these conditions"; it
knows nothing about which tools, which conditions, or why. Those are the host project's,
and they live in the host project's files — the same rule the server's own header states.

A policy file declares gates in the Claude Code shape: an event maps to a list of entries,
each naming which tools it covers and when it fires.

    {"hooks": {"PreToolUse": [
        {"matcher": ["ns.write_a", "ns.write_b"],
         "deny_while": {"state": "some_state", "not_equals": "ok"},
         "unless": {"arg": "target", "equals": "safe_value"},
         "reason": "..."}
    ]}}

An entry denies when its matcher hits AND every condition it declares holds AND its
exception does not fire. Three condition forms, each optional:

  - `deny_while` tests declared STATE the caller supplies — the world outside this call.
  - `deny_if` tests an ARGUMENT of the call itself.
  - `unless` tests an argument too, and carves out the one call that stays reachable. A
    gate with no exception can seal an agent into a corner with no move that clears it.

An entry with none of them denies on a matcher hit alone, which is what makes a tool that
carries no arguments gateable at all.

`evaluate` is pure — hooks, a tool name, the args, the state — which makes it the test
surface: the policy's meaning is decided without a server, a bridge or a socket.

Two rules the shape depends on:

  A STATE THE CALLER DID NOT SUPPLY DENIES. A gate that cannot read the condition it exists
  to enforce must refuse; passing would make an unreadable world look like a safe one.

  A MALFORMED POLICY IS REFUSED WHEN IT IS LOADED, not on the one call it would have
  blocked. A file discovered to be malformed mid-run has already been letting calls through.
"""

import json
from pathlib import Path

DEFAULT_EVENT = "PreToolUse"
_PREDICATES = ("equals", "not_equals")
_CONDITIONS = {"deny_while": "state", "deny_if": "arg", "unless": "arg"}


class GatePolicyError(ValueError):
    """A policy file that does not declare gates this can evaluate."""


def load_hooks(path):
    """Read and validate a policy file. An absent file means no gates.

    Validation is not optional and not deferred: a policy that cannot be read is a policy
    that is not being enforced, and the run should stop at the file rather than at the call.
    """
    location = Path(path)
    if not location.exists():
        return {}
    try:
        hooks = json.loads(location.read_text())
    except (OSError, ValueError) as error:
        raise GatePolicyError(f"cannot read the gate policy at {location}: {error}") from None
    validate(hooks, str(location))
    return hooks


def validate(hooks, where="the gate policy"):
    """Refuse anything in `hooks` this cannot evaluate, naming the entry and the problem."""
    events = hooks.get("hooks") or {}
    if not isinstance(events, dict):
        raise GatePolicyError(f"{where}: `hooks` must be an object of event name to entries")
    for event, entries in events.items():
        if not isinstance(entries, list):
            raise GatePolicyError(f"{where}: event {event!r} must hold a list of entries")
        for index, entry in enumerate(entries):
            _validate_entry(entry, f"{where}: {event}[{index}]")


def _validate_entry(entry, where):
    if not isinstance(entry, dict):
        raise GatePolicyError(f"{where} must be an object, not {type(entry).__name__}")
    matcher = entry.get("matcher")
    if not (matcher is None or isinstance(matcher, str)
            or (isinstance(matcher, list) and all(isinstance(m, str) for m in matcher))):
        raise GatePolicyError(
            f"{where}: `matcher` must be a tool name, a list of them, or a wildcard")
    for key, subject in _CONDITIONS.items():
        condition = entry.get(key)
        if condition is None:
            continue
        if not isinstance(condition, dict):
            raise GatePolicyError(f"{where}: `{key}` must be an object")
        if not isinstance(condition.get(subject), str):
            raise GatePolicyError(f"{where}: `{key}` must name the {subject} it tests, "
                                  f"as `{subject}`")
        present = [name for name in _PREDICATES if name in condition]
        if len(present) != 1:
            raise GatePolicyError(
                f"{where}: `{key}` must carry exactly one of {list(_PREDICATES)}; "
                f"found {present or 'none'}")


def matchers(hooks):
    """Every tool name this policy names, deduplicated and sorted. Wildcards are not names."""
    named = set()
    for entries in (hooks.get("hooks") or {}).values():
        for entry in entries:
            matcher = entry.get("matcher")
            if isinstance(matcher, str) and matcher not in ("*", ""):
                named.add(matcher)
            elif isinstance(matcher, list):
                named.update(matcher)
    return tuple(sorted(named))


def unknown_matchers(hooks, roster):
    """The names this policy gates that the given tool roster does not serve.

    THE QUIET FAILURE THIS EXISTS TO CATCH. A matcher naming a tool that does not exist denies
    nothing and says so to nobody: the policy loads, every call passes, and the gate reports
    itself as working while enforcing nothing. Renaming a tool is enough to do it.

    So a caller that knows what is actually registered should ask this and refuse a policy that
    gates thin air. It is a question, not an answer — which roster is authoritative, and whether
    a stale name is worth stopping for, are the host's to decide.
    """
    return tuple(name for name in matchers(hooks) if name not in set(roster))


def evaluate(hooks, tool, args=None, state=None, event=DEFAULT_EVENT):
    """The deny reason for this call, or None to let it through.

    `args` are the call's arguments as the bridge will receive them. `state` is the declared
    world the policy may condition on, as a mapping of name to value; a policy that names
    state the caller did not supply denies.
    """
    args = args or {}
    state = state or {}
    for entry in (hooks.get("hooks") or {}).get(event, []):
        if not _matches(entry.get("matcher"), tool):
            continue
        exception = entry.get("unless")
        if exception is not None and _holds(exception, args, present=True):
            continue
        unreadable = _unreadable_state(entry, state)
        if unreadable is not None:
            return unreadable
        if not _conditions_hold(entry, args, state):
            continue
        return entry.get("reason") or f"{tool} is denied by the gate"
    return None


def _matches(matcher, tool):
    """A matcher hits on the tool name, on membership of a list of them, or on a wildcard."""
    if matcher in ("*", "", None):
        return True
    if isinstance(matcher, list):
        return tool in matcher
    return matcher == tool


def _unreadable_state(entry, state):
    condition = entry.get("deny_while")
    if condition is None or condition["state"] in state:
        return None
    return (f"{condition['state']!r} is unknown here, so this gate cannot be evaluated and "
            f"the call is refused rather than assumed safe")


def _conditions_hold(entry, args, state):
    if "deny_while" in entry and not _holds(entry["deny_while"], state, key="state",
                                            present=True):
        return False
    if "deny_if" in entry and not _holds(entry["deny_if"], args, present=False):
        return False
    return True


def _holds(condition, values, key="arg", present=False):
    """Whether a condition's predicate holds against `values`.

    `present` says what an absent name means. A state condition is only reached once the
    state is known to be there. An argument condition against an argument nobody passed does
    NOT hold — a gate conditioned on a value is about that value being sent, and firing on
    its absence would deny the calls it was written to allow.
    """
    name = condition[key]
    if name not in values and not present:
        return False
    value = values.get(name)
    if "equals" in condition:
        return value == condition["equals"]
    return value != condition["not_equals"]
