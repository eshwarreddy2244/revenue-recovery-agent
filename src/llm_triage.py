"""
llm_triage.py
The ONLY module in this codebase that calls an LLM (Groq, llama-3.3-70b).

Scoped to exactly two non-safety-critical jobs, per the project spec:
  1. parse_freetext_reason() -- best-guess a known failure_reason_code
     from an ambiguous, free-text description (e.g. what a support
     agent typed, or an unstructured webhook payload field).
  2. generate_dispute_copy() -- templated Hinglish customer-facing
     copy for the dispute-halt notification. Wording only, never a
     decision.

Hard rules this module follows:
  - Never called from diagnose.py, policy_gate.py, or sequencer.py.
    Those stay deterministic and fully testable without a network
    call or an API key.
  - Every function degrades gracefully: no GROQ_API_KEY, a network
    error, or a malformed LLM response all fall back to a safe
    default (None / a plain-English template) rather than raising.
  - parse_freetext_reason() only ever returns a code that already
    exists in diagnose.REASON_CODE_MAP, or None. It cannot invent a
    new bucket -- the deterministic mapper in diagnose.py is always
    the final authority on which bucket a code belongs to.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

MODEL = "llama-3.3-70b-versatile"


def _get_client():
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return None
    try:
        from groq import Groq

        return Groq(api_key=api_key)
    except Exception as exc:  # pragma: no cover - import/env issues only
        logger.warning("Could not initialize Groq client: %s", exc)
        return None


def parse_freetext_reason(free_text: str, known_codes: list[str]) -> str | None:
    """
    Best-guess a known failure_reason_code from an ambiguous free-text
    description. Returns None (never raises) if the LLM is
    unavailable, errors, or returns something that isn't one of the
    known codes -- diagnose.py will classify None as UNKNOWN, which is
    exactly the safe fallback we want.
    """
    client = _get_client()
    if client is None or not free_text or not free_text.strip():
        return None

    prompt = (
        "You classify a free-text payment failure description into exactly "
        "one of these codes, or 'NONE' if none fit:\n"
        f"{', '.join(known_codes)}\n\n"
        f"Description: {free_text.strip()}\n\n"
        "Reply with ONLY the code, nothing else."
    )

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=20,
            temperature=0,
        )
        raw = (response.choices[0].message.content or "").strip().upper()
        raw = raw.split()[0] if raw else ""
        if raw in known_codes:
            return raw
        return None
    except Exception as exc:
        logger.warning("parse_freetext_reason failed, falling back to None: %s", exc)
        return None


def generate_dispute_copy(payment_id: str, amount: float, currency: str = "INR") -> str:
    """
    Generate a short, templated Hinglish message explaining to a
    customer that their payment recovery outreach has been paused
    because a dispute was raised. Always returns a usable string --
    falls back to a plain-English template if the LLM is unavailable
    or errors.
    """
    fallback = (
        f"Aapke payment ({payment_id}, {currency} {amount:,.2f}) par ek dispute "
        f"raise hua hai, isliye hum abhi further follow-up nahi kar rahe. "
        f"Hamari team dispute resolve hone ke baad aapse contact karegi."
    )

    client = _get_client()
    if client is None:
        return fallback

    prompt = (
        "Write a short (2-3 sentence), polite Hinglish (Hindi-English mix, "
        "Latin script) message to an Indian customer explaining that we've "
        "paused automated follow-up on their payment because a dispute was "
        f"raised, and our team will reach out once it's resolved. Payment ID: "
        f"{payment_id}, amount: {currency} {amount:,.2f}. Do not invent any "
        "other details. Reply with ONLY the message."
    )

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150,
            temperature=0.4,
        )
        text = (response.choices[0].message.content or "").strip()
        return text if text else fallback
    except Exception as exc:
        logger.warning("generate_dispute_copy failed, using fallback template: %s", exc)
        return fallback
