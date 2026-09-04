"""
Integration tests for pipeline.run_multiday_simulation -- the
multi-day demo mode that gives the dashboard's Trends and
Escalations tabs a realistic week of history without manual clicking.
Every day still goes through the real run_batch() path; these tests
focus on the parts unique to the multi-day orchestration itself:
history accumulation, queue clearing at the start, and triage
resolving a plausible (not total, not zero) fraction of the queue.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import src.audit as audit_module  # noqa: E402
from src.audit import query_batch_run_history, query_escalations  # noqa: E402
from src.pipeline import run_multiday_simulation  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Every test in this file gets its own throwaway SQLite file."""
    monkeypatch.setattr(audit_module, "DB_PATH", tmp_path / "multiday_test.db")
    yield


def test_returns_expected_top_level_keys():
    result = run_multiday_simulation(days=2, events_per_day_range=(20, 30), seed=1)
    assert set(result.keys()) == {
        "days_simulated", "day_summaries", "final_open_count", "final_resolved_count",
        "last_day_full_result",
    }
    assert result["days_simulated"] == 2
    assert len(result["day_summaries"]) == 2


def test_last_day_full_result_matches_run_batch_shape():
    """
    The dashboard sets st.session_state["pipeline_result"] directly to
    this value so Overview/Cohorts/the invalid-events table/PDF export
    all work without triggering a separate, unrelated run_batch() call
    -- it must have the exact same keys a normal run_batch() call
    returns.
    """
    result = run_multiday_simulation(days=2, events_per_day_range=(20, 30), seed=1)
    last_day = result["last_day_full_result"]
    assert set(last_day.keys()) == {"diagnoses", "sequences", "report", "escalations", "invalid_count"}


def test_last_day_full_result_report_matches_final_day_summary():
    result = run_multiday_simulation(days=3, events_per_day_range=(20, 30), seed=1)
    final_summary_report = result["day_summaries"][-1]["report"]
    assert result["last_day_full_result"]["report"] == final_summary_report


def test_each_day_summary_has_expected_fields():
    result = run_multiday_simulation(days=1, events_per_day_range=(20, 30), seed=1)
    day = result["day_summaries"][0]
    assert set(day.keys()) == {"day", "events_count", "report", "new_escalations", "resolved_by_triage"}
    assert day["day"] == 1
    assert 20 <= day["events_count"] <= 30
    assert "net_recovery_rate_pct" in day["report"]


def test_records_one_batch_run_per_day():
    run_multiday_simulation(days=5, events_per_day_range=(20, 30), seed=2)
    history = query_batch_run_history(audit_module.DB_PATH)
    assert len(history) == 5


def test_clears_pre_existing_escalations_before_starting():
    """
    A leftover queue from an earlier, unrelated run/regenerate must
    not bleed into a fresh multi-day simulation -- same reasoning as
    the regenerate-batch fix (see audit.clear_escalations()'s
    docstring).
    """
    from src.audit import get_connection, log_escalation

    conn = get_connection(audit_module.DB_PATH)
    log_escalation(conn, "pay_stale", "DISPUTE_RAISED", "DISPUTE_RESOLUTION", "old", 3, 100.0, "HIGH", "t0")
    conn.commit()
    conn.close()
    assert len(query_escalations(audit_module.DB_PATH)) == 1

    run_multiday_simulation(days=1, events_per_day_range=(20, 30), seed=3)

    remaining = query_escalations(audit_module.DB_PATH)
    assert all(e["payment_id"] != "pay_stale" for e in remaining)


def test_triage_resolves_some_but_not_all_of_the_queue():
    """
    The whole point of simulating daily triage is a realistic MIX of
    resolved and still-open tickets by the end of the week -- not
    every ticket instantly resolved (which would make the Escalations
    tab look fake/empty) and not zero resolved (which would make the
    "lifecycle" feature look pointless in this demo mode).
    """
    result = run_multiday_simulation(days=7, events_per_day_range=(140, 200), seed=42)
    assert result["final_open_count"] > 0
    assert result["final_resolved_count"] > 0


def test_higher_priority_tickets_resolve_more_often_across_the_week():
    """
    Integration-level check that the priority-weighted triage
    simulation (escalation.simulate_daily_triage) actually has an
    effect end to end: DISPUTE_RAISED tickets (priority >= LOW, often
    HIGH/MEDIUM) should resolve at a noticeably higher rate than
    TOUCH_CAP_EXCEEDED tickets routed to NONE (priority_score 0).
    """
    run_multiday_simulation(days=7, events_per_day_range=(140, 200), seed=7)
    rows = query_escalations(audit_module.DB_PATH)

    zero_priority = [r for r in rows if r["priority_score"] == 0]
    high_priority = [r for r in rows if r["priority_score"] == 3]

    if zero_priority and high_priority:
        zero_resolved_rate = len([r for r in zero_priority if r["status"] == "RESOLVED"]) / len(zero_priority)
        high_resolved_rate = len([r for r in high_priority if r["status"] == "RESOLVED"]) / len(high_priority)
        assert high_resolved_rate > zero_resolved_rate


def test_days_parameter_controls_iteration_count():
    result_short = run_multiday_simulation(days=1, events_per_day_range=(20, 30), seed=5)
    assert len(result_short["day_summaries"]) == 1

    result_long = run_multiday_simulation(days=10, events_per_day_range=(20, 30), seed=5)
    assert len(result_long["day_summaries"]) == 10


def test_deterministic_with_same_seed():
    result_a = run_multiday_simulation(days=3, events_per_day_range=(20, 30), seed=123)
    result_b = run_multiday_simulation(days=3, events_per_day_range=(20, 30), seed=123)
    events_a = [d["events_count"] for d in result_a["day_summaries"]]
    events_b = [d["events_count"] for d in result_b["day_summaries"]]
    assert events_a == events_b
