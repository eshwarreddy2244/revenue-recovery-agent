"""
policy_gate.py
The safety gate. This module is the one place in the codebase that
decides whether a recovery action is allowed to actually happen.

Hard requirements:
  - Pure function. No I/O, no network, no LLM, no randomness.
  - Deterministic: same inputs -> same output, always.
  - Fails LOUD on incomplete input (missing prior_touch_count) rather
    than silently defaulting it to 0 — a safety check that guesses
    is worse than one that refuses to run.

Rules are checked in order; hard halts (opt-out, dispute) always
take priority over soft constraints (touch cap, discount cap), even
if both would independently trigger a block.
"""

from __future__ import annotations

from typing import Any


class GateInputError(Exception):
    """Raised when the gate is given diagnosis input it cannot safely evaluate."""


TOUCH_CAP = 3
DISCOUNT_CAP_PCT = 5


def check_gate(
    event: dict,
    diagnosis: dict,
    proposed_action: dict,
    touch_cap: int = TOUCH_CAP,
    discount_cap_pct: float = DISCOUNT_CAP_PCT,
) -> dict:
    """
    Evaluate whether `proposed_action` may proceed for this event.

    Args:
        event: the raw event dict (needs opt_out, dispute_raised).
        diagnosis: dict form of a DiagnosisResult — must contain the
            key "prior_touch_count".
        proposed_action: dict describing the action under
            consideration, e.g. {"type": "send_payment_link",
            "discount_pct": 0}.
        touch_cap: maximum touches allowed before TOUCH_CAP_EXCEEDED
            fires. Defaults to the module constant TOUCH_CAP. Exposed
            as a parameter (not a global you'd monkeypatch) so
            callers — e.g. a "what-if" policy simulator — can compare
            outcomes under different caps without ever mutating
            shared state, which would break determinism for any
            concurrent caller.
        discount_cap_pct: maximum discount_pct allowed before
            DISCOUNT_CAP_EXCEEDED fires. Defaults to the module
            constant DISCOUNT_CAP_PCT. Same rationale as touch_cap.

    Returns:
        {"decision": "ALLOW" | "BLOCK", "reason": str,
         "touch_count_after": int}

    Raises:
        GateInputError: if diagnosis is missing "prior_touch_count",
            or if touch_cap/discount_cap_pct are not valid numbers.
    """
    if not isinstance(event, dict):
        raise GateInputError("event must be a dict — refusing to evaluate malformed input")
    if not isinstance(diagnosis, dict):
        raise GateInputError("diagnosis must be a dict — refusing to evaluate malformed input")
    if not isinstance(proposed_action, dict):
        raise GateInputError("proposed_action must be a dict — refusing to evaluate malformed input")
    if isinstance(touch_cap, bool) or not isinstance(touch_cap, (int, float)) or touch_cap < 0:
        raise GateInputError(f"touch_cap must be a non-negative number, got {touch_cap!r}")
    if isinstance(discount_cap_pct, bool) or not isinstance(discount_cap_pct, (int, float)) or discount_cap_pct < 0:
        raise GateInputError(f"discount_cap_pct must be a non-negative number, got {discount_cap_pct!r}")

    if "prior_touch_count" not in diagnosis:
        raise GateInputError(
            "diagnosis is missing required key 'prior_touch_count' — "
            "refusing to guess a value for a safety-critical check"
        )

    prior_touch_count = diagnosis["prior_touch_count"]
    if prior_touch_count is None or not isinstance(prior_touch_count, (int, float)) or isinstance(prior_touch_count, bool):
        raise GateInputError(
            "diagnosis['prior_touch_count'] is missing/invalid — "
            "refusing to guess a value for a safety-critical check"
        )

    touch_count_after = int(prior_touch_count) + 1

    # a. opt_out -> always BLOCK (hard halt, checked first).
    # Any truthy value blocks, not just a strict `True` — a
    # safety gate should err toward blocking on ambiguous/malformed
    # truthiness (e.g. opt_out: "yes") rather than silently allowing
    # contact because the value wasn't exactly the boolean True.
    if event.get("opt_out"):
        return _result("BLOCK", "OPT_OUT", touch_count_after)

    # b. dispute_raised -> always BLOCK (hard halt). Same truthy rule.
    if event.get("dispute_raised"):
        return _result("BLOCK", "DISPUTE_RAISED", touch_count_after)

    # c. touch cap (soft constraint)
    if prior_touch_count >= touch_cap:
        return _result("BLOCK", "TOUCH_CAP_EXCEEDED", touch_count_after)

    # d. discount cap (soft constraint)
    discount_pct = proposed_action.get("discount_pct", 0)
    if discount_pct is None:
        discount_pct = 0
    if isinstance(discount_pct, bool) or not isinstance(discount_pct, (int, float)):
        raise GateInputError(
            f"proposed_action['discount_pct'] is not a number ({discount_pct!r}) — "
            "refusing to guess a value for a safety-critical check"
        )
    if discount_pct > discount_cap_pct:
        return _result("BLOCK", "DISCOUNT_CAP_EXCEEDED", touch_count_after)

    # e. otherwise ALLOW
    return _result("ALLOW", "OK", touch_count_after)


def _result(decision: str, reason: str, touch_count_after: int) -> dict[str, Any]:
    return {
        "decision": decision,
        "reason": reason,
        "touch_count_after": touch_count_after,
    }
