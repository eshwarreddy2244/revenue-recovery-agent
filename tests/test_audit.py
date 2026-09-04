import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlite3  # noqa: E402

import pytest  # noqa: E402

from src.audit import (  # noqa: E402
    ESCALATION_STATUSES,
    clear_escalations,
    get_connection,
    get_last_plink_for_payment,
    get_open_escalation,
    log_batch_run,
    log_escalation,
    log_step,
    query_batch,
    query_batch_run_history,
    query_escalations,
    record_issued_link,
    reset_db,
    update_escalation_assignment,
    update_escalation_status,
)


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_audit.db"


def test_reset_db_clears_audit_log_but_not_escalations_or_batch_runs(db_path):
    """
    escalations now persists across reset_db, same as batch_runs and
    issued_links -- a ticket is a real queue item meant to be worked
    across multiple pipeline runs, not a snapshot of the current
    batch. Only audit_log (the step-by-step trail of the CURRENT
    batch) gets cleared.
    """
    conn = get_connection(db_path)
    log_step(conn, "rec_1", "pay_1", "FINANCIAL", "ALLOW", "smart_retry", None, None, "RECOVERED", "t1")
    log_escalation(conn, "pay_1", "DISPUTE_RAISED", "disputes_team", "note", 70, 1000.0, "HIGH", "t1")
    log_batch_run(conn, "t1", 10, 5000.0, 4800.0, 80.0, 6000.0, 2, 1)
    conn.commit()
    conn.close()

    reset_db(db_path)

    assert query_batch(db_path) == []
    escalations = query_escalations(db_path)
    assert len(escalations) == 1
    assert escalations[0]["payment_id"] == "pay_1"
    history = query_batch_run_history(db_path)
    assert len(history) == 1
    assert history[0]["total_cases"] == 10


def test_query_escalations_orders_by_priority_score_desc(db_path):
    conn = get_connection(db_path)
    log_escalation(conn, "pay_low", "OPT_OUT", "archive_no_action", "n", 5, 100.0, "LOW", "t1")
    log_escalation(conn, "pay_high", "DISPUTE_RAISED", "disputes_team", "n", 90, 5000.0, "HIGH", "t2")
    conn.commit()
    conn.close()

    rows = query_escalations(db_path)
    assert rows[0]["payment_id"] == "pay_high"
    assert rows[1]["payment_id"] == "pay_low"


def test_idempotency_returns_none_when_no_link_issued_yet(db_path):
    conn = get_connection(db_path)
    result = get_last_plink_for_payment(conn, "pay_never_issued")
    conn.close()
    assert result is None


def test_idempotency_returns_recorded_link(db_path):
    conn = get_connection(db_path)
    record_issued_link(conn, "pay_1", "plink_abc123", "https://rzp.io/x", "t1")
    result = get_last_plink_for_payment(conn, "pay_1")
    conn.commit()
    conn.close()
    assert result == "plink_abc123"


def test_idempotency_survives_reset_db(db_path):
    """
    Unlike audit_log, issued_links must NOT be cleared by reset_db --
    otherwise idempotency across separate runs wouldn't mean anything.
    """
    conn = get_connection(db_path)
    record_issued_link(conn, "pay_1", "plink_abc123", "https://rzp.io/x", "t1")
    conn.commit()
    conn.close()

    reset_db(db_path)

    conn2 = get_connection(db_path)
    result = get_last_plink_for_payment(conn2, "pay_1")
    conn2.close()
    assert result == "plink_abc123"


def test_record_issued_link_upserts_on_same_payment_id(db_path):
    conn = get_connection(db_path)
    record_issued_link(conn, "pay_1", "plink_first", "https://rzp.io/first", "t1")
    record_issued_link(conn, "pay_1", "plink_second", "https://rzp.io/second", "t2")
    result = get_last_plink_for_payment(conn, "pay_1")
    conn.commit()
    conn.close()
    assert result == "plink_second"


def test_batch_run_history_returned_oldest_first(db_path):
    conn = get_connection(db_path)
    log_batch_run(conn, "t1", 10, 1000.0, 900.0, 70.0, 2000.0, 1, 0)
    log_batch_run(conn, "t2", 12, 1500.0, 1400.0, 75.0, 2500.0, 2, 1)
    conn.commit()
    conn.close()

    history = query_batch_run_history(db_path)
    assert [h["run_timestamp"] for h in history] == ["t1", "t2"]


def test_log_step_and_log_escalation_require_caller_to_commit(db_path):
    """
    Regression guard for the fsync-storm fix: log_step/log_escalation/
    record_issued_link/log_batch_run deliberately do NOT commit
    internally anymore (pipeline.run_batch commits once for the whole
    batch instead of once per row -- see pipeline.py and the comments in
    audit.py). This test pins that contract: without an explicit
    conn.commit(), closing the connection must discard the write.
    """
    conn = get_connection(db_path)
    log_step(conn, "rec_x", "pay_x", "FINANCIAL", "ALLOW", "smart_retry", None, None, "RECOVERED", "t1")
    conn.close()  # deliberately no commit

    assert query_batch(db_path) == []


def test_new_escalation_defaults_to_open_status(db_path):
    conn = get_connection(db_path)
    log_escalation(conn, "pay_1", "DISPUTE_RAISED", "DISPUTE_RESOLUTION", "note", 3, 1000.0, "HIGH", "t1")
    conn.commit()
    conn.close()

    rows = query_escalations(db_path)
    assert rows[0]["status"] == "OPEN"
    assert rows[0]["updated_at"] == "t1"


def test_get_open_escalation_finds_open_ticket(db_path):
    conn = get_connection(db_path)
    log_escalation(conn, "pay_1", "DISPUTE_RAISED", "DISPUTE_RESOLUTION", "note", 3, 1000.0, "HIGH", "t1")
    conn.commit()
    conn.close()

    conn2 = get_connection(db_path)
    found = get_open_escalation(conn2, "pay_1")
    conn2.close()
    assert found is not None
    assert found["payment_id"] == "pay_1"
    assert found["status"] == "OPEN"


def test_get_open_escalation_returns_none_when_none_open(db_path):
    conn = get_connection(db_path)
    found = get_open_escalation(conn, "pay_never_escalated")
    conn.close()
    assert found is None


def test_get_open_escalation_ignores_resolved_tickets(db_path):
    conn = get_connection(db_path)
    log_escalation(conn, "pay_1", "DISPUTE_RAISED", "DISPUTE_RESOLUTION", "note", 3, 1000.0, "HIGH", "t1")
    conn.commit()
    escalation_id = query_escalations(db_path)[0]["id"]
    conn.close()

    update_escalation_status(escalation_id, "RESOLVED", "t2", db_path)

    conn2 = get_connection(db_path)
    found = get_open_escalation(conn2, "pay_1")
    conn2.close()
    assert found is None


def test_get_open_escalation_finds_in_progress_ticket(db_path):
    conn = get_connection(db_path)
    log_escalation(conn, "pay_1", "DISPUTE_RAISED", "DISPUTE_RESOLUTION", "note", 3, 1000.0, "HIGH", "t1")
    conn.commit()
    escalation_id = query_escalations(db_path)[0]["id"]
    conn.close()

    update_escalation_status(escalation_id, "IN_PROGRESS", "t2", db_path)

    conn2 = get_connection(db_path)
    found = get_open_escalation(conn2, "pay_1")
    conn2.close()
    assert found is not None
    assert found["status"] == "IN_PROGRESS"


def test_update_escalation_status_persists_immediately(db_path):
    """
    Unlike log_step/log_escalation, update_escalation_status commits
    for itself -- it's called directly by db_path (no shared conn
    threaded through a batch run), and the whole point is that a
    dashboard click should be durable right away.
    """
    conn = get_connection(db_path)
    log_escalation(conn, "pay_1", "DISPUTE_RAISED", "DISPUTE_RESOLUTION", "note", 3, 1000.0, "HIGH", "t1")
    conn.commit()
    escalation_id = query_escalations(db_path)[0]["id"]
    conn.close()

    update_escalation_status(escalation_id, "RESOLVED", "t2", db_path)

    rows = query_escalations(db_path)
    assert rows[0]["status"] == "RESOLVED"
    assert rows[0]["updated_at"] == "t2"


def test_update_escalation_status_rejects_invalid_status(db_path):
    conn = get_connection(db_path)
    log_escalation(conn, "pay_1", "DISPUTE_RAISED", "DISPUTE_RESOLUTION", "note", 3, 1000.0, "HIGH", "t1")
    conn.commit()
    escalation_id = query_escalations(db_path)[0]["id"]
    conn.close()

    with pytest.raises(ValueError):
        update_escalation_status(escalation_id, "CLOSED_FOREVER", "t2", db_path)


def test_query_escalations_filters_by_status(db_path):
    conn = get_connection(db_path)
    log_escalation(conn, "pay_1", "DISPUTE_RAISED", "DISPUTE_RESOLUTION", "note", 3, 1000.0, "HIGH", "t1")
    log_escalation(conn, "pay_2", "UNRECOVERED", "RETENTION", "note", 2, 500.0, "LOW", "t1")
    conn.commit()
    escalation_id = query_escalations(db_path)[-1]["id"]  # the LOW/pay_2 one (lower priority, sorts last)
    conn.close()

    update_escalation_status(escalation_id, "RESOLVED", "t2", db_path)

    open_only = query_escalations(db_path, status="OPEN")
    resolved_only = query_escalations(db_path, status="RESOLVED")
    assert len(open_only) == 1
    assert open_only[0]["payment_id"] == "pay_1"
    assert len(resolved_only) == 1
    assert resolved_only[0]["payment_id"] == "pay_2"


def test_query_escalations_default_view_puts_resolved_last(db_path):
    conn = get_connection(db_path)
    log_escalation(conn, "pay_1", "DISPUTE_RAISED", "DISPUTE_RESOLUTION", "note", 1, 1000.0, "LOW", "t1")
    conn.commit()
    escalation_id = query_escalations(db_path)[0]["id"]
    conn.close()
    update_escalation_status(escalation_id, "RESOLVED", "t2", db_path)

    conn3 = get_connection(db_path)
    log_escalation(conn3, "pay_2", "OPT_OUT", "NONE", "note", 0, 500.0, "LOW", "t3")
    conn3.commit()
    conn3.close()

    rows = query_escalations(db_path)
    # Even though pay_1's priority_score (1) > pay_2's (0), the RESOLVED
    # ticket must still sort after the OPEN one.
    assert rows[0]["payment_id"] == "pay_2"
    assert rows[0]["status"] == "OPEN"
    assert rows[1]["payment_id"] == "pay_1"
    assert rows[1]["status"] == "RESOLVED"


def test_escalation_statuses_constant_matches_valid_values():
    assert set(ESCALATION_STATUSES) == {"OPEN", "IN_PROGRESS", "RESOLVED"}


def test_clear_escalations_removes_all_regardless_of_status(db_path):
    conn = get_connection(db_path)
    log_escalation(conn, "pay_1", "DISPUTE_RAISED", "DISPUTE_RESOLUTION", "note", 3, 1000.0, "HIGH", "t1")
    log_escalation(conn, "pay_2", "UNRECOVERED", "RETENTION", "note", 2, 500.0, "LOW", "t1")
    conn.commit()
    resolved_id = query_escalations(db_path)[0]["id"]
    conn.close()
    update_escalation_status(resolved_id, "RESOLVED", "t2", db_path)

    assert len(query_escalations(db_path)) == 2

    clear_escalations(db_path)

    assert query_escalations(db_path) == []


def test_clear_escalations_leaves_batch_runs_and_issued_links_untouched(db_path):
    """
    Only the escalation queue is tied to one batch's payment_ids.
    Run history (Trends tab) and payment-link idempotency have no
    such relationship and must survive clear_escalations() exactly
    like they survive reset_db().
    """
    conn = get_connection(db_path)
    log_escalation(conn, "pay_1", "DISPUTE_RAISED", "DISPUTE_RESOLUTION", "note", 3, 1000.0, "HIGH", "t1")
    log_batch_run(conn, "t1", 10, 5000.0, 4800.0, 80.0, 6000.0, 2, 1)
    record_issued_link(conn, "pay_1", "plink_abc123", "https://rzp.io/x", "t1")
    conn.commit()
    conn.close()

    clear_escalations(db_path)

    assert query_escalations(db_path) == []
    assert len(query_batch_run_history(db_path)) == 1

    conn2 = get_connection(db_path)
    result = get_last_plink_for_payment(conn2, "pay_1")
    conn2.close()
    assert result == "plink_abc123"


def test_clear_escalations_on_empty_table_does_not_raise(db_path):
    clear_escalations(db_path)
    assert query_escalations(db_path) == []


def test_new_escalation_starts_unassigned(db_path):
    conn = get_connection(db_path)
    log_escalation(conn, "pay_1", "DISPUTE_RAISED", "DISPUTE_RESOLUTION", "note", 3, 1000.0, "HIGH", "t1")
    conn.commit()
    conn.close()

    rows = query_escalations(db_path)
    assert rows[0]["assigned_to"] is None


def test_update_escalation_assignment_claims_a_ticket(db_path):
    conn = get_connection(db_path)
    log_escalation(conn, "pay_1", "DISPUTE_RAISED", "DISPUTE_RESOLUTION", "note", 3, 1000.0, "HIGH", "t1")
    conn.commit()
    escalation_id = query_escalations(db_path)[0]["id"]
    conn.close()

    update_escalation_assignment(escalation_id, "Ananya K.", "t2", db_path)

    rows = query_escalations(db_path)
    assert rows[0]["assigned_to"] == "Ananya K."
    assert rows[0]["updated_at"] == "t2"


def test_update_escalation_assignment_with_none_unassigns(db_path):
    conn = get_connection(db_path)
    log_escalation(conn, "pay_1", "DISPUTE_RAISED", "DISPUTE_RESOLUTION", "note", 3, 1000.0, "HIGH", "t1")
    conn.commit()
    escalation_id = query_escalations(db_path)[0]["id"]
    conn.close()

    update_escalation_assignment(escalation_id, "Ananya K.", "t2", db_path)
    update_escalation_assignment(escalation_id, None, "t3", db_path)

    rows = query_escalations(db_path)
    assert rows[0]["assigned_to"] is None


def test_update_escalation_assignment_with_empty_string_normalizes_to_none(db_path):
    conn = get_connection(db_path)
    log_escalation(conn, "pay_1", "DISPUTE_RAISED", "DISPUTE_RESOLUTION", "note", 3, 1000.0, "HIGH", "t1")
    conn.commit()
    escalation_id = query_escalations(db_path)[0]["id"]
    conn.close()

    update_escalation_assignment(escalation_id, "", "t2", db_path)

    rows = query_escalations(db_path)
    assert rows[0]["assigned_to"] is None


def test_pre_existing_database_migrates_assigned_to_column(db_path):
    """
    Same migration pattern as status/updated_at: a database created
    before assigned_to existed should pick up the column automatically
    on next connection, defaulting existing rows to NULL (unassigned)
    rather than erroring or requiring a manual reset.
    """
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE escalations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payment_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            route TEXT NOT NULL,
            note TEXT,
            priority_score INTEGER,
            amount REAL,
            ltv_tier TEXT,
            timestamp TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'OPEN',
            updated_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO escalations (payment_id, reason, route, note, priority_score, amount, ltv_tier, timestamp) "
        "VALUES ('pay_legacy', 'DISPUTE_RAISED', 'DISPUTE_RESOLUTION', 'old', 3, 500.0, 'HIGH', 't0')"
    )
    conn.commit()
    conn.close()

    rows = query_escalations(db_path)
    assert rows[0]["assigned_to"] is None
