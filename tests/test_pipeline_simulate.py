import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.pipeline import simulate_batch  # noqa: E402
from src.audit import query_batch, query_batch_run_history  # noqa: E402


def _events():
    return [
        {
            "payment_id": f"pay_sim_{i}",
            "amount": 1000.0 + i,
            "currency": "INR",
            "failure_reason_code": "GATEWAY_TIMEOUT",
            "customer_id": f"cust_{i}",
            "customer_ltv_tier": "MEDIUM",
            "retry_count": 0,
            "opt_out": False,
            "dispute_raised": False,
        }
        for i in range(20)
    ]


def test_simulate_batch_returns_report_and_invalid_count():
    result = simulate_batch(
        _events(), touch_cap=3, discount_cap_pct=5,
        sms_cost=0.20, llm_triage_cost=0.05, gateway_fee_rate=0.02,
    )
    assert "report" in result
    assert "invalid_count" in result
    assert result["report"]["total_cases"] == 20


def test_simulate_batch_never_touches_audit_log(tmp_path, monkeypatch):
    import src.audit as audit_module
    monkeypatch.setattr(audit_module, "DB_PATH", tmp_path / "isolated_test.db")

    before = query_batch(tmp_path / "isolated_test.db") if (tmp_path / "isolated_test.db").exists() else []
    simulate_batch(
        _events(), touch_cap=3, discount_cap_pct=5,
        sms_cost=0.20, llm_triage_cost=0.05, gateway_fee_rate=0.02,
    )
    # simulate_batch uses no DB connection at all -- the isolated path
    # should still not exist afterward, proving no write happened.
    assert not (tmp_path / "isolated_test.db").exists()


def test_lower_touch_cap_blocks_more_cases():
    events = _events()
    lenient = simulate_batch(events, touch_cap=10, discount_cap_pct=5, sms_cost=0.2, llm_triage_cost=0.05, gateway_fee_rate=0.02)
    strict = simulate_batch(events, touch_cap=1, discount_cap_pct=5, sms_cost=0.2, llm_triage_cost=0.05, gateway_fee_rate=0.02)
    assert strict["report"]["blocked_case_count"] >= lenient["report"]["blocked_case_count"]


def test_higher_gateway_fee_reduces_net_preserved():
    events = _events()
    cheap = simulate_batch(events, touch_cap=3, discount_cap_pct=5, sms_cost=0.2, llm_triage_cost=0.05, gateway_fee_rate=0.01)
    expensive = simulate_batch(events, touch_cap=3, discount_cap_pct=5, sms_cost=0.2, llm_triage_cost=0.05, gateway_fee_rate=0.20)
    assert expensive["report"]["net_revenue_preserved"] <= cheap["report"]["net_revenue_preserved"]
