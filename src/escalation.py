"""
escalation.py
Deterministic, side-effect-free escalation routing for cases that
didn't end in a successful recovery -- either BLOCKED by the policy
gate, or UNRECOVERED after running their full action plan.

Not ending in automated recovery doesn't mean "do nothing forever."
Some of these cases warrant a human being told about them. This
module decides WHETHER a case gets a ticket at all, and if so, which
team and priority it's routed to. It NEVER overrides or reopens a
gate decision -- a BLOCK stays a BLOCK, no automated action follows
just because a ticket was raised.

Pure functions throughout: no I/O, no LLM, fully unit-testable, same
spirit as policy_gate.py.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

# Above this amount, a case escalates at HIGH priority regardless of tier.
HIGH_VALUE_AMOUNT_THRESHOLD = 20000.0

_PRIORITY_SCORE = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, None: 0}

# Who a ticket CAN be assigned to, by route -- purely a dashboard
# convenience (see app.py's Escalations tab) so the assignment dropdown
# only offers people who'd plausibly work that kind of ticket, rather
# than one flat list for every route. This has no bearing on routing
# or priority logic above; it's presentation only. A route of "NONE"
# (not actionable -- e.g. OPT_OUT) has no roster, since nobody should
# be claiming a ticket that exists purely for a compliance record.
ROUTE_TEAM_ROSTER: dict[str, list[str]] = {
    "DISPUTE_RESOLUTION": ["Ananya K.", "Farhan S."],
    "RETENTION": ["Priya M.", "Rohan D.", "Meera J."],
    "NONE": [],
}

# Flat, de-duplicated view across every route -- used where the
# dashboard needs one combined list (e.g. an "assigned to" filter that
# isn't scoped to a single route).
ALL_TEAM_MEMBERS: list[str] = sorted({name for roster in ROUTE_TEAM_ROSTER.values() for name in roster})


@dataclass
class EscalationTicket:
    payment_id: str
    reason: str
    route: str  # team name, or "NONE" if not actionable
    note: str
    priority_score: int
    amount: float
    ltv_tier: str | None

    def to_dict(self) -> dict:
        return {
            "payment_id": self.payment_id,
            "reason": self.reason,
            "route": self.route,
            "note": self.note,
            "priority_score": self.priority_score,
            "priority": {3: "HIGH", 2: "MEDIUM", 1: "LOW", 0: "NONE"}[self.priority_score],
            "amount": self.amount,
            "ltv_tier": self.ltv_tier,
        }


def should_escalate(final_outcome: str) -> bool:
    """
    A case gets a ticket (of some kind -- possibly a non-actionable
    one, see build_ticket) whenever it did NOT end in automated
    recovery. RECOVERED cases never generate a ticket.
    """
    return final_outcome in ("BLOCKED", "UNRECOVERED")


def _route_and_priority(reason: str, ltv_tier: str | None, amount: float) -> tuple[str, str | None, str]:
    """Returns (route, priority, note) for a given stopping reason."""
    high_value = ltv_tier == "HIGH" or amount >= HIGH_VALUE_AMOUNT_THRESHOLD

    if reason == "OPT_OUT":
        return (
            "NONE",
            None,
            "Customer opted out — no further contact via any channel. Logged for compliance only.",
        )

    if reason == "DISPUTE_RAISED":
        if high_value:
            priority = "HIGH"
        elif ltv_tier == "MEDIUM":
            priority = "MEDIUM"
        else:
            priority = "LOW"
        return (
            "DISPUTE_RESOLUTION",
            priority,
            "Dispute raised — automated recovery halted, route to dispute resolution.",
        )

    if reason == "TOUCH_CAP_EXCEEDED":
        if high_value:
            return (
                "RETENTION",
                "HIGH" if ltv_tier == "HIGH" else "MEDIUM",
                "Touch cap reached on a high-value/high-tier case — worth a human retention attempt.",
            )
        return (
            "NONE",
            None,
            "Touch cap reached — automated channel capped, no escalation for this tier.",
        )

    if reason == "DISCOUNT_CAP_EXCEEDED":
        return (
            "NONE",
            None,
            "Proposed discount exceeded policy cap — action rejected, no escalation needed.",
        )

    if reason == "UNRECOVERED":
        # Ran its full action plan, gate never blocked it, but nothing
        # recovered the payment. The automated channel is exhausted --
        # always worth a human look, priority scaled by value.
        if high_value:
            priority = "HIGH"
        elif ltv_tier == "MEDIUM":
            priority = "MEDIUM"
        else:
            priority = "LOW"
        return (
            "RETENTION",
            priority,
            "Automated recovery plan exhausted with no recovery — route to retention for manual follow-up.",
        )

    return (
        "NONE",
        None,
        f"Unrecognized stopping reason '{reason}' — no escalation rule defined, logged for review.",
    )


def build_ticket(
    payment_id: str,
    reason: str,
    amount: float,
    ltv_tier: str | None,
) -> EscalationTicket:
    """
    Build the escalation ticket for a case that didn't end in
    automated recovery. Always returns a ticket (for audit-trail
    completeness) -- `route == "NONE"` marks one that isn't actually
    actionable by a human team (e.g. OPT_OUT), as opposed to one that
    is.
    """
    route, priority, note = _route_and_priority(reason, ltv_tier, amount)
    return EscalationTicket(
        payment_id=payment_id,
        reason=reason,
        route=route,
        note=note,
        priority_score=_PRIORITY_SCORE[priority],
        amount=amount,
        ltv_tier=ltv_tier,
    )


# Modeled daily probability that a human team resolves a given OPEN/
# IN_PROGRESS ticket, keyed by priority_score. Used ONLY by
# pipeline.run_multiday_simulation, to give the dashboard's Trends and
# Escalations tabs something realistic to show across a simulated
# week without needing days of actual manual clicking through the UI.
# This is NOT used anywhere in the real single-batch flow -- resolving
# an actual ticket is something a real human does; this codebase
# should never simulate that away for a genuine run_batch() call, only
# for the explicitly-labeled multi-day demo mode.
DAILY_RESOLUTION_PROBABILITY = {3: 0.50, 2: 0.35, 1: 0.20, 0: 0.05}
DEFAULT_DAILY_RESOLUTION_PROBABILITY = 0.10


def simulate_daily_triage(open_tickets: list[dict], rng: random.Random | None = None) -> list[int]:
    """
    Given a list of currently OPEN/IN_PROGRESS escalation ticket dicts
    (each must have at least "id" and "priority_score"), simulate
    which ones a human team resolves over the course of one simulated
    day. Returns the list of ticket ids to mark RESOLVED.

    Pure and deterministic given a seeded rng. Higher-priority tickets
    are modeled as more likely to get worked first, same reasoning as
    the priority score itself -- this does not "cheat" by resolving
    tickets that are actually OPT_OUT/NONE-routed non-actionable ones
    any faster than their (low) priority score already implies.
    """
    rng = rng or random
    resolved_ids = []
    for ticket in open_tickets:
        probability = DAILY_RESOLUTION_PROBABILITY.get(
            ticket.get("priority_score"), DEFAULT_DAILY_RESOLUTION_PROBABILITY
        )
        if rng.random() < probability:
            resolved_ids.append(ticket["id"])
    return resolved_ids
