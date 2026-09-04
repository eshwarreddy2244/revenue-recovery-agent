import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.escalation import (  # noqa: E402
    ALL_TEAM_MEMBERS,
    ROUTE_TEAM_ROSTER,
    build_ticket,
    should_escalate,
    simulate_daily_triage,
)


def test_recovered_never_escalates():
    assert should_escalate("RECOVERED") is False


def test_blocked_and_unrecovered_both_escalate():
    assert should_escalate("BLOCKED") is True
    assert should_escalate("UNRECOVERED") is True


def test_opt_out_ticket_routes_to_none_but_is_still_logged():
    ticket = build_ticket("pay_1", "OPT_OUT", amount=1000.0, ltv_tier="HIGH")
    assert ticket.route == "NONE"
    assert ticket.priority_score == 0
    assert "opted out" in ticket.note.lower()


def test_dispute_always_routes_to_dispute_resolution():
    for tier in ("HIGH", "MEDIUM", "LOW", None):
        ticket = build_ticket("pay_1", "DISPUTE_RAISED", amount=500.0, ltv_tier=tier)
        assert ticket.route == "DISPUTE_RESOLUTION"
        assert ticket.priority_score > 0


def test_dispute_priority_scales_with_tier():
    high = build_ticket("pay_1", "DISPUTE_RAISED", amount=500.0, ltv_tier="HIGH")
    medium = build_ticket("pay_1", "DISPUTE_RAISED", amount=500.0, ltv_tier="MEDIUM")
    low = build_ticket("pay_1", "DISPUTE_RAISED", amount=500.0, ltv_tier="LOW")
    assert high.priority_score > medium.priority_score > low.priority_score


def test_dispute_high_amount_forces_high_priority_regardless_of_tier():
    ticket = build_ticket("pay_1", "DISPUTE_RAISED", amount=25000.0, ltv_tier="LOW")
    assert ticket.to_dict()["priority"] == "HIGH"


def test_touch_cap_escalates_for_high_ltv_only():
    high = build_ticket("pay_1", "TOUCH_CAP_EXCEEDED", amount=500.0, ltv_tier="HIGH")
    medium = build_ticket("pay_1", "TOUCH_CAP_EXCEEDED", amount=500.0, ltv_tier="MEDIUM")
    low = build_ticket("pay_1", "TOUCH_CAP_EXCEEDED", amount=500.0, ltv_tier="LOW")
    assert high.route == "RETENTION"
    assert medium.route == "NONE"
    assert low.route == "NONE"


def test_touch_cap_escalates_for_high_value_amount_regardless_of_tier():
    ticket = build_ticket("pay_1", "TOUCH_CAP_EXCEEDED", amount=20000.0, ltv_tier="LOW")
    assert ticket.route == "RETENTION"


def test_discount_cap_exceeded_never_routes_anywhere():
    ticket = build_ticket("pay_1", "DISCOUNT_CAP_EXCEEDED", amount=99999.0, ltv_tier="HIGH")
    assert ticket.route == "NONE"


def test_unrecovered_always_routes_to_retention():
    for tier in ("HIGH", "MEDIUM", "LOW", None):
        ticket = build_ticket("pay_1", "UNRECOVERED", amount=500.0, ltv_tier=tier)
        assert ticket.route == "RETENTION"


def test_unknown_reason_defaults_to_no_route():
    ticket = build_ticket("pay_1", "SOME_NEW_RULE_NOT_YET_DEFINED", amount=99999.0, ltv_tier="HIGH")
    assert ticket.route == "NONE"
    assert "Unrecognized" in ticket.note


def test_ticket_is_deterministic():
    a = build_ticket("pay_1", "DISPUTE_RAISED", amount=1000.0, ltv_tier="MEDIUM")
    b = build_ticket("pay_1", "DISPUTE_RAISED", amount=1000.0, ltv_tier="MEDIUM")
    assert a == b


def test_to_dict_includes_all_fields():
    ticket = build_ticket("pay_42", "DISPUTE_RAISED", amount=1234.5, ltv_tier="HIGH")
    d = ticket.to_dict()
    assert d["payment_id"] == "pay_42"
    assert d["reason"] == "DISPUTE_RAISED"
    assert d["route"] == "DISPUTE_RESOLUTION"
    assert d["priority"] == "HIGH"
    assert d["amount"] == 1234.5
    assert d["ltv_tier"] == "HIGH"


def test_route_team_roster_covers_every_actionable_route():
    """
    Every route build_ticket can actually produce (other than "NONE",
    which is deliberately not actionable -- see build_ticket's
    docstring) must have at least one person on its roster, or the
    dashboard's assignment dropdown would offer an empty list for a
    ticket someone is supposed to be able to claim.
    """
    assert ROUTE_TEAM_ROSTER["NONE"] == []
    for route in ("DISPUTE_RESOLUTION", "RETENTION"):
        assert len(ROUTE_TEAM_ROSTER[route]) > 0


def test_all_team_members_is_deduplicated_union_of_rosters():
    expected = set()
    for names in ROUTE_TEAM_ROSTER.values():
        expected.update(names)
    assert set(ALL_TEAM_MEMBERS) == expected
    assert len(ALL_TEAM_MEMBERS) == len(set(ALL_TEAM_MEMBERS))  # no duplicates


def _ticket(id_, priority_score):
    return {"id": id_, "priority_score": priority_score}


def test_simulate_daily_triage_is_deterministic_with_seeded_rng():
    import random as random_module

    tickets = [_ticket(1, 3), _ticket(2, 2), _ticket(3, 1), _ticket(4, 0)]
    result_a = simulate_daily_triage(tickets, rng=random_module.Random(7))
    result_b = simulate_daily_triage(tickets, rng=random_module.Random(7))
    assert result_a == result_b


def test_simulate_daily_triage_returns_only_ids_from_input():
    import random as random_module

    tickets = [_ticket(1, 3), _ticket(2, 2), _ticket(3, 1)]
    resolved = simulate_daily_triage(tickets, rng=random_module.Random(1))
    assert set(resolved).issubset({1, 2, 3})


def test_simulate_daily_triage_empty_input_returns_empty():
    import random as random_module

    assert simulate_daily_triage([], rng=random_module.Random(1)) == []


def test_simulate_daily_triage_higher_priority_resolves_more_often_on_average():
    """
    Not a single deterministic assertion (this is genuinely
    probabilistic) -- run many trials and check the aggregate
    resolution rate for HIGH-priority tickets exceeds LOW-priority
    ones by a wide margin, consistent with DAILY_RESOLUTION_PROBABILITY.
    """
    import random as random_module

    rng = random_module.Random(99)
    high_resolved = 0
    low_resolved = 0
    trials = 500
    for _ in range(trials):
        resolved = simulate_daily_triage([_ticket(1, 3), _ticket(2, 1)], rng=rng)
        if 1 in resolved:
            high_resolved += 1
        if 2 in resolved:
            low_resolved += 1
    assert high_resolved > low_resolved


def test_simulate_daily_triage_unknown_priority_uses_default_probability():
    import random as random_module

    tickets = [{"id": 1, "priority_score": 99}]  # not a key in DAILY_RESOLUTION_PROBABILITY
    # Should not raise, and should use DEFAULT_DAILY_RESOLUTION_PROBABILITY.
    result = simulate_daily_triage(tickets, rng=random_module.Random(1))
    assert isinstance(result, list)
