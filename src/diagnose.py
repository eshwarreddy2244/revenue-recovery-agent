"""
diagnose.py
Deterministic, rule-based diagnostic engine.

Intentionally contains NO LLM calls. Failure reason codes are mapped
to a bucket via a static lookup table. An unrecognized code is
classified UNKNOWN — it is never guessed into a bucket it wasn't
seen in.

Every event is validated before diagnosis. A malformed event never
crashes the batch: it comes back with status="INVALID" and a reason,
and the batch keeps moving.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

REQUIRED_FIELDS = ["payment_id", "amount", "customer_id", "failure_reason_code"]

REASON_CODE_MAP: dict[str, str] = {
    # FINANCIAL
    "INSUFFICIENT_BALANCE": "FINANCIAL",
    "CARD_LIMIT_EXCEEDED": "FINANCIAL",
    "BANK_DECLINED_INSUFFICIENT_FUNDS": "FINANCIAL",
    # BEHAVIORAL
    "CHECKOUT_ABANDONED": "BEHAVIORAL",
    "PAYMENT_TIMEOUT_USER_IDLE": "BEHAVIORAL",
    "OTP_NOT_ENTERED": "BEHAVIORAL",
    # TECHNICAL
    "GATEWAY_TIMEOUT": "TECHNICAL",
    "EXPIRED_TOKEN": "TECHNICAL",
    "NETWORK_ERROR": "TECHNICAL",
}


@dataclass
class DiagnosisResult:
    payment_id: Any
    status: str  # "VALID" or "INVALID"
    bucket: str | None = None  # FINANCIAL / BEHAVIORAL / TECHNICAL / UNKNOWN
    ltv_tier: str | None = None
    prior_touch_count: int | None = None
    invalid_reason: str | None = None
    raw_event: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "payment_id": self.payment_id,
            "status": self.status,
            "bucket": self.bucket,
            "ltv_tier": self.ltv_tier,
            "prior_touch_count": self.prior_touch_count,
            "invalid_reason": self.invalid_reason,
        }


def _validate_event(event: dict) -> str | None:
    """Return an invalid_reason string, or None if the event passes."""
    if not isinstance(event, dict):
        return "EVENT_NOT_A_DICT"

    for field_name in REQUIRED_FIELDS:
        if field_name not in event or event.get(field_name) in (None, ""):
            return f"MISSING_REQUIRED_FIELD:{field_name}"

    amount = event.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return "MALFORMED_FIELD_TYPE:amount"
    if isinstance(amount, float) and (math.isnan(amount) or math.isinf(amount)):
        return "MALFORMED_FIELD_TYPE:amount"
    if amount <= 0:
        return "NON_POSITIVE_AMOUNT"

    if not isinstance(event.get("failure_reason_code"), str):
        return "MALFORMED_FIELD_TYPE:failure_reason_code"

    return None


def diagnose_event(event: dict, seen_payment_ids: set[str]) -> DiagnosisResult:
    """
    Diagnose a single event. Never raises on bad input — always
    returns a DiagnosisResult with status VALID or INVALID.

    `seen_payment_ids` is mutated by the caller (diagnose_batch) to
    track duplicates across the batch — every event with a non-null
    payment_id gets added, regardless of whether it was VALID or
    INVALID, so a duplicate is caught even if the first row with that
    id happened to be malformed for an unrelated reason (e.g. a
    negative amount). This function only checks against the set, it
    doesn't add to it — the caller decides when to add.
    """
    payment_id = event.get("payment_id") if isinstance(event, dict) else None

    if payment_id is not None and payment_id in seen_payment_ids:
        return DiagnosisResult(
            payment_id=payment_id,
            status="INVALID",
            invalid_reason="DUPLICATE_PAYMENT_ID",
            raw_event=event if isinstance(event, dict) else {},
        )

    invalid_reason = _validate_event(event)
    if invalid_reason:
        return DiagnosisResult(
            payment_id=payment_id,
            status="INVALID",
            invalid_reason=invalid_reason,
            raw_event=event if isinstance(event, dict) else {},
        )

    code = event["failure_reason_code"]
    bucket = REASON_CODE_MAP.get(code, "UNKNOWN")

    retry_count = event.get("retry_count", 0)
    if not isinstance(retry_count, int) or isinstance(retry_count, bool) or retry_count < 0:
        retry_count = 0

    return DiagnosisResult(
        payment_id=payment_id,
        status="VALID",
        bucket=bucket,
        ltv_tier=event.get("customer_ltv_tier"),
        prior_touch_count=retry_count,
        raw_event=event,
    )


def diagnose_batch(events: list[dict]) -> list[DiagnosisResult]:
    """
    Diagnose a full batch. Duplicate payment_ids: the FIRST occurrence
    of an id is diagnosed normally (VALID or INVALID on its own
    merits); every later occurrence of that same id is flagged
    INVALID/DUPLICATE_PAYMENT_ID, per spec ("flag the repeat
    occurrence") — regardless of whether the first occurrence itself
    was valid.
    """
    results: list[DiagnosisResult] = []
    seen_ids: set[str] = set()

    for event in events:
        try:
            result = diagnose_event(event, seen_ids)
        except Exception as exc:  # belt-and-braces: batch must never die
            result = DiagnosisResult(
                payment_id=(event.get("payment_id") if isinstance(event, dict) else None),
                status="INVALID",
                invalid_reason=f"UNEXPECTED_ERROR:{exc.__class__.__name__}",
                raw_event=event if isinstance(event, dict) else {},
            )
        if result.payment_id is not None and result.invalid_reason != "DUPLICATE_PAYMENT_ID":
            seen_ids.add(result.payment_id)
        results.append(result)

    return results
