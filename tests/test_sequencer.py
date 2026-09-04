import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.sequencer import run_sequence  # noqa: E402


def _event(**overrides):
    e = {
        "payment_id": "pay_seq_1",
        "amount": 1000.0,
        "currency": "INR",
        "customer_id": "cust_1",
        "opt_out": False,
        "dispute_raised": False,
    }
    e.update(overrides)
    return e


def _diagnosis(**overrides):
    d = {"bucket": "TECHNICAL", "prior_touch_count": 0}
    d.update(overrides)
    return d


def _always_succeeds_retry(bucket, rng=None):
    return {"action": "smart_retry", "success": True, "simulated": True, "probability_used": 1.0}


def _always_fails_retry(bucket, rng=None):
    return {"action": "smart_retry", "success": False, "simulated": True, "probability_used": 0.0}


def _fake_payment_link_success(**kwargs):
    return {"success": True, "plink_id": "plink_fake123", "short_url": "https://rzp.io/i/fake", "simulated": False}


def _fake_payment_link_never_called(**kwargs):
    raise AssertionError("payment_link_fn should not have been called")


def _always_confirms(bucket, ltv_tier, rng=None):
    return {"confirmed": True, "probability_used": 1.0}


def _never_confirms(bucket, ltv_tier, rng=None):
    return {"confirmed": False, "probability_used": 0.0}


def _confirmation_fn_never_called(bucket, ltv_tier, rng=None):
    raise AssertionError("confirmation_fn should not have been called")


def test_recovery_at_step1_does_not_proceed_to_step2():
    result = run_sequence(
        _event(),
        _diagnosis(),
        smart_retry_fn=_always_succeeds_retry,
        payment_link_fn=_fake_payment_link_never_called,
    )
    assert result.recovered is True
    assert result.final_outcome == "RECOVERED"
    assert len(result.steps) == 1
    assert result.steps[0].step == 1
    assert result.steps[0].outcome == "RECOVERED"


def test_dispute_injected_between_steps_halts_and_records_stopping_rule():
    event = _event()

    def retry_then_raise_dispute(bucket, rng=None):
        # Simulate a dispute getting raised on the event right after
        # step 1 fails, before step 2's gate check runs.
        event["dispute_raised"] = True
        return {"action": "smart_retry", "success": False, "simulated": True, "probability_used": 0.0}

    result = run_sequence(
        event,
        _diagnosis(),
        smart_retry_fn=retry_then_raise_dispute,
        payment_link_fn=_fake_payment_link_never_called,
    )

    assert result.final_outcome == "BLOCKED"
    assert result.stopping_rule_hit == "DISPUTE_RAISED"
    assert len(result.steps) == 2
    assert result.steps[1].outcome == "BLOCKED"


def test_case_that_never_recovers_reaches_step3_and_is_stopped():
    def _fake_payment_link_fails(**kwargs):
        return {"success": False, "error": "simulated failure", "simulated": False}

    result = run_sequence(
        _event(),
        _diagnosis(),
        smart_retry_fn=_always_fails_retry,
        payment_link_fn=_fake_payment_link_fails,
    )

    assert result.recovered is False
    assert result.final_outcome == "UNRECOVERED"
    assert result.stopping_rule_hit is None
    assert len(result.steps) == 3
    assert [s.step for s in result.steps] == [1, 2, 3]
    assert result.steps[2].action == "stop"
    assert result.steps[2].outcome == "STOPPED"


def test_recovery_at_step2_stops_before_step3():
    """
    Recovery at step 2 now requires the link to be both CREATED and
    CONFIRMED (customer actually paid) -- injecting an always-confirms
    fn isolates that from the earlier "link creation alone means
    recovered" assumption, which is exactly the gap this test used to
    paper over.
    """
    result = run_sequence(
        _event(),
        _diagnosis(),
        smart_retry_fn=_always_fails_retry,
        payment_link_fn=_fake_payment_link_success,
        confirmation_fn=_always_confirms,
    )
    assert result.recovered is True
    assert result.final_outcome == "RECOVERED"
    assert result.link_issued is True
    assert len(result.steps) == 2
    assert result.steps[1].plink_id == "plink_fake123"
    assert result.steps[1].link_issued is True
    assert result.steps[1].detail["confirmed"] is True


def test_link_issued_but_not_confirmed_is_not_recovered():
    """
    The core split this feature adds: a link can be successfully
    CREATED without the customer ever CONFIRMING payment on it. That
    case must NOT count as recovered, but must still show
    link_issued=True -- distinct from a link that was never created
    at all (see the plink_id assertion in the never-recovers test).
    """
    result = run_sequence(
        _event(),
        _diagnosis(),
        smart_retry_fn=_always_fails_retry,
        payment_link_fn=_fake_payment_link_success,
        confirmation_fn=_never_confirms,
    )
    assert result.recovered is False
    assert result.final_outcome == "UNRECOVERED"
    assert result.link_issued is True
    assert result.steps[1].outcome == "NOT_RECOVERED"
    assert result.steps[1].plink_id == "plink_fake123"
    assert result.steps[1].detail["confirmed"] is False
    # Ran the full plan through to the implicit stop step, same as any
    # other case that never recovered.
    assert len(result.steps) == 3
    assert result.steps[2].action == "stop"


def test_confirmation_fn_never_called_when_link_creation_itself_fails():
    def _fake_payment_link_fails(**kwargs):
        return {"success": False, "error": "simulated failure", "simulated": False}

    result = run_sequence(
        _event(),
        _diagnosis(),
        smart_retry_fn=_always_fails_retry,
        payment_link_fn=_fake_payment_link_fails,
        confirmation_fn=_confirmation_fn_never_called,
    )
    assert result.recovered is False
    assert result.link_issued is False
    assert result.steps[1].link_issued is False


def test_touch_count_accumulates_across_steps_and_caps_mid_sequence():
    # Starting at prior_touch_count=2: step 1 is allowed (2 < 3) and
    # bumps the running count to 3. Step 2 must then see 3 and BLOCK
    # on TOUCH_CAP_EXCEEDED — the cap has to apply within a single
    # sequence, not just at the moment the sequence starts.
    result = run_sequence(
        _event(),
        _diagnosis(prior_touch_count=2),
        smart_retry_fn=_always_fails_retry,
        payment_link_fn=_fake_payment_link_never_called,
    )
    assert result.final_outcome == "BLOCKED"
    assert result.stopping_rule_hit == "TOUCH_CAP_EXCEEDED"
    assert len(result.steps) == 2
    assert result.steps[0].outcome == "NOT_RECOVERED"
    assert result.steps[1].outcome == "BLOCKED"


def test_original_diagnosis_dict_is_not_mutated():
    diagnosis = _diagnosis(prior_touch_count=0)
    run_sequence(
        _event(),
        diagnosis,
        smart_retry_fn=_always_fails_retry,
        payment_link_fn=_fake_payment_link_success,
    )
    assert diagnosis["prior_touch_count"] == 0


def test_opt_out_blocks_before_step1_ever_executes():
    result = run_sequence(
        _event(opt_out=True),
        _diagnosis(),
        smart_retry_fn=_fake_payment_link_never_called,  # would raise if called
        payment_link_fn=_fake_payment_link_never_called,
    )
    assert result.final_outcome == "BLOCKED"
    assert result.stopping_rule_hit == "OPT_OUT"
    assert len(result.steps) == 1


def test_fast_track_plan_skips_smart_retry_entirely():
    """A fast-track (send_payment_link only) plan should never call smart_retry_fn."""
    result = run_sequence(
        _event(),
        _diagnosis(),
        smart_retry_fn=_fake_payment_link_never_called,  # raises if called
        payment_link_fn=_fake_payment_link_success,
        action_plan=["send_payment_link"],
        confirmation_fn=_always_confirms,
    )
    assert result.recovered is True
    assert result.link_issued is True
    assert len(result.steps) == 1
    assert result.steps[0].action == "send_payment_link"
    assert result.steps[0].timestamp_offset == "T+0h"


def test_fast_track_plan_reaches_stop_one_step_sooner():
    def _fake_payment_link_fails(**kwargs):
        return {"success": False, "error": "simulated failure", "simulated": False}

    result = run_sequence(
        _event(),
        _diagnosis(),
        smart_retry_fn=_fake_payment_link_never_called,
        payment_link_fn=_fake_payment_link_fails,
        action_plan=["send_payment_link"],
    )
    assert result.final_outcome == "UNRECOVERED"
    assert len(result.steps) == 2  # send_payment_link, then stop
    assert result.steps[1].action == "stop"
    assert result.steps[1].timestamp_offset == "T+24h"


def test_unknown_action_in_plan_raises_value_error():
    import pytest

    with pytest.raises(ValueError):
        run_sequence(
            _event(),
            _diagnosis(),
            smart_retry_fn=_always_succeeds_retry,
            payment_link_fn=_fake_payment_link_success,
            action_plan=["not_a_real_action"],
        )
