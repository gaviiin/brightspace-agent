"""The tiered LLM layer: a small `LLMBackend` protocol, a LangChain/Anthropic
-backed implementation with structured outputs and usage tracking, and a
deterministic mock for tests/offline development.

This is the ONLY module in the project that imports langchain /
langchain-anthropic. Pipeline stages depend on `LLMBackend` (a Protocol),
never on `ChatAnthropic` directly, so swapping providers later only touches
this file.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable
from typing import Literal, Protocol, TypedDict

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from brightspace_agent.agents.schemas import DocSummary
from brightspace_agent.config import Settings

logger = logging.getLogger(__name__)

Tier = Literal["fast", "smart"]


class UsageInfo(TypedDict):
    model: str
    input_tokens: int
    output_tokens: int
    est_cost_usd: float


class LLMCallError(Exception):
    """A structured LLM call failed validation on both the initial attempt
    and the single retry."""


class LLMBackend(Protocol):
    def structured_call(
        self, schema: type[BaseModel], *, system: str, user: str, tier: Tier
    ) -> tuple[BaseModel, UsageInfo]:
        """Call the LLM, parsing its response into `schema`. Retries once
        (backend-dependent) on a validation error before raising
        `LLMCallError`."""
        ...

    def model_for_tier(self, tier: Tier) -> str:
        """The concrete model id `tier` currently resolves to. Cheap, no
        network call -- callers (e.g. the summarize stage) use this to build
        an `llm_cache` lookup key *before* deciding whether a real call is
        even needed."""
        ...


# --------------------------------------------------------------------------
# Cost table -- USD per million tokens (input, output). Edit here when
# Anthropic pricing changes; nothing else in this file needs to know the
# numbers. A model missing from this table estimates to $0 rather than
# raising, since cost estimation is advisory, not load-bearing.
# --------------------------------------------------------------------------

_COST_PER_MTOK_USD: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-sonnet-5": (3.0, 15.0),
}


def _estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    rates = _COST_PER_MTOK_USD.get(model)
    if rates is None:
        logger.warning("no cost table entry for model %r; estimating $0", model)
        return 0.0
    input_rate, output_rate = rates
    return (input_tokens / 1_000_000) * input_rate + (output_tokens / 1_000_000) * output_rate


# --------------------------------------------------------------------------
# AnthropicBackend
# --------------------------------------------------------------------------


class AnthropicBackend:
    """Real backend: one `ChatAnthropic` per tier (lazily built, cached),
    structured output via `with_structured_output(schema, include_raw=True)`
    -- `include_raw` keeps the raw `AIMessage` (and its `usage_metadata`)
    around even when parsing succeeds. On a parse/validation error, retries
    exactly once with the validation error appended to the user message;
    a second failure raises `LLMCallError`.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._chat_models: dict[Tier, ChatAnthropic] = {}

    def model_for_tier(self, tier: Tier) -> str:
        return self._settings.fast_model if tier == "fast" else self._settings.smart_model

    def _get_chat_model(self, tier: Tier) -> ChatAnthropic:
        if tier not in self._chat_models:
            self._chat_models[tier] = ChatAnthropic(
                model=self.model_for_tier(tier),
                api_key=self._settings.anthropic_api_key,
                max_tokens=2048,
            )
        return self._chat_models[tier]

    def structured_call(
        self, schema: type[BaseModel], *, system: str, user: str, tier: Tier
    ) -> tuple[BaseModel, UsageInfo]:
        chat = self._get_chat_model(tier)
        structured = chat.with_structured_output(schema, include_raw=True)

        result = structured.invoke([SystemMessage(content=system), HumanMessage(content=user)])
        parsed, error = result.get("parsed"), result.get("parsing_error")

        if parsed is None or error is not None:
            retry_user = (
                f"{user}\n\n"
                "Your previous response failed validation with this error:\n"
                f"{error}\n"
                "Please respond again with output that satisfies the schema."
            )
            result = structured.invoke([SystemMessage(content=system), HumanMessage(content=retry_user)])
            parsed, error = result.get("parsed"), result.get("parsing_error")

        if parsed is None or error is not None:
            raise LLMCallError(f"structured_call failed after one retry: {error!r}")

        return parsed, self._usage_info(tier, result.get("raw"))

    def _usage_info(self, tier: Tier, raw: object) -> UsageInfo:
        model = self.model_for_tier(tier)
        usage_metadata = getattr(raw, "usage_metadata", None) or {}
        input_tokens = usage_metadata.get("input_tokens", 0)
        output_tokens = usage_metadata.get("output_tokens", 0)
        return UsageInfo(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            est_cost_usd=_estimate_cost_usd(model, input_tokens, output_tokens),
        )


# --------------------------------------------------------------------------
# MockBackend -- deterministic, schema-aware, zero cost/tokens.
#
# Per-schema builders live in a small registry so later stages (topic
# extraction, etc.) can register their own without this module needing to
# know about every schema in the project.
# --------------------------------------------------------------------------

MockBuilder = Callable[[str], BaseModel]

_MOCK_BUILDERS: dict[type[BaseModel], MockBuilder] = {}


def register_mock_builder(schema: type[BaseModel], builder: MockBuilder) -> None:
    """Register `builder` (user-prompt-text -> schema instance) as the
    deterministic mock output for `schema`."""
    _MOCK_BUILDERS[schema] = builder


_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'-]{2,}")


def _mock_doc_summary(user: str) -> DocSummary:
    digest = hashlib.sha256(user.encode("utf-8")).hexdigest()
    words = list(dict.fromkeys(match.lower() for match in _WORD_RE.findall(user)))
    key_terms = words[:10] or ["material"]
    return DocSummary(
        summary=(
            f"Mock summary ({digest[:8]}): a deterministic stand-in summary generated "
            "offline by MockBackend, based on the supplied material text."
        ),
        doc_kind_guess="document",
        key_terms=key_terms,
    )


register_mock_builder(DocSummary, _mock_doc_summary)


class MockBackend:
    """Deterministic, schema-aware stand-in for `AnthropicBackend`: no
    network access, zero cost/tokens, same input always produces the same
    output. Used whenever no Anthropic API key is configured or
    `BSA_MOCK_LLM` is set, so the pipeline runs fully offline."""

    def structured_call(
        self, schema: type[BaseModel], *, system: str, user: str, tier: Tier
    ) -> tuple[BaseModel, UsageInfo]:
        del system  # mock output only depends on `user` + `schema`
        builder = _MOCK_BUILDERS.get(schema)
        if builder is None:
            raise LLMCallError(f"MockBackend has no builder registered for schema {schema.__name__!r}")

        parsed = builder(user)
        usage: UsageInfo = {
            "model": self.model_for_tier(tier),
            "input_tokens": 0,
            "output_tokens": 0,
            "est_cost_usd": 0.0,
        }
        return parsed, usage

    def model_for_tier(self, tier: Tier) -> str:
        return f"mock-{tier}"


# --------------------------------------------------------------------------
# Backend selection
# --------------------------------------------------------------------------


def make_backend(settings: Settings) -> LLMBackend:
    """Pick the real Anthropic backend or the mock, logging which and why:
    mock if `BSA_MOCK_LLM` is set, or if no Anthropic API key is configured
    (covers plain `ANTHROPIC_API_KEY` too -- see Settings.anthropic_api_key).
    """
    if settings.mock_llm:
        logger.info("LLM backend: mock (BSA_MOCK_LLM is set)")
        return MockBackend()
    if not settings.anthropic_api_key:
        logger.info("LLM backend: mock (no Anthropic API key configured)")
        return MockBackend()

    logger.info(
        "LLM backend: anthropic (fast=%s, smart=%s)", settings.fast_model, settings.smart_model
    )
    return AnthropicBackend(settings)
