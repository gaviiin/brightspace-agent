"""Tests for the web-tool agent layer (agents/web.py): MockWebBackend
determinism + intent parsing, the good-domain/paywall verify heuristic, the
version<->tier tool mapping, and make_web_backend's mock/real selection.

No network calls and no real API key anywhere -- AnthropicWebBackend is only
checked for *which class* make_web_backend picks, never invoked.
"""

from __future__ import annotations

import logging

import pytest

from brightspace_agent.agents.promptfmt import (
    SECTION_CANDIDATE,
    SECTION_COURSE,
    SECTION_SEARCH_INTENT,
    SECTION_TOPIC,
)
from brightspace_agent.agents.schemas import CandidateList, Verification
from brightspace_agent.agents.web import (
    AnthropicWebBackend,
    MockWebBackend,
    _web_tools,
    make_web_backend,
)
from brightspace_agent.config import Settings


@pytest.fixture(autouse=True)
def _no_ambient_anthropic_env(monkeypatch):
    """A real key/flag on the host must not change make_web_backend's choice."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("BSA_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("BSA_MOCK_LLM", raising=False)


def _finder_prompt(intent: str, topic: str = "Breadth-First Search") -> str:
    return (
        f"{SECTION_COURSE}\n"
        "CS 2110 — Data Structures and Algorithms\n\n"
        f"{SECTION_TOPIC}\n"
        f"Name: {topic}\n"
        "Description: Graph traversal frontier by layers.\n\n"
        f"{SECTION_SEARCH_INTENT}\n"
        f"intent: {intent}\n"
        f"query: {topic} explained\n"
        "rationale: students need another route.\n"
    )


def _verify_prompt(url: str, topic: str = "Breadth-First Search") -> str:
    return (
        f"{SECTION_TOPIC}\n"
        f"Name: {topic}\n\n"
        f"{SECTION_CANDIDATE}\n"
        f"url: {url}\n"
        "title: Some Resource\n"
        "resource_type: notes\n"
        "intent: university_notes\n"
    )


# --------------------------------------------------------------------------
# MockWebBackend.find -- determinism + intent parsing
# --------------------------------------------------------------------------


def test_mock_find_is_deterministic():
    backend = MockWebBackend()
    prompt = _finder_prompt("video_lecture")

    first, usage_first = backend.find(system="sys", user=prompt, tier="smart")
    second, usage_second = backend.find(system="sys", user=prompt, tier="smart")

    assert first == second
    assert usage_first == usage_second


def test_mock_find_returns_two_or_three_candidates():
    backend = MockWebBackend()

    result, _ = backend.find(system="sys", user=_finder_prompt("video_lecture"), tier="smart")

    assert isinstance(result, CandidateList)
    assert 2 <= len(result.candidates) <= 3


def test_mock_find_parses_intent_from_prompt():
    backend = MockWebBackend()

    result, _ = backend.find(system="sys", user=_finder_prompt("worked_examples"), tier="smart")

    assert result.candidates  # non-empty
    assert all(candidate.intent == "worked_examples" for candidate in result.candidates)


def test_mock_find_url_reflects_topic():
    backend = MockWebBackend()

    result, _ = backend.find(
        system="sys", user=_finder_prompt("university_notes", topic="Hash Tables"), tier="smart"
    )

    assert any("hash-tables" in candidate.url for candidate in result.candidates)


def test_mock_find_is_zero_cost():
    backend = MockWebBackend()

    _, usage = backend.find(system="sys", user=_finder_prompt("video_lecture"), tier="smart")

    assert usage["input_tokens"] == 0
    assert usage["output_tokens"] == 0
    assert usage["est_cost_usd"] == 0.0


# --------------------------------------------------------------------------
# MockWebBackend.verify -- good-domain survives, paywall/login rejected
# --------------------------------------------------------------------------


def test_mock_verify_ok_for_good_domain():
    backend = MockWebBackend()

    result, _ = backend.verify(
        system="sys", user=_verify_prompt("https://ocw.mit.edu/6-006/notes"), tier="smart"
    )

    assert isinstance(result, Verification)
    assert result.ok is True
    assert result.accessible is True
    assert result.on_topic is True
    assert result.evidence_quote  # a non-empty evidence quote


def test_mock_verify_ok_for_dot_edu_host():
    backend = MockWebBackend()

    result, _ = backend.verify(
        system="sys", user=_verify_prompt("https://cs.stanford.edu/lectures/bfs"), tier="smart"
    )

    assert result.ok is True


def test_mock_verify_rejects_paywall():
    backend = MockWebBackend()

    result, _ = backend.verify(
        system="sys", user=_verify_prompt("https://www.coursehero.com/paywall/bfs"), tier="smart"
    )

    assert result.ok is False
    assert result.accessible is False


def test_mock_verify_rejects_login_wall():
    backend = MockWebBackend()

    result, _ = backend.verify(
        system="sys", user=_verify_prompt("https://example.com/login?next=/bfs"), tier="smart"
    )

    assert result.ok is False
    assert result.accessible is False


def test_mock_verify_rejects_unrecognized_domain():
    backend = MockWebBackend()

    result, _ = backend.verify(
        system="sys", user=_verify_prompt("https://random-seo-blog.example/bfs"), tier="smart"
    )

    assert result.ok is False


def test_mock_verify_is_deterministic_and_zero_cost():
    backend = MockWebBackend()
    prompt = _verify_prompt("https://ocw.mit.edu/6-006/notes")

    first, usage = backend.verify(system="sys", user=prompt, tier="smart")
    second, _ = backend.verify(system="sys", user=prompt, tier="smart")

    assert first == second
    assert usage["est_cost_usd"] == 0.0


# --------------------------------------------------------------------------
# _web_tools -- version strings must match the model tier
# --------------------------------------------------------------------------


def test_web_tools_smart_tier_versions():
    tools = _web_tools("smart")
    by_name = {tool["name"]: tool for tool in tools}

    assert by_name["web_search"]["type"] == "web_search_20260209"
    assert by_name["web_fetch"]["type"] == "web_fetch_20260209"
    assert by_name["web_search"]["max_uses"] == 8
    assert by_name["web_fetch"]["max_uses"] == 8


def test_web_tools_fast_tier_versions():
    tools = _web_tools("fast")
    by_name = {tool["name"]: tool for tool in tools}

    assert by_name["web_search"]["type"] == "web_search_20250305"
    assert by_name["web_fetch"]["type"] == "web_fetch_20250910"


def test_web_tools_has_exactly_search_and_fetch():
    for tier in ("fast", "smart"):
        names = sorted(tool["name"] for tool in _web_tools(tier))
        assert names == ["web_fetch", "web_search"]


# --------------------------------------------------------------------------
# make_web_backend selection -- same rule as make_backend
# --------------------------------------------------------------------------


def test_make_web_backend_mock_when_no_key(caplog):
    with caplog.at_level(logging.INFO):
        backend = make_web_backend(Settings())

    assert isinstance(backend, MockWebBackend)
    assert any("mock" in record.message.lower() for record in caplog.records)


def test_make_web_backend_mock_when_flag_set_even_with_key():
    backend = make_web_backend(Settings(anthropic_api_key="real-looking-key", mock_llm=True))

    assert isinstance(backend, MockWebBackend)


def test_make_web_backend_real_when_key_present():
    backend = make_web_backend(Settings(anthropic_api_key="real-looking-key"))

    assert isinstance(backend, AnthropicWebBackend)
