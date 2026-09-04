import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import llm_triage  # noqa: E402


KNOWN_CODES = [
    "INSUFFICIENT_BALANCE",
    "CARD_LIMIT_EXCEEDED",
    "BANK_DECLINED_INSUFFICIENT_FUNDS",
    "CHECKOUT_ABANDONED",
    "PAYMENT_TIMEOUT_USER_IDLE",
    "OTP_NOT_ENTERED",
    "GATEWAY_TIMEOUT",
    "EXPIRED_TOKEN",
    "NETWORK_ERROR",
]


def test_parse_freetext_reason_returns_none_without_api_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    result = llm_triage.parse_freetext_reason("card got declined, limit issue", KNOWN_CODES)
    assert result is None


def test_parse_freetext_reason_returns_none_for_empty_text(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "fake-key-for-test")
    result = llm_triage.parse_freetext_reason("", KNOWN_CODES)
    assert result is None


def test_generate_dispute_copy_falls_back_without_api_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    message = llm_triage.generate_dispute_copy("pay_test123", 1500.0)
    assert "pay_test123" in message
    assert "1,500.00" in message
    assert len(message) > 0


def test_parse_freetext_reason_rejects_out_of_vocabulary_response(monkeypatch):
    """
    If the LLM ever returns something not in the known code list,
    parse_freetext_reason must return None rather than passing the
    hallucinated value through -- diagnose.py's REASON_CODE_MAP is
    always the final authority on valid buckets.
    """

    class _FakeChoice:
        def __init__(self, content):
            self.message = type("M", (), {"content": content})()

    class _FakeResponse:
        def __init__(self, content):
            self.choices = [_FakeChoice(content)]

    class _FakeCompletions:
        def create(self, **kwargs):
            return _FakeResponse("SOME_MADE_UP_CODE_NOT_REAL")

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    monkeypatch.setenv("GROQ_API_KEY", "fake-key-for-test")
    monkeypatch.setattr(llm_triage, "_get_client", lambda: _FakeClient())

    result = llm_triage.parse_freetext_reason("something ambiguous", KNOWN_CODES)
    assert result is None


def test_parse_freetext_reason_accepts_valid_llm_response(monkeypatch):
    class _FakeChoice:
        def __init__(self, content):
            self.message = type("M", (), {"content": content})()

    class _FakeResponse:
        def __init__(self, content):
            self.choices = [_FakeChoice(content)]

    class _FakeCompletions:
        def create(self, **kwargs):
            return _FakeResponse("GATEWAY_TIMEOUT")

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    monkeypatch.setenv("GROQ_API_KEY", "fake-key-for-test")
    monkeypatch.setattr(llm_triage, "_get_client", lambda: _FakeClient())

    result = llm_triage.parse_freetext_reason("request just timed out at the gateway", KNOWN_CODES)
    assert result == "GATEWAY_TIMEOUT"


def test_llm_errors_never_raise_out_of_parse_freetext_reason(monkeypatch):
    class _ExplodingCompletions:
        def create(self, **kwargs):
            raise RuntimeError("simulated network failure")

    class _FakeChat:
        completions = _ExplodingCompletions()

    class _FakeClient:
        chat = _FakeChat()

    monkeypatch.setenv("GROQ_API_KEY", "fake-key-for-test")
    monkeypatch.setattr(llm_triage, "_get_client", lambda: _FakeClient())

    result = llm_triage.parse_freetext_reason("some text", KNOWN_CODES)
    assert result is None


def test_llm_errors_never_raise_out_of_generate_dispute_copy(monkeypatch):
    class _ExplodingCompletions:
        def create(self, **kwargs):
            raise RuntimeError("simulated network failure")

    class _FakeChat:
        completions = _ExplodingCompletions()

    class _FakeClient:
        chat = _FakeChat()

    monkeypatch.setenv("GROQ_API_KEY", "fake-key-for-test")
    monkeypatch.setattr(llm_triage, "_get_client", lambda: _FakeClient())

    message = llm_triage.generate_dispute_copy("pay_x", 100.0)
    assert "pay_x" in message
