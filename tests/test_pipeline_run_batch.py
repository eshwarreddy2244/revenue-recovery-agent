"""
Integration test for pipeline.run_batch -- deliberately exercises the
full diagnose -> gate -> sequence -> audit -> escalate -> report wiring
end to end, importing run_batch by name. Unit tests for the individual
modules (diagnose, policy_gate, sequencer, escalation, report) don't
catch a wiring break between them; this does.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import src.audit as audit_module  # noqa: E402
from src.pipeline import run_batch  # noqa: E402


def _events():
    return [
        {
            "payment_id": f"pay_run_{i}",
            "amount": 1000.0 + i * 10,
            "currency": "INR",
            "failure_reason_code": "GATEWAY_TIMEOUT",
            "customer_id": f"cust_{i}",
            "customer_ltv_tier": "MEDIUM",
            "retry_count": 0,
            "opt_out": False,
            "dispute_raised": False,
        }
        for i in range(15)
    ] + [
        # one deliberately invalid row, one opt-out, one dispute
        {"payment_id": "pay_bad", "amount": -5, "currency": "INR",
         "failure_reason_code": "GATEWAY_TIMEOUT", "customer_id": "c", "customer_ltv_tier": "LOW",
         "retry_count": 0, "opt_out": False, "dispute_raised": False},
        {"payment_id": "pay_optout", "amount": 500.0, "currency": "INR",
         "failure_reason_code": "OTP_NOT_ENTERED", "customer_id": "c2", "customer_ltv_tier": "LOW",
         "retry_count": 0, "opt_out": True, "dispute_raised": False},
        {"payment_id": "pay_dispute", "amount": 700.0, "currency": "INR",
         "failure_reason_code": "CARD_LIMIT_EXCEEDED", "customer_id": "c3", "customer_ltv_tier": "HIGH",
         "retry_count": 0, "opt_out": False, "dispute_raised": True},
    ]


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Every test in this file gets its own throwaway SQLite file."""
    monkeypatch.setattr(audit_module, "DB_PATH", tmp_path / "run_batch_test.db")
    yield


def test_run_batch_returns_all_expected_top_level_keys():
    result = run_batch(_events())
    assert set(result.keys()) == {"diagnoses", "sequences", "report", "escalations", "invalid_count"}


def test_run_batch_flags_the_invalid_row():
    result = run_batch(_events())
    assert result["invalid_count"] == 1
    invalid = [d for d in result["diagnoses"] if d["status"] == "INVALID"]
    assert any(d["payment_id"] == "pay_bad" for d in invalid)


def test_run_batch_blocks_opt_out_and_dispute_cases():
    result = run_batch(_events())
    blocked_ids = {
        s["payment_id"] for s in result["sequences"] if s["final_outcome"] == "BLOCKED"
    }
    assert "pay_optout" in blocked_ids
    assert "pay_dispute" in blocked_ids


def test_run_batch_generates_escalation_tickets_for_blocked_cases():
    result = run_batch(_events())
    escalation_ids = {e["payment_id"] for e in result["escalations"]}
    assert "pay_optout" in escalation_ids
    assert "pay_dispute" in escalation_ids


def test_run_batch_does_not_duplicate_open_escalation_tickets_across_runs():
    """
    escalations now persist across reset_db (see audit.py) -- a case
    still unresolved from a prior run must not spawn a SECOND open
    ticket on the next run, or the queue would just accumulate
    duplicate work items for the same underlying problem forever.
    """
    from src.audit import query_escalations

    run_batch(_events())
    run_batch(_events())
    run_batch(_events())

    rows = query_escalations(audit_module.DB_PATH)
    optout_tickets = [r for r in rows if r["payment_id"] == "pay_optout"]
    dispute_tickets = [r for r in rows if r["payment_id"] == "pay_dispute"]
    assert len(optout_tickets) == 1
    assert len(dispute_tickets) == 1
    assert optout_tickets[0]["status"] == "OPEN"


def test_run_batch_report_totals_match_case_count():
    result = run_batch(_events())
    # 18 total events, 1 invalid -> 17 valid cases should reach the sequencer.
    assert result["report"]["total_cases"] == 17


def test_run_batch_writes_to_audit_log(tmp_path):
    from src.audit import query_batch

    run_batch(_events())
    rows = query_batch(audit_module.DB_PATH)
    assert len(rows) > 0


def test_run_batch_records_batch_run_history():
    from src.audit import query_batch_run_history

    run_batch(_events())
    history = query_batch_run_history(audit_module.DB_PATH)
    assert len(history) == 1
    assert history[0]["total_cases"] == 17


def test_run_batch_opens_new_ticket_after_prior_one_was_resolved():
    """
    Deduping only applies to OPEN/IN_PROGRESS tickets -- once a human
    has resolved one, a case that escalates again later is a genuinely
    new issue and should get a fresh ticket, not be silently swallowed
    forever because *a* ticket for this payment_id existed once.
    """
    from src.audit import query_escalations, update_escalation_status

    run_batch(_events())
    first_ticket = [r for r in query_escalations(audit_module.DB_PATH) if r["payment_id"] == "pay_optout"][0]
    update_escalation_status(first_ticket["id"], "RESOLVED", "t_resolved", audit_module.DB_PATH)

    run_batch(_events())

    tickets = [r for r in query_escalations(audit_module.DB_PATH) if r["payment_id"] == "pay_optout"]
    assert len(tickets) == 2
    statuses = {t["status"] for t in tickets}
    assert statuses == {"RESOLVED", "OPEN"}


def test_run_batch_is_importable_and_callable_twice_in_a_row():
    """Regression guard: a prior bug left run_batch's `def` line accidentally
    deleted during an edit, which silently turned it into dead code appended
    to simulate_batch. No unit test caught it because none imported run_batch
    directly. This test exists specifically to catch that class of bug again."""
    r1 = run_batch(_events())
    r2 = run_batch(_events())
    assert r1["report"]["total_cases"] == r2["report"]["total_cases"] == 17
