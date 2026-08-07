"""Tests for the web-tool agent layer (agents/web.py): MockWebBackend
determinism + intent parsing, the good-domain/paywall verify heuristic, the
version<->tier tool mapping, make_web_backend's mock/real selection, and --
against a stub chat model -- AnthropicWebBackend's two-call
tools-then-structured shape and its per-search cost accounting.

No network calls and no real API key anywhere: the AnthropicWebBackend tests
at the bottom drive a stub that stands in for `ChatAnthropic`.
"""

from __future__ import annotations

import logging

import pytest
from langchain_core.messages import AIMessage

from brightspace_agent.agents.llm import WEB_SEARCH_COST_PER_SEARCH_USD, LLMCallError
from brightspace_agent.agents.promptfmt import (
    SECTION_CANDIDATE,
    SECTION_COURSE,
    SECTION_SEARCH_INTENT,
    SECTION_TOPIC,
)
from brightspace_agent.agents.schemas import Candidate, CandidateList, Verification
from brightspace_agent.agents.web import (
    AnthropicWebBackend,
    MockWebBackend,
    _web_tools,
    make_web_backend,
    web_search_max_uses,
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


# ==========================================================================
# AnthropicWebBackend: the two-call tools-then-structured shape.
#
# These pin the one part of M3 that MockWebBackend can never exercise -- the
# *shape of the requests* the real backend builds. In particular the coercion
# (second) call must carry only TEXT, never the tool turn's server_tool_use /
# web_search_tool_result blocks: the API requires a request whose history
# contains server-tool blocks to still declare those tools, and the coercion
# call binds only the schema tool, so replaying the raw turn would 400 on
# every real find/verify while every mock-backed test stayed green.
# ==========================================================================

# A realistic tool-loop turn: prose interleaved with the server-tool blocks
# the Anthropic API returns for `web_search`.
_TOOL_TURN_BLOCKS = [
    {"type": "text", "text": "Let me search for lecture notes."},
    {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "bfs notes"}},
    {
        "type": "web_search_tool_result",
        "tool_use_id": "srvtoolu_1",
        "content": [{"type": "web_search_result", "url": "https://ocw.mit.edu/bfs", "title": "MIT BFS"}],
    },
    {"type": "server_tool_use", "id": "srvtoolu_2", "name": "web_search", "input": {"query": "bfs video"}},
    {
        "type": "web_search_tool_result",
        "tool_use_id": "srvtoolu_2",
        "content": [{"type": "web_search_result", "url": "https://www.khanacademy.org/bfs", "title": "KA BFS"}],
    },
    {"type": "text", "text": "I found https://ocw.mit.edu/bfs (notes) and https://www.khanacademy.org/bfs (video)."},
]

_SERVER_TOOL_BLOCK_TYPES = {
    "server_tool_use",
    "web_search_tool_result",
    "web_fetch_tool_result",
    "tool_use",
    "tool_result",
}

_CANDIDATES = CandidateList(
    candidates=[
        Candidate(
            url="https://ocw.mit.edu/bfs", title="MIT BFS", resource_type="notes",
            intent="university_notes", claimed_coverage="bfs", why="mit",
        )
    ]
)


# --------------------------------------------------------------------------
# Stub chat model: the whole surface AnthropicWebBackend uses is
# bind_tools() -> .invoke(), with_structured_output() -> .invoke().
# --------------------------------------------------------------------------


class _Usage:
    """Minimal stand-in for the raw AIMessage the structured runnable returns
    under include_raw=True -- only `usage_metadata` is read."""

    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.usage_metadata = {"input_tokens": input_tokens, "output_tokens": output_tokens}


class _StubStructured:
    def __init__(self, owner: "_StubChat") -> None:
        self._owner = owner

    def invoke(self, messages):
        self._owner.coercion_calls.append(messages)
        return self._owner.coercion_results.pop(0)


class _StubChat:
    def __init__(self, *, tool_turn: AIMessage, coercion_results: list[dict]) -> None:
        self._tool_turn = tool_turn
        self.coercion_results = coercion_results
        self.bound_tools: list[dict] = []
        self.tool_loop_calls: list[list] = []
        self.coercion_calls: list[list] = []
        self.structured_schemas: list[type] = []

    # (1) the tool loop
    def bind_tools(self, tools):
        self.bound_tools = tools
        return self

    def invoke(self, messages):
        self.tool_loop_calls.append(messages)
        return self._tool_turn

    # (2) the coercion call
    def with_structured_output(self, schema, include_raw=False):
        assert include_raw is True  # usage accounting depends on the raw message
        self.structured_schemas.append(schema)
        return _StubStructured(self)


def _backend_with(chat: _StubChat, tier="smart") -> AnthropicWebBackend:
    backend = AnthropicWebBackend(Settings(anthropic_api_key="test-not-used", data_dir="/tmp/bsa-test"))
    # Pre-seed the per-tier cache so no real ChatAnthropic is ever constructed.
    backend._chat_models[tier] = chat  # type: ignore[assignment]
    return backend


def _all_blocks(messages) -> list[dict]:
    blocks: list[dict] = []
    for message in messages:
        content = getattr(message, "content", message)
        if isinstance(content, list):
            blocks.extend(block for block in content if isinstance(block, dict))
    return blocks


# --------------------------------------------------------------------------
# (a) The coercion call carries text only -- no server-tool blocks
# --------------------------------------------------------------------------


def test_coercion_call_carries_no_server_tool_blocks():
    chat = _StubChat(
        tool_turn=AIMessage(content=_TOOL_TURN_BLOCKS),
        coercion_results=[{"parsed": _CANDIDATES, "parsing_error": None, "raw": _Usage(0, 0)}],
    )
    backend = _backend_with(chat)

    backend.find(system="sys", user="usr", tier="smart")

    assert len(chat.coercion_calls) == 1
    messages = chat.coercion_calls[0]
    for block in _all_blocks(messages):
        assert block.get("type") not in _SERVER_TOOL_BLOCK_TYPES, block
    # Every message's content is plain text.
    assert all(isinstance(getattr(m, "content", None), str) for m in messages)
    # And the tool turn's own prose IS forwarded, so the coercion step has the
    # findings to work from.
    joined = "\n".join(m.content for m in messages)
    assert "https://ocw.mit.edu/bfs" in joined
    assert "Let me search for lecture notes." in joined
    # The raw tool-turn AIMessage object itself never appears.
    assert all(m is not chat._tool_turn for m in messages)


def test_verify_coercion_call_is_text_only_too():
    fetch_turn = AIMessage(
        content=[
            {"type": "server_tool_use", "id": "srvtoolu_9", "name": "web_fetch", "input": {"url": "https://x.edu"}},
            {"type": "web_fetch_tool_result", "tool_use_id": "srvtoolu_9", "content": {"type": "document"}},
            {"type": "text", "text": 'The page says "BFS explores a graph layer by layer".'},
        ]
    )
    verification = Verification(
        ok=True, accessible=True, on_topic=True, level_fit="on_level",
        evidence_quote="BFS explores a graph layer by layer", reason="fetched and on-topic",
    )
    chat = _StubChat(
        tool_turn=fetch_turn,
        coercion_results=[{"parsed": verification, "parsing_error": None, "raw": _Usage(0, 0)}],
    )
    backend = _backend_with(chat)

    result, usage = backend.verify(system="sys", user="usr", tier="smart")

    assert result is verification
    # verify binds web_fetch only -- and web_fetch has no per-search fee.
    assert [tool["name"] for tool in chat.bound_tools] == ["web_fetch"]
    assert usage["est_cost_usd"] == 0.0
    for block in _all_blocks(chat.coercion_calls[0]):
        assert block.get("type") not in _SERVER_TOOL_BLOCK_TYPES, block


def test_plain_string_tool_turn_is_forwarded_as_is():
    chat = _StubChat(
        tool_turn=AIMessage(content="I found https://ocw.mit.edu/bfs."),
        coercion_results=[{"parsed": _CANDIDATES, "parsing_error": None, "raw": _Usage(0, 0)}],
    )
    backend = _backend_with(chat)

    backend.find(system="sys", user="usr", tier="smart")

    joined = "\n".join(m.content for m in chat.coercion_calls[0])
    assert "I found https://ocw.mit.edu/bfs." in joined


# --------------------------------------------------------------------------
# (b) Usage from BOTH calls accumulates (+ the per-search surcharge)
# --------------------------------------------------------------------------


def test_usage_accumulates_across_both_calls_and_charges_per_search():
    tool_turn = AIMessage(content=_TOOL_TURN_BLOCKS)
    tool_turn.usage_metadata = {"input_tokens": 1000, "output_tokens": 200}  # type: ignore[assignment]
    chat = _StubChat(
        tool_turn=tool_turn,
        coercion_results=[{"parsed": _CANDIDATES, "parsing_error": None, "raw": _Usage(300, 100)}],
    )
    backend = _backend_with(chat)

    _, usage = backend.find(system="sys", user="usr", tier="smart")

    assert usage["input_tokens"] == 1300  # 1000 (tool loop) + 300 (coercion)
    assert usage["output_tokens"] == 300  # 200 + 100
    # Two server_tool_use blocks -> two billed searches, on top of tokens.
    token_cost = (1300 / 1_000_000) * 3.0 + (300 / 1_000_000) * 15.0  # claude-sonnet-5 rates
    assert usage["est_cost_usd"] == pytest.approx(token_cost + 2 * WEB_SEARCH_COST_PER_SEARCH_USD)


def test_unknowable_search_count_charges_the_conservative_max_uses(caplog):
    # A plain-string response tells us nothing about how many searches ran;
    # the cap must assume the worst rather than silently under-count.
    chat = _StubChat(
        tool_turn=AIMessage(content="I searched and found things."),
        coercion_results=[{"parsed": _CANDIDATES, "parsing_error": None, "raw": _Usage(0, 0)}],
    )
    backend = _backend_with(chat)

    with caplog.at_level(logging.WARNING, logger="brightspace_agent.agents.web"):
        _, usage = backend.find(system="sys", user="usr", tier="smart")

    assert web_search_max_uses("smart") == 8
    assert usage["est_cost_usd"] == pytest.approx(8 * WEB_SEARCH_COST_PER_SEARCH_USD)
    # The guess must be observable: silence here is what made the fallback
    # impossible to calibrate against real traffic.
    assert any("max_uses" in record.message for record in caplog.records if record.levelno == logging.WARNING)


def test_exact_search_count_logs_info_not_warning(caplog):
    chat = _StubChat(
        tool_turn=AIMessage(content=_TOOL_TURN_BLOCKS),
        coercion_results=[{"parsed": _CANDIDATES, "parsing_error": None, "raw": _Usage(0, 0)}],
    )
    backend = _backend_with(chat)

    with caplog.at_level(logging.INFO, logger="brightspace_agent.agents.web"):
        backend.find(system="sys", user="usr", tier="smart")

    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
    # The exact count is visible at INFO, so a live run shows its arithmetic.
    assert any("counted 2 web_search" in record.getMessage() for record in caplog.records)


# --------------------------------------------------------------------------
# (c) Retry once, then LLMCallError
# --------------------------------------------------------------------------


def test_coercion_retries_once_then_succeeds():
    chat = _StubChat(
        tool_turn=AIMessage(content=_TOOL_TURN_BLOCKS),
        coercion_results=[
            {"parsed": None, "parsing_error": "not valid json", "raw": _Usage(50, 10)},
            {"parsed": _CANDIDATES, "parsing_error": None, "raw": _Usage(60, 20)},
        ],
    )
    backend = _backend_with(chat)

    parsed, usage = backend.find(system="sys", user="usr", tier="smart")

    assert parsed is _CANDIDATES
    assert len(chat.coercion_calls) == 2
    # The retry appends the validation error, keeps the text-only history, and
    # its tokens count too.
    assert "failed validation" in chat.coercion_calls[1][-1].content
    assert usage["input_tokens"] == 110
    for block in _all_blocks(chat.coercion_calls[1]):
        assert block.get("type") not in _SERVER_TOOL_BLOCK_TYPES, block


def test_second_coercion_failure_raises_llm_call_error():
    chat = _StubChat(
        tool_turn=AIMessage(content=_TOOL_TURN_BLOCKS),
        coercion_results=[
            {"parsed": None, "parsing_error": "bad 1", "raw": _Usage(0, 0)},
            {"parsed": None, "parsing_error": "bad 2", "raw": _Usage(0, 0)},
        ],
    )
    backend = _backend_with(chat)

    with pytest.raises(LLMCallError) as excinfo:
        backend.find(system="sys", user="usr", tier="smart")

    assert "CandidateList" in str(excinfo.value)
    assert len(chat.coercion_calls) == 2  # exactly one retry, not a loop
