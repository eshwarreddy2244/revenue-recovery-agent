"""
scripts/check_razorpay_connection.py
Standalone sanity check for your Razorpay TEST-mode credentials.

Run this after filling in .env to confirm RAZORPAY_KEY_ID /
RAZORPAY_KEY_SECRET actually work, before demoing the full app:

    python -m scripts.check_razorpay_connection
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from src.adapters.payment_link import create_payment_link  # noqa: E402


def main() -> int:
    key_id = os.environ.get("RAZORPAY_KEY_ID")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET")

    if not key_id or not key_secret:
        print("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set.")
        print("Copy .env.example to .env and fill in your TEST-mode keys.")
        return 1

    if not key_id.startswith("rzp_test_"):
        print(f"Warning: RAZORPAY_KEY_ID '{key_id}' doesn't look like a TEST-mode key "
              f"(expected it to start with 'rzp_test_'). Refusing to proceed against a "
              f"possible LIVE key.")
        return 1

    print(f"Using key id: {key_id}")
    print("Attempting to create a Rs 1.00 test payment link...")

    result = create_payment_link(
        amount_paise=100,
        currency="INR",
        description="Connectivity check from check_razorpay_connection.py",
    )

    if result.get("success"):
        print("SUCCESS")
        print(f"  plink_id : {result['plink_id']}")
        print(f"  short_url: {result['short_url']}")
        return 0
    else:
        print("FAILED")
        print(f"  error: {result.get('error')}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
