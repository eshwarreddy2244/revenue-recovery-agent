"""
report.py
Computes revenue-recovery metrics from a completed batch of
SequenceResult objects (paired with their original events/diagnoses).

Reports two distinct funnel stages rather than collapsing them into
one number: links_issued_count (a payment link was successfully
CREATED) and the recovered/net-recovery figures (the customer actually
CONFIRMED payment on it -- see adapters/confirmation.py and
sequencer.py). links_issued_count is always >= the confirmed count;
link_confirmation_rate_pct is what fraction of issued links actually
converted, which is the honest "did this actually work" number as
opposed to gross issuance volume.

Cost model per action taken (any step that actually executed,
i.e. not BLOCKED/STOPPED with no action):
    SMS / notification touch : Rs 0.20
    LLM triage (if used)     : Rs 0.05
    Gateway fee              : 2% of recovered amount
"""

from __future__ import annotations

from dataclasses import dataclass

SMS_COST = 0.20
LLM_TRIAGE_COST = 0.05
GATEWAY_FEE_RATE = 0.02


@dataclass
class CaseRecord:
    payment_id: str
    amount: float
    bucket: str
    recovered: bool
    final_outcome: str  # RECOVERED / UNRECOVERED / BLOCKED
    stopping_rule_hit: str | None
    steps_executed: int  # count of steps that actually ran an action
    ltv_tier: str | None = None
    used_llm_triage: bool = False
    link_issued: bool = False  # a payment link was successfully CREATED for
                                # this case -- may be True even when recovered
                                # is False, if the link was never paid on


def _action_cost(case: CaseRecord, sms_cost: float, llm_triage_cost: float, gateway_fee_rate: float) -> float:
    cost = case.steps_executed * sms_cost
    if case.used_llm_triage:
        cost += llm_triage_cost
    if case.recovered:
        cost += case.amount * gateway_fee_rate
    return round(cost, 4)


def build_report(
    cases: list[CaseRecord],
    sms_cost: float = SMS_COST,
    llm_triage_cost: float = LLM_TRIAGE_COST,
    gateway_fee_rate: float = GATEWAY_FEE_RATE,
) -> dict:
    """
    Compute the full metrics dict for a batch of CaseRecords.

    `sms_cost` / `llm_triage_cost` / `gateway_fee_rate` default to the
    module constants but can be overridden per call -- this is what
    lets a "what-if" caller (see pipeline.simulate_batch) recompute
    the same batch's economics under different cost assumptions
    without mutating any global state.
    """
    revenue_at_risk = sum(c.amount for c in cases)
    gross_recovered = sum(c.amount for c in cases if c.recovered)
    total_cost = sum(_action_cost(c, sms_cost, llm_triage_cost, gateway_fee_rate) for c in cases)
    net_preserved = round(gross_recovered - total_cost, 2)

    recoverable_denominator = len(cases) if cases else 1
    net_recovery_rate = round((len(
        [c for c in cases if c.recovered]
    ) / recoverable_denominator) * 100, 2)

    pct_of_at_risk_recovered = round(
        (gross_recovered / revenue_at_risk) * 100 if revenue_at_risk else 0.0, 2
    )

    # The confirmed-recovery funnel: links_issued_count is a TECHNICAL
    # count (we successfully created a payment link) and is always >=
    # the number of those that were actually paid on. Reporting both,
    # plus the conversion rate between them, is what keeps "recovered"
    # honest -- see adapters/confirmation.py for the model behind the
    # split, and sequencer.py for where it's enforced.
    links_issued_count = len([c for c in cases if c.link_issued])
    links_issued_amount = round(sum(c.amount for c in cases if c.link_issued), 2)
    link_issued_not_confirmed_count = len(
        [c for c in cases if c.link_issued and not c.recovered]
    )
    link_confirmation_rate_pct = round(
        (len([c for c in cases if c.link_issued and c.recovered]) / links_issued_count) * 100
        if links_issued_count else 0.0,
        2,
    )

    by_bucket: dict[str, dict] = {}
    for c in cases:
        b = by_bucket.setdefault(
            c.bucket, {"total": 0, "recovered": 0, "gross_amount_recovered": 0.0}
        )
        b["total"] += 1
        if c.recovered:
            b["recovered"] += 1
            b["gross_amount_recovered"] += c.amount

    for bucket, stats in by_bucket.items():
        stats["recovery_rate_pct"] = round(
            (stats["recovered"] / stats["total"]) * 100 if stats["total"] else 0.0, 2
        )
        stats["gross_amount_recovered"] = round(stats["gross_amount_recovered"], 2)

    # Cohort breakdown by customer LTV tier -- lets you see whether
    # the recovery flow is actually working harder/better for
    # higher-value customers, not just whether it works overall.
    by_ltv_tier: dict[str, dict] = {}
    for c in cases:
        tier = c.ltv_tier or "UNKNOWN"
        t = by_ltv_tier.setdefault(
            tier, {"total": 0, "recovered": 0, "gross_amount_recovered": 0.0}
        )
        t["total"] += 1
        if c.recovered:
            t["recovered"] += 1
            t["gross_amount_recovered"] += c.amount

    for tier, stats in by_ltv_tier.items():
        stats["recovery_rate_pct"] = round(
            (stats["recovered"] / stats["total"]) * 100 if stats["total"] else 0.0, 2
        )
        stats["gross_amount_recovered"] = round(stats["gross_amount_recovered"], 2)

    blocked_reasons: dict[str, int] = {}
    stopped_count = 0
    for c in cases:
        if c.final_outcome == "BLOCKED" and c.stopping_rule_hit:
            blocked_reasons[c.stopping_rule_hit] = blocked_reasons.get(c.stopping_rule_hit, 0) + 1
        elif c.final_outcome == "UNRECOVERED":
            stopped_count += 1

    return {
        "revenue_at_risk": round(revenue_at_risk, 2),
        "gross_revenue_recovered": round(gross_recovered, 2),
        "pct_of_at_risk_recovered": pct_of_at_risk_recovered,
        "net_revenue_preserved": net_preserved,
        "total_action_cost": round(total_cost, 2),
        "net_recovery_rate_pct": net_recovery_rate,
        "links_issued_count": links_issued_count,
        "links_issued_amount": links_issued_amount,
        "link_issued_not_confirmed_count": link_issued_not_confirmed_count,
        "link_confirmation_rate_pct": link_confirmation_rate_pct,
        "recovery_by_bucket": by_bucket,
        "recovery_by_ltv_tier": by_ltv_tier,
        "blocked_case_count": sum(blocked_reasons.values()),
        "blocked_reasons": blocked_reasons,
        "stopped_unrecovered_count": stopped_count,
        "total_cases": len(cases),
    }
