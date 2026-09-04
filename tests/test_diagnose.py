import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.diagnose import diagnose_batch, diagnose_event  # noqa: E402


def _good_event(**overrides):
    e = {
        "payment_id": "pay_abc123",
        "amount": 1000.0,
        "currency": "INR",
        "failure_reason_code": "INSUFFICIENT_BALANCE",
        "customer_id": "cust_1",
        "customer_ltv_tier": "HIGH",
        "retry_count": 1,
        "timestamp": "2026-01-01T00:00:00+00:00",
        "opt_out": False,
        "dispute_raised": False,
    }
    e.update(overrides)
    return e


def test_known_reason_codes_map_correctly():
    cases = {
        "INSUFFICIENT_BALANCE": "FINANCIAL",
        "CARD_LIMIT_EXCEEDED": "FINANCIAL",
        "BANK_DECLINED_INSUFFICIENT_FUNDS": "FINANCIAL",
        "CHECKOUT_ABANDONED": "BEHAVIORAL",
        "PAYMENT_TIMEOUT_USER_IDLE": "BEHAVIORAL",
        "OTP_NOT_ENTERED": "BEHAVIORAL",
        "GATEWAY_TIMEOUT": "TECHNICAL",
        "EXPIRED_TOKEN": "TECHNICAL",
        "NETWORK_ERROR": "TECHNICAL",
    }
    for code, expected_bucket in cases.items():
        result = diagnose_event(_good_event(failure_reason_code=code), set())
        assert result.status == "VALID"
        assert result.bucket == expected_bucket


def test_unknown_reason_code_maps_to_unknown_not_guessed():
    result = diagnose_event(_good_event(failure_reason_code="SOME_NEW_CODE_9999"), set())
    assert result.status == "VALID"
    assert result.bucket == "UNKNOWN"


def test_missing_required_field_marks_invalid_with_field_named():
    event = _good_event()
    del event["customer_id"]
    result = diagnose_event(event, set())
    assert result.status == "INVALID"
    assert result.invalid_reason == "MISSING_REQUIRED_FIELD:customer_id"


def test_negative_amount_marks_invalid():
    result = diagnose_event(_good_event(amount=-50.0), set())
    assert result.status == "INVALID"
    assert result.invalid_reason == "NON_POSITIVE_AMOUNT"


def test_zero_amount_marks_invalid():
    result = diagnose_event(_good_event(amount=0), set())
    assert result.status == "INVALID"
    assert result.invalid_reason == "NON_POSITIVE_AMOUNT"


def test_malformed_amount_type_marks_invalid_without_crashing():
    result = diagnose_event(_good_event(amount="not-a-number"), set())
    assert result.status == "INVALID"
    assert result.invalid_reason == "MALFORMED_FIELD_TYPE:amount"


def test_duplicate_payment_id_flags_second_occurrence():
    e1 = _good_event(payment_id="pay_dup")
    e2 = _good_event(payment_id="pay_dup", failure_reason_code="GATEWAY_TIMEOUT")
    results = diagnose_batch([e1, e2])
    assert results[0].status == "VALID"
    assert results[1].status == "INVALID"
    assert results[1].invalid_reason == "DUPLICATE_PAYMENT_ID"


def test_batch_never_raises_on_bad_rows():
    events = [
        _good_event(),
        {"garbage": True},
        None,
        _good_event(amount=-1),
        "not even a dict",
    ]
    # Should not raise despite the garbage rows.
    results = diagnose_batch(events)
    assert len(results) == len(events)
    statuses = [r.status for r in results]
    assert statuses[0] == "VALID"
    assert statuses[1] == "INVALID"
    assert statuses[3] == "INVALID"


def test_enrichment_carries_ltv_and_touch_count():
    result = diagnose_event(_good_event(customer_ltv_tier="LOW", retry_count=2), set())
    assert result.ltv_tier == "LOW"
    assert result.prior_touch_count == 2


def test_nan_amount_marks_invalid():
    result = diagnose_event(_good_event(amount=float("nan")), set())
    assert result.status == "INVALID"
    assert result.invalid_reason == "MALFORMED_FIELD_TYPE:amount"


def test_infinite_amount_marks_invalid():
    result = diagnose_event(_good_event(amount=float("inf")), set())
    assert result.status == "INVALID"
    assert result.invalid_reason == "MALFORMED_FIELD_TYPE:amount"


def test_duplicate_caught_even_when_first_occurrence_was_invalid():
    """
    If the FIRST row with a given payment_id is invalid for an
    unrelated reason (e.g. negative amount), a later row reusing that
    same id must still be flagged as a duplicate, not diagnosed as if
    it were the first sighting of that id.
    """
    e1 = _good_event(payment_id="pay_reused", amount=-10.0)
    e2 = _good_event(payment_id="pay_reused", amount=500.0)
    results = diagnose_batch([e1, e2])
    assert results[0].status == "INVALID"
    assert results[0].invalid_reason == "NON_POSITIVE_AMOUNT"
    assert results[1].status == "INVALID"
    assert results[1].invalid_reason == "DUPLICATE_PAYMENT_ID"


def test_triple_duplicate_flags_second_and_third():
    e1 = _good_event(payment_id="pay_triple")
    e2 = _good_event(payment_id="pay_triple")
    e3 = _good_event(payment_id="pay_triple")
    results = diagnose_batch([e1, e2, e3])
    assert results[0].status == "VALID"
    assert results[1].invalid_reason == "DUPLICATE_PAYMENT_ID"
    assert results[2].invalid_reason == "DUPLICATE_PAYMENT_ID"


def test_missing_payment_ids_are_not_treated_as_duplicates_of_each_other():
    e1 = _good_event()
    del e1["payment_id"]
    e2 = _good_event()
    del e2["payment_id"]
    results = diagnose_batch([e1, e2])
    # Both invalid for missing payment_id, but neither should be
    # mistakenly flagged as a duplicate of the other via None == None.
    assert results[0].invalid_reason == "MISSING_REQUIRED_FIELD:payment_id"
    assert results[1].invalid_reason == "MISSING_REQUIRED_FIELD:payment_id"


def test_negative_retry_count_treated_as_zero_not_negative_touch_count():
    result = diagnose_event(_good_event(retry_count=-5), set())
    assert result.status == "VALID"
    assert result.prior_touch_count == 0
