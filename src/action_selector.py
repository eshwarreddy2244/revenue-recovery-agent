"""
action_selector.py
Decides WHICH sequence of actions to run for a case, based on
customer LTV tier and diagnosed bucket. This is business logic, not
safety logic — it never overrides or bypasses check_gate. Every step
this module proposes still goes through the same policy_gate.py
check as any other step, with the same touch/discount caps. This
module only changes the *menu* of actions offered, never the rules
that gate them.

Rationale: not every case deserves the same 3-step cadence.
  - HIGH LTV customers are worth a faster, higher-touch resolution —
    skip the impersonal smart_retry step and go straight to a
    payment link, so they're not kept waiting on something that
    rarely fixes FINANCIAL failures anyway.
  - LOW/MEDIUM LTV customers get the standard, cost-efficient
    smart_retry-first cadence, since smart_retry is free-ish and
    resolves a meaningful share of TECHNICAL/BEHAVIORAL cases without
    any customer-facing contact at all.

This is intentionally simple and fully deterministic — the same
inputs always produce the same plan — so it's as testable as
policy_gate.py, just not safety-critical in the same way.
"""

from __future__ import annotations

STANDARD_PLAN = ["smart_retry", "send_payment_link"]
FAST_TRACK_PLAN = ["send_payment_link"]  # skips smart_retry entirely

HIGH_VALUE_TIERS = {"HIGH"}


def get_action_plan(ltv_tier: str | None, bucket: str | None) -> list[str]:
    """
    Return the ordered list of action types to attempt for this case
    (before the final implicit "stop" step, which always runs if
    nothing else recovers the payment).

    HIGH LTV customers fast-track straight to send_payment_link,
    UNLESS the bucket is BEHAVIORAL (checkout abandoned, OTP not
    entered, timed out) — those are much more likely to self-resolve
    on a quick automated retry than a financial failure is, so even
    high-value customers get the cheap retry first there.
    """
    if ltv_tier in HIGH_VALUE_TIERS and bucket != "BEHAVIORAL":
        return list(FAST_TRACK_PLAN)
    return list(STANDARD_PLAN)


def explain_plan(ltv_tier: str | None, bucket: str | None) -> str:
    """Human-readable one-liner for why this plan was chosen — used in the audit trail / dashboard."""
    plan = get_action_plan(ltv_tier, bucket)
    if plan == FAST_TRACK_PLAN:
        return f"Fast-tracked to payment link: {ltv_tier} LTV tier, {bucket} bucket skips smart_retry."
    return f"Standard cadence: smart_retry then payment link ({ltv_tier or 'unknown'} tier, {bucket or 'unknown'} bucket)."
