"""
adapters/confirmation.py
SIMULATED -- there is no real "did the customer actually pay" webhook
to call in Razorpay TEST mode for a payment link nobody is going to
click during a demo. This models whether an issued payment link
actually gets PAID, as a separate event from whether the link itself
was successfully CREATED.

This is what closes the gap the README has flagged from the start:
elsewhere in this codebase, "recovered" at the send_payment_link step
used to mean "we successfully created a link" -- a technical success,
not a business outcome. This module adds the missing second half:
given a link was issued, did the customer actually complete payment
on it? See sequencer.py's send_payment_link runner, which now calls
this after a successful link creation, and report.py's
links_issued_count / payment_confirmed split.

The base rates below are hand-tuned, not fit to a real dataset -- be
honest about that if asked. They ARE chosen to land in the range
published dunning/involuntary-churn recovery studies report for
payment-recovery campaigns overall (commonly cited industry figures
from recovery/dunning vendors put blended recovery somewhere in the
roughly 20-40% band across all failure causes), with the SPREAD across
buckets -- technical/behavioral recovering meaningfully better than
financial -- reflecting the same reasoning those studies give: a
transient failure resolves once you ask again, a root-cause financial
one usually doesn't. Treat the overall shape (bucket ordering, the
existence of a real technical/business gap) as the defensible part;
treat the exact decimal values as a placeholder for what would, in a
real deployment, get replaced with rates measured from actual payment
completions.
"""

from __future__ import annotations

import random

# Base confirmation rate per diagnosed bucket: how likely a customer
# who receives a payment link actually pays on it, given WHY their
# original payment failed. A behavioral or technical failure (cart
# abandonment, a network blip, an expired token) is usually resolved
# once the customer is nudged with a fresh link. A financial failure
# (insufficient balance, card limit exceeded) often isn't -- the
# underlying problem that caused the ORIGINAL failure is frequently
# still there when the link arrives, so the link alone doesn't fix it.
BASE_CONFIRMATION_RATE = {
    "TECHNICAL": 0.60,
    "BEHAVIORAL": 0.55,
    "FINANCIAL": 0.35,
    "UNKNOWN": 0.30,
}
DEFAULT_BASE_RATE = 0.30

# LTV-tier adjustment: modeled as more engaged / higher-value
# customers being somewhat more likely to follow through and
# actually complete the payment once a link reaches them.
_TIER_ADJUSTMENT = {"HIGH": 0.10, "MEDIUM": 0.0, "LOW": -0.05}


def simulate_confirmation(
    bucket: str | None, ltv_tier: str | None, rng: random.Random | None = None
) -> dict:
    """
    Simulate whether a customer actually completes payment on an
    issued payment link.

    Returns:
        {"confirmed": bool, "probability_used": float}
    """
    rng = rng or random
    base = BASE_CONFIRMATION_RATE.get(bucket, DEFAULT_BASE_RATE)
    adjustment = _TIER_ADJUSTMENT.get(ltv_tier, 0.0)
    probability = min(1.0, max(0.0, base + adjustment))
    confirmed = rng.random() < probability
    return {"confirmed": confirmed, "probability_used": probability}
