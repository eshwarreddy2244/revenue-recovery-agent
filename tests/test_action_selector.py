import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.action_selector import FAST_TRACK_PLAN, STANDARD_PLAN, explain_plan, get_action_plan  # noqa: E402


def test_high_ltv_non_behavioral_gets_fast_tracked():
    plan = get_action_plan("HIGH", "FINANCIAL")
    assert plan == FAST_TRACK_PLAN


def test_high_ltv_behavioral_still_gets_standard_plan():
    plan = get_action_plan("HIGH", "BEHAVIORAL")
    assert plan == STANDARD_PLAN


def test_low_and_medium_ltv_always_get_standard_plan():
    assert get_action_plan("LOW", "FINANCIAL") == STANDARD_PLAN
    assert get_action_plan("MEDIUM", "TECHNICAL") == STANDARD_PLAN


def test_unknown_or_missing_ltv_defaults_to_standard_plan():
    assert get_action_plan(None, "FINANCIAL") == STANDARD_PLAN
    assert get_action_plan("SOMETHING_UNEXPECTED", "TECHNICAL") == STANDARD_PLAN


def test_returned_plan_is_a_copy_not_a_shared_mutable_list():
    plan_a = get_action_plan("HIGH", "FINANCIAL")
    plan_a.append("mutated")
    plan_b = get_action_plan("HIGH", "FINANCIAL")
    assert plan_b == FAST_TRACK_PLAN
    assert "mutated" not in plan_b


def test_explain_plan_mentions_the_tier_and_bucket():
    text = explain_plan("HIGH", "FINANCIAL")
    assert "HIGH" in text
    assert "FINANCIAL" in text
