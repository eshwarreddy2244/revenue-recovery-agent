"""
generate_data.py
Generates synthetic failed-payment events for the AI Revenue Recovery Agent.

Produces 150-200 events with a realistic bucket distribution
(~50% FINANCIAL, ~30% BEHAVIORAL, ~20% TECHNICAL) plus 7 deliberate
edge-case rows that exercise the validation logic downstream in
diagnose.py:

  1. missing customer_id
  2. negative amount
  3. unknown / unmapped failure_reason_code
  4. duplicate payment_id (two rows share an id)
  5. dispute_raised=True at ingestion
  6. opt_out=True at ingestion
  7. malformed / unexpected data type in a field (amount as a string)

Run directly to (re)generate data/synthetic_events.json:
    python -m src.generate_data
"""

from __future__ import annotations

import json
import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --- Failure reason codes, grouped by the bucket diagnose.py will assign ---

FINANCIAL_CODES = [
    "INSUFFICIENT_BALANCE",
    "CARD_LIMIT_EXCEEDED",
    "BANK_DECLINED_INSUFFICIENT_FUNDS",
]
BEHAVIORAL_CODES = [
    "CHECKOUT_ABANDONED",
    "PAYMENT_TIMEOUT_USER_IDLE",
    "OTP_NOT_ENTERED",
]
TECHNICAL_CODES = [
    "GATEWAY_TIMEOUT",
    "EXPIRED_TOKEN",
    "NETWORK_ERROR",
]

LTV_TIERS = ["LOW", "MEDIUM", "HIGH"]

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUTPUT_PATH = DATA_DIR / "synthetic_events.json"


def _now_minus(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _new_payment_id() -> str:
    return f"pay_{uuid.uuid4().hex[:14]}"


def _base_event(rng: random.Random, bucket_codes: list[str]) -> dict:
    return {
        "payment_id": _new_payment_id(),
        "amount": round(rng.uniform(150, 45000), 2),
        "currency": "INR",
        "failure_reason_code": rng.choice(bucket_codes),
        "customer_id": f"cust_{uuid.uuid4().hex[:10]}",
        "customer_ltv_tier": rng.choice(LTV_TIERS),
        "retry_count": rng.choice([0, 0, 0, 1, 1, 2]),
        "timestamp": _now_minus(rng.uniform(1, 96)),
        "opt_out": False,
        "dispute_raised": False,
    }


def generate_events(n_normal: int = 168, seed: int = 42) -> list[dict]:
    """Generate n_normal well-formed events plus 7 fixed edge cases."""
    rng = random.Random(seed)
    events: list[dict] = []

    n_financial = round(n_normal * 0.5)
    n_behavioral = round(n_normal * 0.3)
    n_technical = n_normal - n_financial - n_behavioral

    for _ in range(n_financial):
        events.append(_base_event(rng, FINANCIAL_CODES))
    for _ in range(n_behavioral):
        events.append(_base_event(rng, BEHAVIORAL_CODES))
    for _ in range(n_technical):
        events.append(_base_event(rng, TECHNICAL_CODES))

    rng.shuffle(events)

    # --- 7 deliberate edge cases, appended so they're easy to locate ---

    # 1. Missing customer_id entirely.
    e1 = _base_event(rng, FINANCIAL_CODES)
    del e1["customer_id"]
    events.append(e1)

    # 2. Negative amount.
    e2 = _base_event(rng, BEHAVIORAL_CODES)
    e2["amount"] = -499.00
    events.append(e2)

    # 3. Unknown / unmapped failure_reason_code.
    e3 = _base_event(rng, TECHNICAL_CODES)
    e3["failure_reason_code"] = "UNMAPPED_UPSTREAM_ERROR_9912"
    events.append(e3)

    # 4. Duplicate payment_id — two rows sharing the same id.
    e4a = _base_event(rng, FINANCIAL_CODES)
    dup_id = e4a["payment_id"]
    events.append(e4a)
    e4b = _base_event(rng, BEHAVIORAL_CODES)
    e4b["payment_id"] = dup_id
    events.append(e4b)

    # 5. Dispute already raised at ingestion.
    e5 = _base_event(rng, FINANCIAL_CODES)
    e5["dispute_raised"] = True
    events.append(e5)

    # 6. Customer opted out at ingestion.
    e6 = _base_event(rng, BEHAVIORAL_CODES)
    e6["opt_out"] = True
    events.append(e6)

    # 7. Malformed data type — amount arrives as a non-numeric string.
    e7 = _base_event(rng, TECHNICAL_CODES)
    e7["amount"] = "not-a-number"
    events.append(e7)

    rng.shuffle(events)
    return events


def save_events(events: list[dict], path: Path = OUTPUT_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(events, f, indent=2)
    return path


def _summarize(events: list[dict]) -> None:
    buckets = {"FINANCIAL": 0, "BEHAVIORAL": 0, "TECHNICAL": 0, "OTHER": 0}
    all_codes = set(FINANCIAL_CODES) | set(BEHAVIORAL_CODES) | set(TECHNICAL_CODES)
    for e in events:
        code = e.get("failure_reason_code")
        if code in FINANCIAL_CODES:
            buckets["FINANCIAL"] += 1
        elif code in BEHAVIORAL_CODES:
            buckets["BEHAVIORAL"] += 1
        elif code in TECHNICAL_CODES:
            buckets["TECHNICAL"] += 1
        else:
            buckets["OTHER"] += 1
    print(f"Total events: {len(events)}")
    print(f"Bucket distribution: {buckets}")
    print(f"Known reason codes considered: {len(all_codes)}")


if __name__ == "__main__":
    events = generate_events()
    out_path = save_events(events)
    _summarize(events)
    print(f"Wrote {len(events)} events to {out_path}")
