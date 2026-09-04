"""
pipeline.py
Orchestrates a full batch run: diagnose -> select action plan ->
sequence (gate + adapters) -> audit log -> escalate unresolved
cases -> report -> log run history. This is the module app.py
(Streamlit) calls.

If RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET aren't set, payment-link
creation automatically falls back to a clearly-labeled simulated
link (simulated=True) so the pipeline is still fully demoable
without live credentials. Whenever real keys ARE present, the real
Razorpay test-mode API is used -- and is checked for an already-
issued link first (idempotency), so a case that already has a live
payment link outstanding never gets a second one.
"""

from __future__ import annotations

import os
import random
import uuid
from datetime import datetime, timezone

from src.action_selector import get_action_plan
from src.adapters.payment_link import create_payment_link
from src.adapters.smart_retry import attempt_smart_retry
from src.audit import (
    clear_escalations,
    get_connection,
    get_last_plink_for_payment,
    get_open_escalation,
    log_batch_run,
    log_escalation,
    log_step,
    query_escalations,
    record_issued_link,
    reset_db,
    update_escalation_status,
)
from src.diagnose import diagnose_batch
from src.escalation import build_ticket, should_escalate, simulate_daily_triage
from src.generate_data import generate_events
from src.policy_gate import GateInputError
from src.report import CaseRecord, build_report
from src.sequencer import run_sequence


def _simulated_payment_link(**kwargs) -> dict:
    return {
        "success": True,
        "plink_id": f"plink_sim_{uuid.uuid4().hex[:12]}",
        "short_url": "https://rzp.io/simulated-test-mode-link",
        "simulated": True,
    }


def _real_or_simulated_link_fn():
    """Pick the real adapter if credentials exist, else a simulated one."""
    if os.environ.get("RAZORPAY_KEY_ID") and os.environ.get("RAZORPAY_KEY_SECRET"):
        return create_payment_link
    return _simulated_payment_link


def _make_idempotent_payment_link_fn(conn, base_fn):
    """
    Wrap a payment-link function so it never issues a second live link
    for a payment_id that already has one outstanding. Checks
    `issued_links` (persistent across runs) before calling `base_fn`,
    and records what it creates so future calls -- in this run or a
    later one -- see it.
    """

    def _wrapped(**kwargs):
        payment_id = kwargs.get("reference_id")
        if payment_id:
            existing = get_last_plink_for_payment(conn, payment_id)
            if existing:
                return {
                    "success": True,
                    "plink_id": existing,
                    "short_url": None,
                    "simulated": False,
                    "reused_existing": True,
                }

        result = base_fn(**kwargs)
        if result.get("success") and payment_id and result.get("plink_id"):
            record_issued_link(
                conn,
                payment_id,
                result["plink_id"],
                result.get("short_url"),
                datetime.now(timezone.utc).isoformat(),
            )
        return result

    return _wrapped


def simulate_batch(
    events: list[dict],
    touch_cap: int,
    discount_cap_pct: float,
    sms_cost: float,
    llm_triage_cost: float,
    gateway_fee_rate: float,
    seed: int = 7,
) -> dict:
    """
    Run a "what-if" pass over the same batch under different policy
    limits and cost assumptions, WITHOUT touching the real audit
    trail, run history, or issued-links idempotency table, and
    WITHOUT ever calling the real Razorpay API -- this always uses
    the simulated adapters, regardless of whether real keys are
    configured, since a hypothetical exploration run should never
    create a real side effect.

    Returns just the report dict (plus invalid_count) -- there's no
    audit log, no escalation queue, and no run-history entry for a
    simulation, by design: it's exploratory, not an actual batch run.
    """
    rng = random.Random(seed)
    diagnoses = diagnose_batch(events)

    case_records: list[CaseRecord] = []

    for diagnosis in diagnoses:
        if diagnosis.status != "VALID":
            continue

        event = diagnosis.raw_event
        diag_dict = diagnosis.to_dict()
        action_plan = get_action_plan(diagnosis.ltv_tier, diagnosis.bucket)

        try:
            seq_result = run_sequence(
                event,
                diag_dict,
                smart_retry_fn=attempt_smart_retry,
                payment_link_fn=_simulated_payment_link,
                rng=rng,
                action_plan=action_plan,
                touch_cap=touch_cap,
                discount_cap_pct=discount_cap_pct,
            )
        except GateInputError:
            continue

        amount = event.get("amount", 0)
        amount = float(amount) if isinstance(amount, (int, float)) else 0.0

        case_records.append(
            CaseRecord(
                payment_id=str(diagnosis.payment_id),
                amount=amount,
                bucket=diagnosis.bucket or "UNKNOWN",
                recovered=seq_result.recovered,
                final_outcome=seq_result.final_outcome,
                stopping_rule_hit=seq_result.stopping_rule_hit,
                steps_executed=len([s for s in seq_result.steps if s.outcome not in ("BLOCKED",)]),
                ltv_tier=diagnosis.ltv_tier,
                used_llm_triage=False,
                link_issued=seq_result.link_issued,
            )
        )

    report = build_report(
        case_records,
        sms_cost=sms_cost,
        llm_triage_cost=llm_triage_cost,
        gateway_fee_rate=gateway_fee_rate,
    )
    invalid_count = len([d for d in diagnoses if d.status != "VALID"])

    return {"report": report, "invalid_count": invalid_count}


def run_batch(events: list[dict], seed: int = 7) -> dict:
    """
    Run the full pipeline over a batch of raw events.

    Returns a dict with:
        - "diagnoses": list of DiagnosisResult.to_dict()
        - "sequences": list of SequenceResult.to_dict() (VALID events only)
        - "report": output of report.build_report()
        - "escalations": list of EscalationTicket.to_dict() for every
          BLOCKED or UNRECOVERED case
        - "invalid_count": count of events that never reached the gate
    """
    rng = random.Random(seed)
    diagnoses = diagnose_batch(events)

    reset_db()
    conn = get_connection()

    payment_link_fn = _make_idempotent_payment_link_fn(conn, _real_or_simulated_link_fn())

    sequence_results = []
    case_records: list[CaseRecord] = []
    escalation_tickets: list[dict] = []

    for diagnosis in diagnoses:
        recovery_id = f"rec_{uuid.uuid4().hex[:10]}"
        now = datetime.now(timezone.utc).isoformat()

        if diagnosis.status != "VALID":
            log_step(
                conn,
                recovery_id=recovery_id,
                trigger_event=str(diagnosis.payment_id),
                diagnosed_cause=None,
                policy_evaluation="N/A",
                intervention_executed="NONE",
                stopping_rule_hit=f"INVALID_EVENT:{diagnosis.invalid_reason}",
                plink_id=None,
                outcome="INVALID",
                timestamp=now,
            )
            continue

        event = diagnosis.raw_event
        diag_dict = diagnosis.to_dict()
        action_plan = get_action_plan(diagnosis.ltv_tier, diagnosis.bucket)

        try:
            seq_result = run_sequence(
                event,
                diag_dict,
                smart_retry_fn=attempt_smart_retry,
                payment_link_fn=payment_link_fn,
                rng=rng,
                action_plan=action_plan,
            )
        except GateInputError as exc:
            log_step(
                conn,
                recovery_id=recovery_id,
                trigger_event=str(diagnosis.payment_id),
                diagnosed_cause=diagnosis.bucket,
                policy_evaluation="GATE_INPUT_ERROR",
                intervention_executed="NONE",
                stopping_rule_hit=f"GATE_INPUT_ERROR:{exc}",
                plink_id=None,
                outcome="ERROR",
                timestamp=now,
            )
            continue

        sequence_results.append(seq_result.to_dict())

        for step in seq_result.steps:
            log_step(
                conn,
                recovery_id=recovery_id,
                trigger_event=str(diagnosis.payment_id),
                diagnosed_cause=diagnosis.bucket,
                policy_evaluation=(step.gate_decision or {}).get("decision", "N/A"),
                intervention_executed=step.action,
                stopping_rule_hit=(
                    step.gate_decision.get("reason")
                    if step.gate_decision and step.gate_decision.get("decision") == "BLOCK"
                    else None
                ),
                plink_id=step.plink_id,
                outcome=step.outcome,
                timestamp=now,
            )

        amount = event.get("amount", 0)
        amount = float(amount) if isinstance(amount, (int, float)) else 0.0

        case_records.append(
            CaseRecord(
                payment_id=str(diagnosis.payment_id),
                amount=amount,
                bucket=diagnosis.bucket or "UNKNOWN",
                recovered=seq_result.recovered,
                final_outcome=seq_result.final_outcome,
                stopping_rule_hit=seq_result.stopping_rule_hit,
                steps_executed=len([s for s in seq_result.steps if s.outcome not in ("BLOCKED",)]),
                ltv_tier=diagnosis.ltv_tier,
                used_llm_triage=False,
                link_issued=seq_result.link_issued,
            )
        )

        # Every case that didn't recover needs a human queue entry --
        # either it was blocked by the safety gate, or it ran its full
        # sequence with no recovery. But escalations now persist across
        # runs (see audit.py's module docstring), so a case that's still
        # unresolved from a PRIOR run must not get a second ticket piling
        # up on top of the one already open -- only open a fresh ticket
        # if there isn't already one being worked for this payment_id.
        if should_escalate(seq_result.final_outcome):
            existing = get_open_escalation(conn, str(diagnosis.payment_id))
            if existing is None:
                reason = seq_result.stopping_rule_hit or "UNRECOVERED"
                ticket = build_ticket(
                    payment_id=str(diagnosis.payment_id),
                    reason=reason,
                    amount=amount,
                    ltv_tier=diagnosis.ltv_tier,
                )
                log_escalation(
                    conn,
                    payment_id=ticket.payment_id,
                    reason=ticket.reason,
                    route=ticket.route,
                    note=ticket.note,
                    priority_score=ticket.priority_score,
                    amount=ticket.amount,
                    ltv_tier=ticket.ltv_tier,
                    timestamp=now,
                )
                escalation_tickets.append(ticket.to_dict())
            else:
                escalation_tickets.append(dict(existing))

    report = build_report(case_records)
    invalid_count = len([d for d in diagnoses if d.status != "VALID"])

    log_batch_run(
        conn,
        run_timestamp=datetime.now(timezone.utc).isoformat(),
        total_cases=len(case_records),
        gross_revenue_recovered=report["gross_revenue_recovered"],
        net_revenue_preserved=report["net_revenue_preserved"],
        net_recovery_rate_pct=report["net_recovery_rate_pct"],
        revenue_at_risk=report["revenue_at_risk"],
        blocked_case_count=report["blocked_case_count"],
        invalid_count=invalid_count,
    )

    # Single commit for the whole run, instead of one per log_step/
    # log_escalation/record_issued_link call -- see the comments in
    # audit.py. This also makes a run's audit trail effectively atomic:
    # if something raises partway through the loop above, nothing from
    # this run has been persisted yet, rather than leaving a half-written
    # partial trail from whichever rows happened to commit first.
    conn.commit()
    conn.close()

    return {
        "diagnoses": [d.to_dict() for d in diagnoses],
        "sequences": sequence_results,
        "report": report,
        "escalations": escalation_tickets,
        "invalid_count": invalid_count,
    }


def run_multiday_simulation(
    days: int,
    events_per_day_range: tuple[int, int] = (140, 200),
    seed: int = 100,
) -> dict:
    """
    Simulate `days` consecutive days of a fresh batch of failed
    payments arriving and being processed, with a simulated human team
    triaging the escalation queue in between days.

    This exists purely to give the dashboard's Trends and Escalations
    tabs something realistic to demo without needing days of actual
    manual clicking -- every single day still goes through the exact
    same diagnose -> gate -> sequence -> audit -> escalate -> report
    path as one real run_batch() call. NOTHING about the recovery
    logic itself is relaxed, sped up, or faked for this mode; the only
    thing this function adds on top of calling run_batch() in a loop
    is a simulated pace of human ticket resolution between days (see
    escalation.simulate_daily_triage), so the escalation queue has a
    realistic mix of OPEN/IN_PROGRESS/RESOLVED tickets by the end
    instead of every single ticket from every day sitting untouched.

    Clears the escalation queue once at the very start, same reasoning
    as the "Regenerate synthetic batch" button (see
    audit.clear_escalations()'s docstring) -- each day mints a fresh
    random set of payment_ids sharing no relationship with the
    previous day's, so old tickets from a PRIOR simulation run could
    never be resolved or matched again.

    Returns a summary dict:
        {"days_simulated": int,
         "day_summaries": [{"day", "events_count", "report",
                             "new_escalations", "resolved_by_triage"}, ...],
         "final_open_count": int, "final_resolved_count": int,
         "last_day_full_result": <same shape run_batch() returns, for
             the most recently simulated day>}
    """
    rng = random.Random(seed)
    clear_escalations()

    day_summaries = []
    last_day_full_result = None
    for day in range(1, days + 1):
        n_events = rng.randint(*events_per_day_range)
        events = generate_events(n_normal=n_events, seed=rng.randint(1, 10_000_000))
        result = run_batch(events)
        last_day_full_result = result

        open_tickets = [e for e in query_escalations() if e["status"] in ("OPEN", "IN_PROGRESS")]
        resolved_ids = simulate_daily_triage(open_tickets, rng)
        now = datetime.now(timezone.utc).isoformat()
        for escalation_id in resolved_ids:
            update_escalation_status(escalation_id, "RESOLVED", now)

        day_summaries.append(
            {
                "day": day,
                "events_count": n_events,
                "report": result["report"],
                "new_escalations": len(result["escalations"]),
                "resolved_by_triage": len(resolved_ids),
            }
        )

    final_queue = query_escalations()
    return {
        "days_simulated": days,
        "day_summaries": day_summaries,
        "final_open_count": len([e for e in final_queue if e["status"] in ("OPEN", "IN_PROGRESS")]),
        "final_resolved_count": len([e for e in final_queue if e["status"] == "RESOLVED"]),
        # The full run_batch() result for the LAST simulated day -- same shape
        # a normal single run_batch() call returns (report/invalid_count/
        # diagnoses/sequences/escalations). The dashboard uses this directly
        # to populate Overview/Cohorts/etc. for "the most recent day", instead
        # of triggering a separate, unrelated run_batch() call against
        # whatever happens to be in data/synthetic_events.json.
        "last_day_full_result": last_day_full_result,
    }
