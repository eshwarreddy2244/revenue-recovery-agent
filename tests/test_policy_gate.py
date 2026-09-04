import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.policy_gate import GateInputError, check_gate  # noqa: E402


def _event(**overrides):
    e = {"opt_out": False, "dispute_raised": False}
    e.update(overrides)
    return e


def _diagnosis(**overrides):
    d = {"prior_touch_count": 0}
    d.update(overrides)
    return d


def _action(**overrides):
    a = {"type": "send_payment_link", "discount_pct": 0}
    a.update(overrides)
    return a


def test_opt_out_always_blocks_regardless_of_other_factors():
    result = check_gate(
        _event(opt_out=True),
        _diagnosis(prior_touch_count=0),
        _action(discount_pct=0),
    )
    assert result["decision"] == "BLOCK"
    assert result["reason"] == "OPT_OUT"


def test_dispute_always_blocks():
    result = check_gate(_event(dispute_raised=True), _diagnosis(), _action())
    assert result["decision"] == "BLOCK"
    assert result["reason"] == "DISPUTE_RAISED"


def test_touch_count_boundary_two_allows():
    result = check_gate(_event(), _diagnosis(prior_touch_count=2), _action())
    assert result["decision"] == "ALLOW"


def test_touch_count_boundary_three_blocks():
    result = check_gate(_event(), _diagnosis(prior_touch_count=3), _action())
    assert result["decision"] == "BLOCK"
    assert result["reason"] == "TOUCH_CAP_EXCEEDED"


def test_discount_boundary_five_allows():
    result = check_gate(_event(), _diagnosis(), _action(discount_pct=5))
    assert result["decision"] == "ALLOW"


def test_discount_boundary_six_blocks():
    result = check_gate(_event(), _diagnosis(), _action(discount_pct=6))
    assert result["decision"] == "BLOCK"
    assert result["reason"] == "DISCOUNT_CAP_EXCEEDED"


def test_hard_halts_take_priority_over_soft_constraints():
    # opt_out AND touch cap exceeded AND discount cap exceeded all at once.
    result = check_gate(
        _event(opt_out=True),
        _diagnosis(prior_touch_count=5),
        _action(discount_pct=50),
    )
    assert result["decision"] == "BLOCK"
    assert result["reason"] == "OPT_OUT"

    # dispute AND touch cap exceeded.
    result2 = check_gate(
        _event(dispute_raised=True),
        _diagnosis(prior_touch_count=10),
        _action(),
    )
    assert result2["reason"] == "DISPUTE_RAISED"


def test_gate_input_error_raised_on_missing_prior_touch_count():
    with pytest.raises(GateInputError):
        check_gate(_event(), {}, _action())


def test_touch_count_after_increments():
    result = check_gate(_event(), _diagnosis(prior_touch_count=1), _action())
    assert result["touch_count_after"] == 2


def test_truthy_non_bool_opt_out_still_blocks():
    """A safety gate should err toward blocking on ambiguous truthiness."""
    result = check_gate(_event(opt_out="yes"), _diagnosis(), _action())
    assert result["decision"] == "BLOCK"
    assert result["reason"] == "OPT_OUT"


def test_truthy_non_bool_dispute_still_blocks():
    result = check_gate(_event(dispute_raised=1), _diagnosis(), _action())
    assert result["decision"] == "BLOCK"
    assert result["reason"] == "DISPUTE_RAISED"


def test_falsy_opt_out_values_do_not_block():
    for falsy in (False, None, 0, ""):
        result = check_gate(_event(opt_out=falsy), _diagnosis(), _action())
        assert result["decision"] == "ALLOW"


def test_malformed_event_raises_gate_input_error():
    with pytest.raises(GateInputError):
        check_gate("not a dict", _diagnosis(), _action())


def test_malformed_diagnosis_raises_gate_input_error():
    with pytest.raises(GateInputError):
        check_gate(_event(), "not a dict", _action())


def test_malformed_proposed_action_raises_gate_input_error():
    with pytest.raises(GateInputError):
        check_gate(_event(), _diagnosis(), "not a dict")


def test_non_numeric_discount_pct_raises_gate_input_error():
    with pytest.raises(GateInputError):
        check_gate(_event(), _diagnosis(), _action(discount_pct="a lot"))


def test_missing_discount_pct_defaults_to_zero_and_allows():
    result = check_gate(_event(), _diagnosis(), {"type": "smart_retry"})
    assert result["decision"] == "ALLOW"


def test_bool_prior_touch_count_raises_gate_input_error():
    """bool is a subclass of int in Python — must not silently pass as a touch count."""
    with pytest.raises(GateInputError):
        check_gate(_event(), _diagnosis(prior_touch_count=True), _action())


def test_configurable_touch_cap_overrides_default():
    # Default cap is 3; prior_touch_count=1 would normally allow.
    # With a custom cap of 1, it should now block.
    result = check_gate(_event(), _diagnosis(prior_touch_count=1), _action(), touch_cap=1)
    assert result["decision"] == "BLOCK"
    assert result["reason"] == "TOUCH_CAP_EXCEEDED"


def test_configurable_discount_cap_overrides_default():
    # Default discount cap is 5; discount_pct=4 would normally allow.
    # With a custom cap of 3, it should now block.
    result = check_gate(_event(), _diagnosis(), _action(discount_pct=4), discount_cap_pct=3)
    assert result["decision"] == "BLOCK"
    assert result["reason"] == "DISCOUNT_CAP_EXCEEDED"


def test_default_caps_unchanged_when_not_specified():
    result = check_gate(_event(), _diagnosis(prior_touch_count=2), _action(discount_pct=5))
    assert result["decision"] == "ALLOW"


def test_negative_touch_cap_raises_gate_input_error():
    with pytest.raises(GateInputError):
        check_gate(_event(), _diagnosis(), _action(), touch_cap=-1)


def test_negative_discount_cap_raises_gate_input_error():
    with pytest.raises(GateInputError):
        check_gate(_event(), _diagnosis(), _action(), discount_cap_pct=-1)
