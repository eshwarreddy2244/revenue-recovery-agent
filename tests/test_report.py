import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.report import CaseRecord, build_report  # noqa: E402


def _case(**overrides):
    c = dict(
        payment_id="pay_1",
        amount=1000.0,
        bucket="FINANCIAL",
        recovered=True,
        final_outcome="RECOVERED",
        stopping_rule_hit=None,
        steps_executed=1,
        ltv_tier="MEDIUM",
    )
    c.update(overrides)
    return CaseRecord(**c)


def test_revenue_at_risk_sums_all_case_amounts_regardless_of_outcome():
    cases = [
        _case(payment_id="a", amount=1000.0, recovered=True),
        _case(payment_id="b", amount=2000.0, recovered=False, final_outcome="UNRECOVERED"),
    ]
    report = build_report(cases)
    assert report["revenue_at_risk"] == 3000.0


def test_pct_of_at_risk_recovered_computed_correctly():
    cases = [
        _case(payment_id="a", amount=1000.0, recovered=True),
        _case(payment_id="b", amount=1000.0, recovered=False, final_outcome="UNRECOVERED"),
    ]
    report = build_report(cases)
    assert report["pct_of_at_risk_recovered"] == 50.0


def test_pct_of_at_risk_recovered_handles_empty_batch():
    report = build_report([])
    assert report["pct_of_at_risk_recovered"] == 0.0
    assert report["revenue_at_risk"] == 0.0


def test_recovery_by_ltv_tier_breaks_down_correctly():
    cases = [
        _case(payment_id="a", amount=1000.0, recovered=True, ltv_tier="HIGH"),
        _case(payment_id="b", amount=500.0, recovered=False, final_outcome="UNRECOVERED", ltv_tier="HIGH"),
        _case(payment_id="c", amount=200.0, recovered=True, ltv_tier="LOW"),
    ]
    report = build_report(cases)
    assert report["recovery_by_ltv_tier"]["HIGH"]["total"] == 2
    assert report["recovery_by_ltv_tier"]["HIGH"]["recovered"] == 1
    assert report["recovery_by_ltv_tier"]["HIGH"]["recovery_rate_pct"] == 50.0
    assert report["recovery_by_ltv_tier"]["LOW"]["recovery_rate_pct"] == 100.0


def test_missing_ltv_tier_grouped_as_unknown():
    cases = [_case(payment_id="a", ltv_tier=None)]
    report = build_report(cases)
    assert "UNKNOWN" in report["recovery_by_ltv_tier"]


def test_cost_overrides_change_net_preserved_without_touching_defaults():
    cases = [_case(payment_id="a", amount=1000.0, recovered=True, steps_executed=2)]

    default_report = build_report(cases)
    higher_cost_report = build_report(cases, sms_cost=5.0, gateway_fee_rate=0.10)

    assert higher_cost_report["total_action_cost"] > default_report["total_action_cost"]
    assert higher_cost_report["net_revenue_preserved"] < default_report["net_revenue_preserved"]
    # Revenue at risk / gross recovered are outcome-based, not cost-based --
    # overriding costs must never change them.
    assert higher_cost_report["revenue_at_risk"] == default_report["revenue_at_risk"]
    assert higher_cost_report["gross_revenue_recovered"] == default_report["gross_revenue_recovered"]


def test_zero_cost_overrides_make_net_preserved_equal_gross_recovered():
    cases = [_case(payment_id="a", amount=1000.0, recovered=True, steps_executed=3)]
    report = build_report(cases, sms_cost=0.0, llm_triage_cost=0.0, gateway_fee_rate=0.0)
    assert report["net_revenue_preserved"] == report["gross_revenue_recovered"]


def test_links_issued_count_tracks_link_issued_flag_not_recovered():
    """
    A case recovered via smart_retry (step 1) never touches
    send_payment_link, so it must NOT count toward links_issued_count
    even though it's recovered -- link issuance and recovery are
    tracked independently.
    """
    cases = [
        _case(payment_id="a", recovered=True, link_issued=False),   # recovered via retry
        _case(payment_id="b", recovered=True, link_issued=True),    # recovered via a confirmed link
        _case(payment_id="c", recovered=False, final_outcome="UNRECOVERED", link_issued=True),  # issued, never confirmed
    ]
    report = build_report(cases)
    assert report["links_issued_count"] == 2
    assert report["link_issued_not_confirmed_count"] == 1


def test_link_confirmation_rate_only_counts_confirmed_among_issued():
    cases = [
        _case(payment_id="a", amount=1000.0, recovered=True, link_issued=True),
        _case(payment_id="b", amount=1000.0, recovered=False, final_outcome="UNRECOVERED", link_issued=True),
        _case(payment_id="c", amount=1000.0, recovered=False, final_outcome="UNRECOVERED", link_issued=True),
        _case(payment_id="d", amount=1000.0, recovered=False, final_outcome="UNRECOVERED", link_issued=True),
    ]
    report = build_report(cases)
    assert report["links_issued_count"] == 4
    assert report["link_confirmation_rate_pct"] == 25.0


def test_link_confirmation_rate_zero_when_no_links_issued():
    cases = [_case(payment_id="a", recovered=True, link_issued=False)]
    report = build_report(cases)
    assert report["links_issued_count"] == 0
    assert report["link_confirmation_rate_pct"] == 0.0


def test_links_issued_amount_sums_only_issued_cases():
    cases = [
        _case(payment_id="a", amount=1000.0, recovered=True, link_issued=True),
        _case(payment_id="b", amount=5000.0, recovered=True, link_issued=False),
    ]
    report = build_report(cases)
    assert report["links_issued_amount"] == 1000.0
