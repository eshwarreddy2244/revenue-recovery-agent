"""
adapters/smart_retry.py
SIMULATED adapter — there is no real "retry a failed payment" endpoint
available in Razorpay test mode, so this models a realistic outcome
using a configurable success probability per failure bucket.

TECHNICAL failures (timeouts, expired tokens, network blips) retry
successfully more often than FINANCIAL failures (insufficient
balance genuinely won't be fixed by retrying immediately).
"""

from __future__ import annotations

import random

# Success probability per diagnosed bucket. Tunable.
SUCCESS_PROBABILITY = {
    "TECHNICAL": 0.55,
    "BEHAVIORAL": 0.25,
    "FINANCIAL": 0.12,
    "UNKNOWN": 0.10,
}

DEFAULT_PROBABILITY = 0.10


def attempt_smart_retry(bucket: str, rng: random.Random | None = None) -> dict:
    """
    Simulate a smart-retry attempt for a given diagnosed bucket.

    Returns:
        {"action": "smart_retry", "success": bool, "simulated": True,
         "probability_used": float}
    """
    rng = rng or random
    probability = SUCCESS_PROBABILITY.get(bucket, DEFAULT_PROBABILITY)
    success = rng.random() < probability
    return {
        "action": "smart_retry",
        "success": success,
        "simulated": True,
        "probability_used": probability,
    }
