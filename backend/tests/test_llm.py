"""Tests for the tiered LLM layer: MockBackend determinism, AnthropicBackend
retry-on-validation-error logic, and make_backend's mock/real selection.

No network calls and no real API key anywhere here -- AnthropicBackend's
chat-model construction is monkeypatched out where it's exercised, and
make_backend's "real" branch is only checked for *which class* it picks,
never actually invoked.
"""

from __future__ import annotations

import logging
from typing import get_args

import pytest

from brightspace_agent.agents.llm import (
    AnthropicBackend,
    LLMCallError,
    MockBackend,
    make_backend,
)
from brightspace_agent.agents.schemas import DocSummary
from brightspace_agent.config import Settings


@pytest.fixture(autouse=True)
def _no_ambient_anthropic_env(monkeypatch):
    """Make sure a real key/flag on the host running these tests can't leak
    in and change make_backend's decision."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("BSA_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("BSA_MOCK_LLM", raising=False)


# --------------------------------------------------------------------------
# MockBackend determinism
# --------------------------------------------------------------------------


def test_mock_backend_same_input_returns_identical_doc_summary():
    backend = MockBackend()

    first, usage_first = backend.structured_call(
        DocSummary, system="sys", user="Lecture on graph traversal, BFS and DFS.", tier="fast"
    )
    second, usage_second = backend.structured_call(
        DocSummary, system="sys", user="Lecture on graph traversal, BFS and DFS.", tier="fast"
    )

    assert first == second
    assert usage_first == usage_second


def test_mock_backend_different_input_returns_different_summary():
    backend = MockBackend()

    first, _ = backend.structured_call(
        DocSummary, system="sys", user="Lecture on graph traversal, BFS and DFS.", tier="fast"
    )
    second, _ = backend.structured_call(
        DocSummary, system="sys", user="Assignment on dynamic programming and memoization.", tier="fast"
    )

    assert first.summary != second.summary


def test_mock_backend_zero_cost_and_tokens():
    backend = MockBackend()

    _, usage = backend.structured_call(DocSummary, system="sys", user="anything", tier="fast")

    assert usage["input_tokens"] == 0
    assert usage["output_tokens"] == 0
    assert usage["est_cost_usd"] == 0.0


def test_mock_backend_output_is_valid_doc_summary_instance():
    backend = MockBackend()

    parsed, _ = backend.structured_call(DocSummary, system="sys", user="Some material text here", tier="fast")

    assert isinstance(parsed, DocSummary)
    assert parsed.doc_kind_guess in get_args(DocSummary.model_fields["doc_kind_guess"].annotation)
    assert len(parsed.key_terms) <= 10


# --------------------------------------------------------------------------
# AnthropicBackend retry-on-validation-error logic
# --------------------------------------------------------------------------


class _FakeAIMessage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.usage_metadata = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }


class _FakeStructuredRunnable:
    """Stands in for `chat.with_structured_output(schema, include_raw=True)`:
    a `.invoke()` that returns pre-scripted include_raw-style dicts, one per
    call, counts how many times it was called, and records the messages
    each call received (so a retry can be asserted to actually carry the
    validation error, not just re-ask the same question)."""

    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.call_count = 0
        self.call_args_list: list[list] = []

    def invoke(self, messages):
        self.call_count += 1
        self.call_args_list.append(messages)
        return self._responses.pop(0)


class _FakeChatModel:
    def __init__(self, structured: _FakeStructuredRunnable) -> None:
        self._structured = structured

    def with_structured_output(self, schema, include_raw=False):
        assert include_raw is True
        return self._structured


def _valid_response() -> dict:
    return {
        "raw": _FakeAIMessage(input_tokens=100, output_tokens=50),
        "parsed": DocSummary(summary="A valid mock summary.", doc_kind_guess="document", key_terms=["x"]),
        "parsing_error": None,
    }


def _invalid_response() -> dict:
    return {
        "raw": _FakeAIMessage(input_tokens=100, output_tokens=10),
        "parsed": None,
        "parsing_error": ValueError("key_terms: field required"),
    }


def _backend_with_fake_chat(monkeypatch, structured: _FakeStructuredRunnable) -> AnthropicBackend:
    backend = AnthropicBackend(Settings(anthropic_api_key="fake-key-not-used"))
    fake_chat = _FakeChatModel(structured)
    monkeypatch.setattr(backend, "_get_chat_model", lambda tier: fake_chat)
    return backend


def test_anthropic_backend_retries_once_on_validation_error_then_succeeds(monkeypatch):
    structured = _FakeStructuredRunnable([_invalid_response(), _valid_response()])
    backend = _backend_with_fake_chat(monkeypatch, structured)

    parsed, usage = backend.structured_call(DocSummary, system="sys", user="user text", tier="fast")

    assert structured.call_count == 2  # initial attempt + exactly one retry
    assert isinstance(parsed, DocSummary)
    assert parsed.summary == "A valid mock summary."
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 50
    assert usage["model"] == Settings().fast_model

    # The retry must actually surface the validation error to the model,
    # not silently re-ask the identical question and hope for a different
    # answer.
    assert len(structured.call_args_list) == 2
    retry_messages = structured.call_args_list[1]
    retry_human_content = retry_messages[-1].content
    assert "key_terms: field required" in retry_human_content


def test_anthropic_backend_raises_llm_call_error_when_always_invalid(monkeypatch):
    structured = _FakeStructuredRunnable([_invalid_response(), _invalid_response()])
    backend = _backend_with_fake_chat(monkeypatch, structured)

    with pytest.raises(LLMCallError):
        backend.structured_call(DocSummary, system="sys", user="user text", tier="fast")

    assert structured.call_count == 2  # does not retry more than once


def test_anthropic_backend_succeeds_first_try_without_retry(monkeypatch):
    structured = _FakeStructuredRunnable([_valid_response()])
    backend = _backend_with_fake_chat(monkeypatch, structured)

    parsed, _ = backend.structured_call(DocSummary, system="sys", user="user text", tier="fast")

    assert structured.call_count == 1
    assert isinstance(parsed, DocSummary)


def test_anthropic_backend_model_for_tier_matches_settings():
    backend = AnthropicBackend(Settings(anthropic_api_key="fake-key-not-used"))

    assert backend.model_for_tier("fast") == Settings().fast_model
    assert backend.model_for_tier("smart") == Settings().smart_model


# --------------------------------------------------------------------------
# make_backend selection
# --------------------------------------------------------------------------


def test_make_backend_returns_mock_when_no_key_and_no_mock_flag(caplog):
    settings = Settings()  # no key, mock_llm defaults False (env vars cleared by fixture)

    with caplog.at_level(logging.INFO):
        backend = make_backend(settings)

    assert isinstance(backend, MockBackend)
    assert any("mock" in record.message.lower() for record in caplog.records)


def test_make_backend_returns_mock_when_mock_flag_set_even_with_key():
    settings = Settings(anthropic_api_key="real-looking-key", mock_llm=True)

    backend = make_backend(settings)

    assert isinstance(backend, MockBackend)


def test_make_backend_returns_anthropic_backend_when_key_present():
    settings = Settings(anthropic_api_key="real-looking-key")

    backend = make_backend(settings)

    assert isinstance(backend, AnthropicBackend)
