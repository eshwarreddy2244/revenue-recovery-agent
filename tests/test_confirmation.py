import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.adapters.confirmation import simulate_confirmation  # noqa: E402


def test_returns_confirmed_and_probability_used():
    rng = random.Random(1)
    result = simulate_confirmation("TECHNICAL", "HIGH", rng=rng)
    assert "confirmed" in result
    assert "probability_used" in result
    assert isinstance(result["confirmed"], bool)


def test_technical_and_behavioral_have_higher_base_rate_than_financial():
    """
    A financial failure (insufficient balance, card limit) is modeled
    as less likely to resolve via a follow-up link than a behavioral
    or technical one, since the underlying cause often persists.
    """
    rng = random.Random(1)
    technical = simulate_confirmation("TECHNICAL", None, rng=rng)
    financial = simulate_confirmation("FINANCIAL", None, rng=rng)
    assert technical["probability_used"] > financial["probability_used"]


def test_high_ltv_tier_increases_probability_over_low():
    rng = random.Random(1)
    high = simulate_confirmation("BEHAVIORAL", "HIGH", rng=rng)
    low = simulate_confirmation("BEHAVIORAL", "LOW", rng=rng)
    assert high["probability_used"] > low["probability_used"]


def test_unknown_bucket_and_tier_fall_back_to_defaults_without_raising():
    rng = random.Random(1)
    result = simulate_confirmation("SOME_NEW_BUCKET", "SOME_NEW_TIER", rng=rng)
    assert 0.0 <= result["probability_used"] <= 1.0


def test_probability_is_always_clamped_between_zero_and_one():
    rng = random.Random(1)
    # LOW tier + FINANCIAL is the lowest combination (0.35 - 0.05 = 0.30);
    # HIGH tier + TECHNICAL is the highest (0.60 + 0.10 = 0.70) -- neither
    # should ever be able to escape [0, 1] regardless of future tuning.
    lowest = simulate_confirmation("FINANCIAL", "LOW", rng=rng)
    highest = simulate_confirmation("TECHNICAL", "HIGH", rng=rng)
    assert 0.0 <= lowest["probability_used"] <= 1.0
    assert 0.0 <= highest["probability_used"] <= 1.0


def test_deterministic_with_seeded_rng():
    result_a = simulate_confirmation("BEHAVIORAL", "MEDIUM", rng=random.Random(42))
    result_b = simulate_confirmation("BEHAVIORAL", "MEDIUM", rng=random.Random(42))
    assert result_a == result_b


def test_none_ltv_tier_uses_no_adjustment():
    rng = random.Random(1)
    result = simulate_confirmation("BEHAVIORAL", None, rng=rng)
    assert result["probability_used"] == 0.55  # base rate, no tier adjustment
