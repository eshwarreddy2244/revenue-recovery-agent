"""
sequencer.py
Runs a temporal recovery sequence for a single diagnosed case, using
a simulated/accelerated clock -- no real waiting happens.

The DEFAULT plan is 3 steps:
  Step 1, T+0h : smart_retry (simulated)
  Step 2, T+24h: send_payment_link (real Razorpay test-mode API to
                 CREATE the link) followed by a simulated payment
                 CONFIRMATION check -- only attempted if step 1 did
                 not recover the payment
  Step 3, T+72h: stop -- mark unrecovered, no further action

Creating a payment link and the customer actually PAYING it are two
different events. This module only ever marks a case "RECOVERED" at
the send_payment_link step if BOTH happened: the link was
successfully created AND the simulated confirmation check (see
adapters/confirmation.py) says the customer completed payment on it.
A link that was issued but never confirmed still shows up as
`link_issued=True` on the result -- see report.py for how the
dashboard surfaces both numbers instead of collapsing them into one.

`action_selector.py` can propose a shorter FAST-TRACK plan (skip
smart_retry, go straight to send_payment_link) for high-value
customers on non-behavioral buckets -- see action_selector.py for
the reasoning. Whichever plan is used, the timing/stop-after-last-step
shape is the same: every proposed action is followed by an implicit
final "stop" step if nothing recovered the payment.

Before every step the policy gate (check_gate) is re-evaluated
against the event's *current* state, using whatever plan was chosen
-- the plan changes WHICH actions are offered, never whether the
gate's rules apply to them. If a step is blocked (e.g. a dispute was
raised on the event between steps), the sequence halts immediately
and the stopping_rule_hit is recorded.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Callable

from src.adapters.confirmation import simulate_confirmation
from src.policy_gate import DISCOUNT_CAP_PCT, TOUCH_CAP, GateInputError, check_gate

SmartRetryFn = Callable[..., dict]
PaymentLinkFn = Callable[..., dict]
ConfirmationFn = Callable[..., dict]

STANDARD_PLAN = ["smart_retry", "send_payment_link"]

# Time offsets for a given plan length (excluding the final "stop"
# step, which always lands one slot after the last real action).
_OFFSETS_BY_PLAN_LENGTH = {
    1: ["T+0h", "T+24h"],           # 1 action + stop
    2: ["T+0h", "T+24h", "T+72h"],  # 2 actions + stop (the original default shape)
}


@dataclass
class StepRecord:
    step: int
    timestamp_offset: str
    action: str
    gate_decision: dict | None
    outcome: str  # "RECOVERED" / "NOT_RECOVERED" / "BLOCKED" / "STOPPED"
    plink_id: str | None = None
    link_issued: bool = False
    detail: dict = field(default_factory=dict)


@dataclass
class SequenceResult:
    payment_id: str
    steps: list[StepRecord]
    recovered: bool
    stopping_rule_hit: str | None  # None if it ran its natural course
    final_outcome: str  # "RECOVERED" / "UNRECOVERED" / "BLOCKED"
    link_issued: bool = False  # a payment link was successfully CREATED,
                                # regardless of whether it was ever paid --
                                # see report.py for the issued-vs-confirmed split

    def to_dict(self) -> dict:
        return {
            "payment_id": self.payment_id,
            "recovered": self.recovered,
            "stopping_rule_hit": self.stopping_rule_hit,
            "final_outcome": self.final_outcome,
            "link_issued": self.link_issued,
            "steps": [
                {
                    "step": s.step,
                    "timestamp_offset": s.timestamp_offset,
                    "action": s.action,
                    "gate_decision": s.gate_decision,
                    "outcome": s.outcome,
                    "plink_id": s.plink_id,
                    "link_issued": s.link_issued,
                    "detail": s.detail,
                }
                for s in self.steps
            ],
        }


def _run_smart_retry(event, diagnosis, rng, *, smart_retry_fn, payment_link_fn, confirmation_fn):
    retry_result = smart_retry_fn(diagnosis.get("bucket"), rng=rng)
    success = bool(retry_result.get("success"))
    return success, retry_result, None, False


def _run_send_payment_link(event, diagnosis, rng, *, smart_retry_fn, payment_link_fn, confirmation_fn):
    payment_id = event.get("payment_id")
    amount = event.get("amount", 0)
    amount_paise = int(round(float(amount) * 100)) if isinstance(amount, (int, float)) else 0
    link_result = payment_link_fn(
        amount_paise=amount_paise,
        currency=event.get("currency", "INR"),
        description=f"Complete your payment for {payment_id}",
        customer_id=event.get("customer_id"),
        reference_id=payment_id,
    )
    link_created = bool(link_result.get("success"))
    plink_id = link_result.get("plink_id")

    if not link_created:
        # Couldn't even create the link -- no confirmation to simulate.
        detail = dict(link_result)
        detail["confirmed"] = False
        return False, detail, plink_id, False

    # The link exists. Whether the CUSTOMER actually pays on it is a
    # separate, later event -- simulate that here rather than treating
    # link creation itself as "recovered". This is the technical
    # success (link_issued) vs. business outcome (confirmed) split.
    confirmation_result = confirmation_fn(diagnosis.get("bucket"), diagnosis.get("ltv_tier"), rng=rng)
    confirmed = bool(confirmation_result.get("confirmed"))

    detail = dict(link_result)
    detail["confirmed"] = confirmed
    detail["confirmation_probability_used"] = confirmation_result.get("probability_used")

    return confirmed, detail, plink_id, True


_ACTION_RUNNERS = {
    "smart_retry": _run_smart_retry,
    "send_payment_link": _run_send_payment_link,
}


def run_sequence(
    event: dict,
    diagnosis: dict,
    smart_retry_fn: SmartRetryFn,
    payment_link_fn: PaymentLinkFn,
    rng: random.Random | None = None,
    touch_cap: int = TOUCH_CAP,
    discount_cap_pct: float = DISCOUNT_CAP_PCT,
    action_plan: list[str] | None = None,
    confirmation_fn: ConfirmationFn | None = None,
) -> SequenceResult:
    """
    Run the recovery sequence for one case.

    `event` may be mutated between steps by the caller in tests to
    simulate a dispute being raised mid-sequence; this function
    always re-reads `event` fresh before each gate check.

    `touch_cap` / `discount_cap_pct` are passed straight through to
    every check_gate call in this sequence, so a "what-if" caller can
    re-run the exact same batch under different policy limits without
    touching global state.

    `action_plan` is the ordered list of action types to attempt
    (each one of "smart_retry" / "send_payment_link"), defaulting to
    the original 2-action standard plan. See action_selector.py for
    how a caller derives this from LTV tier / bucket. Regardless of
    which plan is used, every action is gated by check_gate exactly
    like the original 3-step sequence was, and an implicit "stop"
    step always follows the last action if nothing recovered.

    `confirmation_fn` defaults to adapters.confirmation.simulate_confirmation.
    It's called only after send_payment_link successfully CREATES a
    link, to simulate whether the customer actually PAYS on it --
    that confirmation, not link creation, is what determines whether
    the step (and the case) counts as RECOVERED. See
    adapters/confirmation.py for why this split exists.
    """
    confirmation_fn = confirmation_fn or simulate_confirmation

    plan = list(action_plan) if action_plan else list(STANDARD_PLAN)
    for action in plan:
        if action not in _ACTION_RUNNERS:
            raise ValueError(f"Unknown action in action_plan: {action!r}")

    offsets = _OFFSETS_BY_PLAN_LENGTH.get(len(plan))
    if offsets is None:
        # Generic fallback for any future plan length: T+0h, then
        # +24h per subsequent slot -- keeps this function from ever
        # hard-crashing on a plan length nobody anticipated yet.
        offsets = [f"T+{i * 24}h" for i in range(len(plan) + 1)]

    steps: list[StepRecord] = []
    payment_id = event.get("payment_id")
    link_issued = False

    # Work on a shallow copy of diagnosis so we can update
    # prior_touch_count between steps without mutating the caller's
    # dict -- each step's touch count must reflect the touches
    # already made earlier in *this* sequence, or the touch cap never
    # actually caps anything within a single sequence.
    diagnosis = dict(diagnosis)

    for idx, action in enumerate(plan, start=1):
        offset = offsets[idx - 1]
        try:
            gate = check_gate(
                event, diagnosis, {"type": action, "discount_pct": 0},
                touch_cap=touch_cap, discount_cap_pct=discount_cap_pct,
            )
        except GateInputError:
            raise
        diagnosis["prior_touch_count"] = gate["touch_count_after"]

        if gate["decision"] == "BLOCK":
            steps.append(
                StepRecord(step=idx, timestamp_offset=offset, action=action, gate_decision=gate, outcome="BLOCKED")
            )
            return SequenceResult(
                payment_id=payment_id, steps=steps, recovered=False,
                stopping_rule_hit=gate["reason"], final_outcome="BLOCKED",
                link_issued=link_issued,
            )

        runner = _ACTION_RUNNERS[action]
        success, detail, plink_id, step_link_issued = runner(
            event, diagnosis, rng,
            smart_retry_fn=smart_retry_fn, payment_link_fn=payment_link_fn, confirmation_fn=confirmation_fn,
        )
        if step_link_issued:
            link_issued = True
        outcome = "RECOVERED" if success else "NOT_RECOVERED"
        steps.append(
            StepRecord(
                step=idx, timestamp_offset=offset, action=action, gate_decision=gate,
                outcome=outcome, plink_id=plink_id, link_issued=step_link_issued, detail=detail,
            )
        )

        if outcome == "RECOVERED":
            return SequenceResult(
                payment_id=payment_id, steps=steps, recovered=True,
                stopping_rule_hit=None, final_outcome="RECOVERED",
                link_issued=link_issued,
            )

    # --- Final implicit "stop" step: nothing in the plan recovered it ---
    stop_step_number = len(plan) + 1
    stop_offset = offsets[len(plan)] if len(offsets) > len(plan) else f"T+{stop_step_number * 24}h"
    try:
        gate_stop = check_gate(
            event, diagnosis, {"type": "stop", "discount_pct": 0},
            touch_cap=touch_cap, discount_cap_pct=discount_cap_pct,
        )
    except GateInputError:
        raise

    if gate_stop["decision"] == "BLOCK":
        steps.append(
            StepRecord(step=stop_step_number, timestamp_offset=stop_offset, action="stop", gate_decision=gate_stop, outcome="BLOCKED")
        )
        return SequenceResult(
            payment_id=payment_id, steps=steps, recovered=False,
            stopping_rule_hit=gate_stop["reason"], final_outcome="BLOCKED",
            link_issued=link_issued,
        )

    steps.append(
        StepRecord(step=stop_step_number, timestamp_offset=stop_offset, action="stop", gate_decision=gate_stop, outcome="STOPPED")
    )
    return SequenceResult(
        payment_id=payment_id, steps=steps, recovered=False,
        stopping_rule_hit=None, final_outcome="UNRECOVERED",
        link_issued=link_issued,
    )
