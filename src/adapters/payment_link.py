"""
adapters/payment_link.py
REAL adapter — calls the actual Razorpay test-mode SDK to create a
payment link. Keys come from environment variables only; never
hardcode credentials here.

Any API error is caught and logged rather than propagated, so a
single failed link creation never crashes the batch run.
"""

from __future__ import annotations

import logging
import os

import razorpay

logger = logging.getLogger(__name__)


def _get_client() -> "razorpay.Client":
    key_id = os.environ.get("RAZORPAY_KEY_ID")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret:
        raise RuntimeError(
            "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET not set in environment. "
            "Copy .env.example to .env and fill in your Razorpay TEST mode keys."
        )
    return razorpay.Client(auth=(key_id, key_secret))


def create_payment_link(
    amount_paise: int,
    currency: str,
    description: str,
    customer_id: str | None = None,
    reference_id: str | None = None,
) -> dict:
    """
    Create a real Razorpay test-mode payment link.

    Returns on success:
        {"success": True, "plink_id": "plink_xxx", "short_url": "...",
         "simulated": False}

    Returns on failure (never raises out of this function):
        {"success": False, "error": "<message>", "simulated": False}
    """
    try:
        client = _get_client()
        payload = {
            "amount": amount_paise,
            "currency": currency,
            "description": description,
            "accept_partial": False,
            "reminder_enable": True,
            "notes": {"source": "ai-revenue-recovery-agent"},
        }
        if reference_id:
            payload["reference_id"] = reference_id

        response = client.payment_link.create(payload)
        return {
            "success": True,
            "plink_id": response.get("id"),
            "short_url": response.get("short_url"),
            "simulated": False,
        }
    except Exception as exc:  # razorpay.errors.* and network errors alike
        logger.warning("payment_link.create failed: %s", exc)
        return {
            "success": False,
            "error": str(exc),
            "simulated": False,
        }
