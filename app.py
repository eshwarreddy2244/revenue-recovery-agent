"""
app.py
Streamlit dashboard for the AI Revenue Recovery Agent.

Run with:
    streamlit run app.py
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from src.audit import (
    ESCALATION_STATUSES,
    clear_escalations,
    query_batch,
    query_batch_run_history,
    query_escalations,
    update_escalation_assignment,
    update_escalation_status,
)
from src.diagnose import REASON_CODE_MAP
from src.escalation import ALL_TEAM_MEMBERS
from src.generate_data import generate_events, save_events
from src.llm_triage import generate_dispute_copy, parse_freetext_reason
from src.pipeline import run_batch, run_multiday_simulation, simulate_batch
from src.report import GATEWAY_FEE_RATE, LLM_TRIAGE_COST, SMS_COST
from src.report_pdf import build_report_pdf
from src.sequencer import run_sequence

load_dotenv()

st.set_page_config(page_title="AI Revenue Recovery Agent", page_icon="\U0001F4B3", layout="wide")

DATA_PATH = Path(__file__).resolve().parent / "data" / "synthetic_events.json"

st.markdown(
    """
    <style>
      div[data-testid="stMetricValue"] { font-size: 1.7rem; }
      div[data-testid="stMetric"] {
          background-color: rgba(255,255,255,0.03);
          border: 1px solid rgba(255,255,255,0.08);
          border-radius: 10px;
          padding: 0.75rem 1rem 0.4rem 1rem;
      }
      .block-container { padding-top: 2rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("\U0001F4B3 AI Revenue Recovery Agent")
st.caption("Razorpay Buildathon — AI Revenue Recovery Track")

# ---------------------------------------------------------------------------
# Sidebar controls
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("Batch controls")
    if st.button("\U0001F504 Regenerate synthetic batch", use_container_width=True):
        events = generate_events(seed=random.randint(1, 999999))
        save_events(events)
        st.session_state.pop("pipeline_result", None)
        # A fresh batch mints entirely new random payment_ids, sharing no
        # relationship with the previous batch's -- old escalation tickets
        # can never be matched, resolved, or deduped against again, so
        # they'd just accumulate forever across regenerates. Clearing them
        # here is what keeps the escalation queue meaningful; see
        # clear_escalations()'s docstring for the full reasoning. Run
        # history (Trends tab) and issued-link idempotency intentionally
        # do NOT get cleared -- only the escalation queue is tied to
        # payment_ids specific to one batch.
        clear_escalations()
        st.success(f"Regenerated {len(events)} events. Escalation queue cleared for the new batch.")

    run_clicked = st.button(
        "\u25B6\uFE0F Run pipeline on current batch", type="primary", use_container_width=True
    )

    st.divider()
    st.caption(
        "Payment links use the real Razorpay TEST-mode API when "
        "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are set in .env, and are "
        "issued at most once per payment_id (idempotent) — a case that "
        "already has a live link is never sent a second one. Otherwise, "
        "a clearly-labeled simulated link is used."
    )
    st.caption(
        "Free-text triage and dispute copy use Groq (llama-3.3-70b) when "
        "GROQ_API_KEY is set — never in the diagnose/gate/sequencer path."
    )
    st.caption(
        "High-LTV customers on non-behavioral failures are fast-tracked "
        "straight to a payment link, skipping the impersonal retry step "
        "(see action_selector.py)."
    )

# ---------------------------------------------------------------------------
# Load events
# ---------------------------------------------------------------------------
if not DATA_PATH.exists():
    events = generate_events()
    save_events(events)
else:
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        events = json.load(f)

if run_clicked or "pipeline_result" not in st.session_state:
    with st.spinner("Running diagnose -> gate -> sequence -> audit -> report..."):
        st.session_state["pipeline_result"] = run_batch(events)

result = st.session_state["pipeline_result"]
report = result["report"]

overview_tab, cohorts_tab, audit_tab, escalations_tab, trends_tab, multiday_tab, simulator_tab, safety_tab, triage_tab = st.tabs(
    [
        "\U0001F4CA Overview",
        "\U0001F465 Cohorts",
        "\U0001F4C3 Audit log",
        "\u2600\uFE0F Escalations",
        "\U0001F4C8 Trends",
        "\U0001F4C5 Simulate a week",
        "\U0001F9EA What-if simulator",
        "\U0001F6E1\uFE0F Safety-gate demo",
        "\U0001F916 LLM triage (optional)",
    ]
)

# ---------------------------------------------------------------------------
# Overview tab
# ---------------------------------------------------------------------------
with overview_tab:
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Revenue at risk", f"\u20B9{report['revenue_at_risk']:,.0f}")
    col2.metric(
        "Gross revenue recovered",
        f"\u20B9{report['gross_revenue_recovered']:,.0f}",
        delta=f"{report['pct_of_at_risk_recovered']:.1f}% of at-risk",
    )
    col3.metric("Net revenue preserved", f"\u20B9{report['net_revenue_preserved']:,.0f}")
    col4.metric("Net recovery rate", f"{report['net_recovery_rate_pct']:.1f}%")
    col5.metric("Invalid events (skipped safely)", result["invalid_count"])

    st.markdown(
        f"""
        <div style="background-color: rgba(46,164,79,0.08); border: 1px solid rgba(46,164,79,0.3);
                    border-radius: 10px; padding: 1rem 1.25rem; margin: 0.5rem 0 1rem 0;">
        <b>The business case in one line:</b> without any intervention, all
        \u20B9{report['revenue_at_risk']:,.0f} of failed payments in this batch would simply be lost.
        Running this agent recovered \u20B9{report['gross_revenue_recovered']:,.0f} of it
        ({report['pct_of_at_risk_recovered']:.1f}%), and after the cost of every SMS, LLM triage
        call, and gateway fee (\u20B9{report['total_action_cost']:,.0f} total), that's
        <b>\u20B9{report['net_revenue_preserved']:,.0f} preserved</b> \u2014 for roughly
        \u20B9{report['total_action_cost']:,.0f} spent, i.e. a
        {(report['net_revenue_preserved'] / report['total_action_cost']) if report['total_action_cost'] else 0:,.1f}x
        return on the cost of running the recovery flow itself.
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.caption(
        "\u2139\uFE0F 'Recovered' means the customer actually CONFIRMED payment — a simulated "
        "confirmation check, not a real webhook (there isn't one to call in test mode for a "
        "link nobody clicks). It is NOT the same as a payment link merely being issued; see "
        "the funnel below for that split, and README → adapters/confirmation.py for the model."
    )

    st.divider()

    st.subheader("Payment link funnel: issued vs. confirmed")
    st.caption(
        "Every payment link created is a technical success. Not every one gets paid. "
        "This is the number that answers 'so what's the REAL recovery rate' honestly."
    )
    f1, f2, f3, f4 = st.columns(4)
    f1.metric("Links issued", report["links_issued_count"])
    f2.metric("...of which confirmed (paid)", report["links_issued_count"] - report["link_issued_not_confirmed_count"])
    f3.metric("Issued but never paid", report["link_issued_not_confirmed_count"])
    f4.metric("Link → payment conversion", f"{report['link_confirmation_rate_pct']:.1f}%")

    st.divider()

    st.subheader("Recovery rate by failure bucket")
    bucket_rows = []
    for bucket, stats in report["recovery_by_bucket"].items():
        bucket_rows.append(
            {
                "bucket": bucket,
                "recovery_rate_pct": stats["recovery_rate_pct"],
                "total_cases": stats["total"],
                "recovered_cases": stats["recovered"],
                "gross_amount_recovered": stats["gross_amount_recovered"],
            }
        )
    bucket_df = pd.DataFrame(bucket_rows).set_index("bucket")
    if not bucket_df.empty:
        chart_col, table_col = st.columns([2, 1])
        chart_col.bar_chart(bucket_df["recovery_rate_pct"])
        table_col.dataframe(bucket_df[["total_cases", "recovered_cases"]], use_container_width=True)

    st.divider()

    st.subheader("Blocked, stopped, and skipped cases")
    b1, b2, b3 = st.columns(3)
    b1.metric("Blocked by safety gate", report["blocked_case_count"])
    b2.metric("Reached final step unrecovered", report["stopped_unrecovered_count"])
    b3.metric("Invalid / skipped events", result["invalid_count"])
    if report["blocked_reasons"]:
        reasons_df = pd.DataFrame(
            [{"reason": k, "count": v} for k, v in report["blocked_reasons"].items()]
        ).set_index("reason")
        st.dataframe(reasons_df, use_container_width=True, key="overview_blocked_reasons_table")

    st.divider()

    st.subheader("Invalid / skipped events")
    st.caption(
        "Events that failed validation before ever reaching the safety gate — "
        "each carries the specific reason it was rejected, and never crashed the batch."
    )
    invalid_rows = [d for d in result["diagnoses"] if d["status"] != "VALID"]
    if invalid_rows:
        invalid_df = pd.DataFrame(invalid_rows)[["payment_id", "status", "invalid_reason"]]
        st.dataframe(invalid_df, use_container_width=True, key="overview_invalid_events_table")
        st.download_button(
            "\u2B07\uFE0F Download invalid events (CSV)",
            data=invalid_df.to_csv(index=False).encode("utf-8"),
            file_name="invalid_events.csv",
            mime="text/csv",
        )
    else:
        st.info("No invalid events in this batch.")

    st.divider()
    dl1, dl2 = st.columns(2)
    dl1.download_button(
        "\u2B07\uFE0F Download full report (JSON)",
        data=json.dumps(report, indent=2).encode("utf-8"),
        file_name="revenue_recovery_report.json",
        mime="application/json",
    )
    pdf_bytes = build_report_pdf(report, result["invalid_count"])
    dl2.download_button(
        "\U0001F4C4 Download report (PDF)",
        data=pdf_bytes,
        file_name="revenue_recovery_report.pdf",
        mime="application/pdf",
    )

# ---------------------------------------------------------------------------
# Cohorts tab (by customer LTV tier)
# ---------------------------------------------------------------------------
with cohorts_tab:
    st.subheader("Recovery rate by customer LTV tier")
    st.caption(
        "High-LTV customers on non-behavioral failures skip straight to a payment link "
        "(see the Safety-gate demo tab's sidebar note) — this view shows whether that "
        "actually pays off."
    )
    st.caption(
        "\u2139\uFE0F Confirmation is genuinely probabilistic (see adapters/confirmation.py), and "
        "each tier here is only ~50-60 cases — tier ordering can flip from run to run on sampling "
        "noise alone. The LTV-routing advantage is a real, tested effect (see test_action_selector.py), "
        "but don't read one batch's exact ranking as proof; look at it across several runs."
    )
    ltv_rows = []
    for tier, stats in report.get("recovery_by_ltv_tier", {}).items():
        ltv_rows.append(
            {
                "tier": tier,
                "recovery_rate_pct": stats["recovery_rate_pct"],
                "total_cases": stats["total"],
                "recovered_cases": stats["recovered"],
                "gross_amount_recovered": stats["gross_amount_recovered"],
            }
        )
    ltv_df = pd.DataFrame(ltv_rows).set_index("tier")
    if not ltv_df.empty:
        # Keep a sensible tier order when present.
        order = [t for t in ["HIGH", "MEDIUM", "LOW", "UNKNOWN"] if t in ltv_df.index]
        ltv_df = ltv_df.reindex(order) if order else ltv_df
        chart_col, table_col = st.columns([2, 1])
        chart_col.bar_chart(ltv_df["recovery_rate_pct"])
        table_col.dataframe(ltv_df, use_container_width=True)
    else:
        st.info("No cohort data yet — run the pipeline.")

# ---------------------------------------------------------------------------
# Audit log tab
# ---------------------------------------------------------------------------
with audit_tab:
    st.subheader("Audit log")
    audit_rows = query_batch()
    audit_df = pd.DataFrame(audit_rows)

    if not audit_df.empty:
        filt_col1, filt_col2 = st.columns(2)
        outcomes = ["All"] + sorted(audit_df["outcome"].dropna().unique().tolist())
        causes = ["All"] + sorted(audit_df["diagnosed_cause"].dropna().unique().tolist())
        outcome_filter = filt_col1.selectbox("Filter by outcome", outcomes)
        bucket_filter = filt_col2.selectbox("Filter by diagnosed bucket", causes)

        filtered = audit_df.copy()
        if outcome_filter != "All":
            filtered = filtered[filtered["outcome"] == outcome_filter]
        if bucket_filter != "All":
            filtered = filtered[filtered["diagnosed_cause"] == bucket_filter]

        st.dataframe(filtered, use_container_width=True, height=400, key="audit_log_table")
        st.download_button(
            "\u2B07\uFE0F Download filtered audit log (CSV)",
            data=filtered.to_csv(index=False).encode("utf-8"),
            file_name="audit_log.csv",
            mime="text/csv",
        )
    else:
        st.info("No audit rows yet — run the pipeline.")

# ---------------------------------------------------------------------------
# Escalations tab (human handoff queue)
# ---------------------------------------------------------------------------
with escalations_tab:
    st.subheader("Escalation queue")
    st.caption(
        "Every BLOCKED or fully-UNRECOVERED case lands here with a route and a "
        "priority score (amount + LTV tier + reason) — nothing the gate stops is a "
        "dead end, it's a queue with an owner. Priority score is a triage aid only; "
        "it never overrides what the safety gate allowed or blocked. Tickets persist "
        "across re-runs of the same batch — a case still unresolved from a prior run "
        "keeps its existing ticket rather than getting a duplicate. Regenerating a "
        "fresh synthetic batch clears the queue, since new batches use entirely new "
        "payment IDs with no relationship to the old tickets."
    )

    esc_rows = query_escalations()
    esc_df = pd.DataFrame(esc_rows)

    if not esc_df.empty:
        esc_df["assigned_to"] = esc_df["assigned_to"].fillna("Unassigned")

        status_counts = esc_df["status"].value_counts()
        open_mask = esc_df["status"].isin(["OPEN", "IN_PROGRESS"])
        unassigned_open_count = int(((esc_df["assigned_to"] == "Unassigned") & open_mask).sum())

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Open", int(status_counts.get("OPEN", 0)))
        m2.metric("In progress", int(status_counts.get("IN_PROGRESS", 0)))
        m3.metric("Resolved", int(status_counts.get("RESOLVED", 0)))
        m4.metric("Unassigned (open work)", unassigned_open_count)

        f1, f2, f3 = st.columns(3)
        route_filter = f1.selectbox("Filter by route", ["All"] + sorted(esc_df["route"].unique().tolist()))
        status_filter = f2.selectbox("Filter by status", ["All (open first)"] + list(ESCALATION_STATUSES))
        assignee_filter = f3.selectbox("Filter by assignee", ["All", "Unassigned"] + ALL_TEAM_MEMBERS)

        filtered_esc = esc_df.copy()
        if route_filter != "All":
            filtered_esc = filtered_esc[filtered_esc["route"] == route_filter]
        if status_filter != "All (open first)":
            filtered_esc = filtered_esc[filtered_esc["status"] == status_filter]
        else:
            # Open/in-progress work first, resolved last -- same ordering
            # query_escalations() already applies server-side.
            filtered_esc = filtered_esc.sort_values(
                by=["status", "priority_score"],
                key=lambda col: col.map({"OPEN": 0, "IN_PROGRESS": 0, "RESOLVED": 1}) if col.name == "status" else col,
                ascending=[True, False],
            )
        if assignee_filter != "All":
            filtered_esc = filtered_esc[filtered_esc["assigned_to"] == assignee_filter]

        st.caption("Edit **status** or **assigned_to** directly, then click Save below.")
        display_cols = [
            "id", "payment_id", "reason", "route", "priority_score", "amount",
            "ltv_tier", "status", "assigned_to", "note",
        ]
        edited = st.data_editor(
            filtered_esc[display_cols],
            use_container_width=True,
            height=420,
            hide_index=True,
            disabled=["id", "payment_id", "reason", "route", "priority_score", "amount", "ltv_tier", "note"],
            column_config={
                "status": st.column_config.SelectboxColumn(
                    "status", options=list(ESCALATION_STATUSES), required=True
                ),
                "assigned_to": st.column_config.SelectboxColumn(
                    "assigned_to", options=["Unassigned"] + ALL_TEAM_MEMBERS, required=True
                ),
            },
            key="escalation_editor",
        )

        if st.button("Save changes", type="primary"):
            original_status = filtered_esc.set_index("id")["status"].to_dict()
            original_assignee = filtered_esc.set_index("id")["assigned_to"].to_dict()
            edited_status = edited.set_index("id")["status"].to_dict()
            edited_assignee = edited.set_index("id")["assigned_to"].to_dict()

            changed_status = {
                esc_id: new for esc_id, new in edited_status.items() if original_status.get(esc_id) != new
            }
            changed_assignee = {
                esc_id: new for esc_id, new in edited_assignee.items() if original_assignee.get(esc_id) != new
            }

            if changed_status or changed_assignee:
                now = datetime.now(timezone.utc).isoformat()
                for esc_id, new_status in changed_status.items():
                    update_escalation_status(int(esc_id), new_status, now)
                for esc_id, new_assignee in changed_assignee.items():
                    update_escalation_assignment(
                        int(esc_id), None if new_assignee == "Unassigned" else new_assignee, now
                    )
                total_changed = len(set(changed_status) | set(changed_assignee))
                st.success(f"Updated {total_changed} ticket(s).")
                st.rerun()
            else:
                st.info("No changes to save.")

        st.download_button(
            "\u2B07\uFE0F Download escalation queue (CSV)",
            data=filtered_esc[display_cols].to_csv(index=False).encode("utf-8"),
            file_name="escalation_queue.csv",
            mime="text/csv",
        )
    else:
        st.info("No escalations yet — nothing has been blocked or left unrecovered so far.")

# ---------------------------------------------------------------------------
# Trends tab (across multiple pipeline runs)
# ---------------------------------------------------------------------------
with trends_tab:
    st.subheader("Trend across pipeline runs")
    st.caption(
        "Unlike the audit log, run history is NOT cleared when you regenerate or "
        "re-run a batch — every run you trigger in this session adds a point here."
    )
    history_rows = query_batch_run_history()

    if len(history_rows) >= 2:
        hist_df = pd.DataFrame(history_rows)
        hist_df["run"] = range(1, len(hist_df) + 1)
        hist_df = hist_df.set_index("run")

        st.line_chart(hist_df[["net_recovery_rate_pct"]])
        c1, c2 = st.columns(2)
        c1.line_chart(hist_df[["gross_revenue_recovered", "revenue_at_risk"]])
        c2.line_chart(hist_df[["blocked_case_count", "invalid_count"]])

        st.dataframe(
            hist_df[
                [
                    "run_timestamp", "total_cases", "revenue_at_risk",
                    "gross_revenue_recovered", "net_recovery_rate_pct", "blocked_case_count",
                ]
            ],
            use_container_width=True,
            key="trends_history_table_multi_run",
        )
    elif len(history_rows) == 1:
        # A line chart needs at least two points to draw a line -- with
        # exactly one row, Streamlit's underlying Altair chart still
        # renders an (empty, oddly-scaled) axis frame rather than nothing,
        # which reads as broken. Show the single run as a table instead,
        # and skip the chart entirely until there's a second point.
        hist_df = pd.DataFrame(history_rows)
        st.dataframe(
            hist_df[
                [
                    "run_timestamp", "total_cases", "revenue_at_risk",
                    "gross_revenue_recovered", "net_recovery_rate_pct", "blocked_case_count",
                ]
            ],
            use_container_width=True,
            key="trends_history_table_single_run",
        )
        st.info(
            "Only one run so far — a trend needs at least two points to draw a "
            "line. Click 'Regenerate synthetic batch' then 'Run pipeline on "
            "current batch' again to see the actual trend charts."
        )
    else:
        st.info("No run history yet — run the pipeline at least once.")

# ---------------------------------------------------------------------------
# Multi-day simulation tab -- gives Trends/Escalations a realistic
# week of history without manual clicking, using the exact same
# run_batch() path per day plus simulated human triage between days.
# ---------------------------------------------------------------------------
with multiday_tab:
    st.subheader("Simulate a week of operation")
    st.caption(
        "Runs several consecutive days of a fresh batch of failed payments through the "
        "REAL pipeline (diagnose → gate → sequence → audit → escalate → report — nothing "
        "about the recovery logic itself is different or relaxed), then simulates a human "
        "team triaging a probabilistic, priority-weighted slice of the escalation queue "
        "between each day. This is what turns the Trends and Escalations tabs from a single "
        "flat run into something that looks like actual ongoing operation — queue growing "
        "AND shrinking day to day, not just accumulating forever."
    )
    st.caption(
        "\u26A0\uFE0F This clears the current escalation queue and adds several rows to run "
        "history, same as clicking 'Regenerate synthetic batch' + 'Run pipeline' several "
        "times in a row — it does not touch anything beyond what those buttons already do."
    )

    mc1, mc2 = st.columns(2)
    num_days = mc1.slider("Number of days to simulate", 3, 14, 7)
    day_seed = mc2.number_input("Random seed", min_value=1, max_value=999999, value=42, step=1)

    if st.button("\U0001F4C5 Simulate week", type="primary"):
        with st.spinner(f"Running {num_days} days through the full pipeline..."):
            multiday_result = run_multiday_simulation(days=num_days, seed=int(day_seed))
        st.session_state["multiday_result"] = multiday_result
        # Point Overview/Cohorts/Audit-log/PDF-export at the LAST simulated
        # day's actual result, instead of popping pipeline_result and letting
        # the bootstrap logic below re-run against whatever's on disk in
        # data/synthetic_events.json -- that would be an unrelated extra
        # run_batch() call (a wasted 8th batch_runs row) showing data that
        # has nothing to do with the week just simulated.
        st.session_state["pipeline_result"] = multiday_result["last_day_full_result"]
        st.success(f"Simulated {num_days} days.")
        st.rerun()

    if "multiday_result" in st.session_state:
        mres = st.session_state["multiday_result"]
        day_df = pd.DataFrame(
            [
                {
                    "day": d["day"],
                    "events": d["events_count"],
                    "net_recovery_rate_pct": d["report"]["net_recovery_rate_pct"],
                    "gross_revenue_recovered": d["report"]["gross_revenue_recovered"],
                    "new_escalations": d["new_escalations"],
                    "resolved_by_triage": d["resolved_by_triage"],
                }
                for d in mres["day_summaries"]
            ]
        ).set_index("day")

        s1, s2 = st.columns(2)
        s1.metric("Escalation tickets still open/in-progress", mres["final_open_count"])
        s2.metric("Escalation tickets resolved by simulated triage", mres["final_resolved_count"])

        st.line_chart(day_df[["net_recovery_rate_pct"]])
        c1, c2 = st.columns(2)
        c1.bar_chart(day_df[["new_escalations", "resolved_by_triage"]])
        c2.line_chart(day_df[["gross_revenue_recovered"]])

        st.dataframe(day_df, use_container_width=True, key="multiday_summary_table")
        st.caption(
            "Check the Trends and Escalations tabs now — both reflect the full week you just "
            "simulated, with a realistic mix of resolved and still-open tickets."
        )

# ---------------------------------------------------------------------------
# What-if simulator tab -- explore policy limits without touching the
# real audit trail, run history, or Razorpay API.
# ---------------------------------------------------------------------------
with simulator_tab:
    st.subheader("What-if policy simulator")
    st.caption(
        "Re-runs the SAME batch through the same deterministic gate and sequencer, "
        "under limits and costs you choose here. This never writes to the audit log, "
        "run history, or issued-links table, and never calls the real Razorpay API — "
        "it's exploratory, not a real batch run. Compare it against the baseline "
        "(current fixed policy) below."
    )

    sim_col1, sim_col2 = st.columns(2)
    with sim_col1:
        st.markdown("**Policy limits**")
        sim_touch_cap = st.slider("Touch cap (max contacts per case)", 1, 8, 3)
        sim_discount_cap = st.slider("Discount cap (%)", 0, 25, 5)
    with sim_col2:
        st.markdown("**Cost assumptions**")
        sim_sms_cost = st.number_input("SMS / notification cost (₹)", 0.0, 50.0, SMS_COST, step=0.05)
        sim_llm_cost = st.number_input("LLM triage cost (₹)", 0.0, 10.0, LLM_TRIAGE_COST, step=0.01)
        sim_gateway_fee = st.slider("Gateway fee (%)", 0.0, 10.0, GATEWAY_FEE_RATE * 100, step=0.1) / 100

    if st.button("Run simulation", type="primary"):
        baseline = simulate_batch(
            events, touch_cap=3, discount_cap_pct=5.0,
            sms_cost=SMS_COST, llm_triage_cost=LLM_TRIAGE_COST, gateway_fee_rate=GATEWAY_FEE_RATE,
        )
        scenario = simulate_batch(
            events, touch_cap=sim_touch_cap, discount_cap_pct=float(sim_discount_cap),
            sms_cost=sim_sms_cost, llm_triage_cost=sim_llm_cost, gateway_fee_rate=sim_gateway_fee,
        )

        base_report = baseline["report"]
        scen_report = scenario["report"]

        st.markdown("**Baseline (fixed policy: touch cap 3, discount cap 5%) vs. your scenario**")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric(
            "Net revenue preserved",
            f"\u20B9{scen_report['net_revenue_preserved']:,.0f}",
            delta=f"{scen_report['net_revenue_preserved'] - base_report['net_revenue_preserved']:,.0f}",
        )
        m2.metric(
            "Net recovery rate",
            f"{scen_report['net_recovery_rate_pct']:.1f}%",
            delta=f"{scen_report['net_recovery_rate_pct'] - base_report['net_recovery_rate_pct']:.1f} pts",
        )
        m3.metric(
            "Blocked cases",
            scen_report["blocked_case_count"],
            delta=int(scen_report["blocked_case_count"] - base_report["blocked_case_count"]),
            delta_color="inverse",
        )
        m4.metric(
            "Total action cost",
            f"\u20B9{scen_report['total_action_cost']:,.0f}",
            delta=f"{scen_report['total_action_cost'] - base_report['total_action_cost']:,.0f}",
            delta_color="inverse",
        )

        compare_df = pd.DataFrame(
            {
                "baseline": {
                    "net_revenue_preserved": base_report["net_revenue_preserved"],
                    "net_recovery_rate_pct": base_report["net_recovery_rate_pct"],
                    "blocked_case_count": base_report["blocked_case_count"],
                    "total_action_cost": base_report["total_action_cost"],
                },
                "your scenario": {
                    "net_revenue_preserved": scen_report["net_revenue_preserved"],
                    "net_recovery_rate_pct": scen_report["net_recovery_rate_pct"],
                    "blocked_case_count": scen_report["blocked_case_count"],
                    "total_action_cost": scen_report["total_action_cost"],
                },
            }
        )
        st.dataframe(compare_df, use_container_width=True, key="whatif_compare_table")
        st.caption(
            "Note: loosening the touch cap or discount cap doesn't change what the "
            "*safety-critical* rules are (opt-out and dispute still always block) — "
            "it only changes the soft-constraint thresholds, and only for this "
            "exploratory run."
        )

# ---------------------------------------------------------------------------
# Safety-gate demo tab
# ---------------------------------------------------------------------------
with safety_tab:
    st.subheader("Safety-gate demo: dispute raised mid-sequence")
    st.write(
        "This injects a dispute onto a live case partway through its recovery "
        "sequence and shows the gate halting further action immediately, with "
        "the exact stopping rule that fired."
    )

    if st.button("Run dispute-halt demo"):
        demo_event = {
            "payment_id": "pay_demo_dispute_case",
            "amount": 2499.0,
            "currency": "INR",
            "failure_reason_code": "GATEWAY_TIMEOUT",
            "customer_id": "cust_demo",
            "customer_ltv_tier": "HIGH",
            "retry_count": 0,
            "opt_out": False,
            "dispute_raised": False,
        }
        demo_diagnosis = {"bucket": "TECHNICAL", "prior_touch_count": 0}

        def _retry_fails_then_dispute(bucket, rng=None):
            # Step 1 fails to recover, then -- simulating something that
            # happened out of band -- a dispute lands on the case before
            # step 2's gate check runs.
            demo_event["dispute_raised"] = True
            return {"action": "smart_retry", "success": False, "simulated": True, "probability_used": 0.0}

        def _link_should_never_be_called(**kwargs):
            raise AssertionError("send_payment_link should never fire after a dispute halt")

        demo_result = run_sequence(
            demo_event,
            demo_diagnosis,
            smart_retry_fn=_retry_fails_then_dispute,
            payment_link_fn=_link_should_never_be_called,
        )

        st.json(demo_result.to_dict())

        if demo_result.stopping_rule_hit == "DISPUTE_RAISED":
            st.success(
                f"Sequence halted correctly. stopping_rule_hit = "
                f"'{demo_result.stopping_rule_hit}' — no further customer contact "
                f"was attempted after the dispute was detected."
            )
            st.markdown("**Customer-facing copy for this halt (Groq, Hinglish — optional, non-critical):**")
            copy_text = generate_dispute_copy(demo_event["payment_id"], demo_event["amount"], demo_event["currency"])
            st.info(copy_text)
        else:
            st.error("Expected a DISPUTE_RAISED halt but did not get one — check policy_gate.py")

# ---------------------------------------------------------------------------
# LLM triage tab (optional, non-safety-critical)
# ---------------------------------------------------------------------------
with triage_tab:
    st.subheader("Free-text failure triage (optional)")
    st.write(
        "Parses an ambiguous, free-text failure description into a known "
        "`failure_reason_code`. This never runs inside `diagnose.py`, "
        "`policy_gate.py`, or `sequencer.py` — it's a pre-processing helper "
        "only, and its output is always validated against the same "
        "deterministic map those modules use."
    )

    sample_text = st.text_area(
        "Free-text failure description",
        value="customer's card got declined, said something about a limit",
        height=80,
    )

    if st.button("Parse with LLM"):
        known_codes = list(REASON_CODE_MAP.keys())
        guessed = parse_freetext_reason(sample_text, known_codes)
        if guessed:
            st.success(f"Best-guess code: **{guessed}** → bucket **{REASON_CODE_MAP[guessed]}**")
        else:
            st.warning(
                "No confident match (or GROQ_API_KEY not set) — this would fall "
                "through to diagnose.py as bucket **UNKNOWN**, never guessed."
            )
