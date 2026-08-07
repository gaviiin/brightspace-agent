"""The web-tool agent layer: the two agents that need Anthropic's server-side
web tools -- the finder (searches for candidate resources) and the verifier
(fetches a candidate URL and judges it) -- plus a deterministic offline mock.

The planner and judge are plain structured calls and use `agents/llm.py`'s
`LLMBackend.structured_call`; only the finder/verifier need `web_search` /
`web_fetch`, which `structured_call` does not provide. This module is the
*second* (and last) place in the project that imports langchain -- kept
separate from `llm.py` so a stage still depends only on a Protocol, never on
`ChatAnthropic`.

Two things this module owns that are easy to get wrong elsewhere:

- **Tool version <-> model tier.** Anthropic's server tools are versioned, and
  the version has to match the model generation. `_web_tools(tier)` is the one
  place that mapping lives.
- **Tools + structured output in one turn.** A `.with_structured_output(...)`
  runnable can't also carry server tools cleanly, so `find`/`verify` use a
  two-call shape: first let the model run the search/fetch loop with tools
  bound, then coerce the result to the schema on the same tier -- accumulating
  usage from *both* calls. Crucially the *second* call carries only the tool
  turn's TEXT (see `_text_blocks_only`), never the raw `server_tool_use` /
  `web_search_tool_result` blocks: a request whose history contains
  server-tool blocks must still declare those tools, and the coercion call
  binds only the schema tool, so replaying the raw turn would be a 400 on
  every real find/verify. `tests/test_web_backend.py` pins that contract
  against a stub chat model, offline.
- **Per-search billing.** `web_search` is billed per search on top of tokens
  (`agents/llm.py`'s `WEB_SEARCH_COST_PER_SEARCH_USD`), so `_usage_info`
  folds that surcharge into the `est_cost_usd` the enrich stage's cost cap
  reads -- otherwise the dominant cost of this stage is invisible to the cap.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Protocol

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from brightspace_agent.agents.llm import (
    WEB_SEARCH_COST_PER_SEARCH_USD,
    LLMCallError,
    Tier,
    UsageInfo,
    _estimate_cost_usd,
    register_mock_builder,
)
from brightspace_agent.agents.promptfmt import (
    SECTION_CANDIDATE,
    SECTION_PRIOR_FAILURES,
    SECTION_SEARCH_INTENT,
    SECTION_TOPIC,
    SECTION_VERIFIED_CANDIDATES,
    labeled_value,
    section_body,
    slugify,
)
from brightspace_agent.agents.schemas import (
    Candidate,
    CandidateList,
    IntentType,
    JudgedResource,
    JudgeResult,
    SearchIntent,
    SearchPlan,
    Verification,
)
from brightspace_agent.config import Settings

logger = logging.getLogger(__name__)


class WebBackend(Protocol):
    def find(self, *, system: str, user: str, tier: Tier) -> tuple[CandidateList, UsageInfo]:
        """Search the web for candidate resources for one search intent."""
        ...

    def verify(self, *, system: str, user: str, tier: Tier) -> tuple[Verification, UsageInfo]:
        """Fetch one candidate URL and judge it live/on-topic/accessible/level."""
        ...


# --------------------------------------------------------------------------
# Tool version <-> model tier. The version string MUST match the tier's model
# generation; a mismatch is an API error. This is the single source of truth.
# No beta header is needed for any of these variants.
# --------------------------------------------------------------------------

_WEB_TOOLS_BY_TIER: dict[Tier, list[dict]] = {
    "smart": [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": 8},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 8},
    ],
    # The 'fast' entry is NOT dead code, even though the enrich stage runs
    # everything on 'smart' today: it is the versioned tool spec a fast-tier
    # verifier would need, and the tier split is a planned quality/cost
    # experiment. See the TIER DECISION comment in
    # pipeline/stages/enrich.py for why both agents are on smart for now.
    "fast": [
        {"type": "web_search_20250305", "name": "web_search", "max_uses": 8},
        {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 8},
    ],
}


def _web_tools(tier: Tier) -> list[dict]:
    """The `[web_search, web_fetch]` server-tool specs whose versions match
    `tier`'s model. `verify` slices out just `web_fetch` from this."""
    return [dict(tool) for tool in _WEB_TOOLS_BY_TIER[tier]]


def web_search_max_uses(tier: Tier) -> int:
    """The upper bound on searches ONE finder call can run at `tier` -- the
    `web_search` spec's `max_uses`. This is what the runtime charges when a
    response doesn't tell us how many searches actually happened, and what
    api/enrichment.py's dry-run assumes per finder, so the estimate and the
    cap agree on the same conservative number instead of drifting apart."""
    return max(
        (tool.get("max_uses", 0) for tool in _WEB_TOOLS_BY_TIER[tier] if tool["name"] == "web_search"),
        default=0,
    )


# --------------------------------------------------------------------------
# AnthropicWebBackend
# --------------------------------------------------------------------------

# The finder's turn carries a page or two of fetched search results into the
# drafting step, and the verifier quotes a fetched page; both need more output
# room than a one-shot structured call, but neither approaches the taxonomy
# stage's budget.
_MAX_TOKENS_BY_TIER: dict[Tier, int] = {"fast": 4096, "smart": 8192}


class AnthropicWebBackend:
    """Real backend: one `ChatAnthropic` per tier (lazily built, cached), the
    versioned server web tools bound per `_web_tools`, and the two-call
    tools-then-structured shape described in the module docstring. On a
    parse/validation error the structured coercion retries exactly once with
    the error appended; a second failure raises `LLMCallError` (mirrors
    `AnthropicBackend`)."""

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
                max_tokens=_MAX_TOKENS_BY_TIER[tier],
            )
        return self._chat_models[tier]

    def find(self, *, system: str, user: str, tier: Tier) -> tuple[CandidateList, UsageInfo]:
        return self._tools_then_structured(
            CandidateList,
            system=system,
            user=user,
            tier=tier,
            tools=_web_tools(tier),  # search + fetch
            coerce_instruction=(
                "Return the candidate resources you actually found via the tools as "
                "structured JSON matching the schema. Include only real URLs you "
                "retrieved; never invent one."
            ),
        )

    def verify(self, *, system: str, user: str, tier: Tier) -> tuple[Verification, UsageInfo]:
        # Only web_fetch: the candidate URL is already in the prompt, and
        # web_fetch fetches URLs present in the conversation.
        fetch_only = [tool for tool in _web_tools(tier) if tool["name"] == "web_fetch"]
        return self._tools_then_structured(
            Verification,
            system=system,
            user=user,
            tier=tier,
            tools=fetch_only,
            coerce_instruction=(
                "Return your verification verdict as structured JSON matching the "
                "schema, grounded in the page you fetched (not the search snippet)."
            ),
        )

    def _tools_then_structured(
        self, schema, *, system: str, user: str, tier: Tier, tools: list[dict], coerce_instruction: str
    ):
        chat = self._get_chat_model(tier)
        base = [SystemMessage(content=system), HumanMessage(content=user)]

        # (1) Let the server run the search/fetch loop inside one invoke.
        ai_msg: AIMessage = chat.bind_tools(tools).invoke(base)
        tokens = _add_tokens({"input_tokens": 0, "output_tokens": 0}, ai_msg)
        searches = _count_searches(ai_msg, tools, tier)

        # (2) Coerce to the schema on the same tier, carrying forward only the
        # tool turn's TEXT -- the model's own drafted findings -- as a plain
        # assistant message. The raw AIMessage must NOT be replayed: it holds
        # `server_tool_use`/`web_search_tool_result` blocks, and the API
        # requires a request whose history contains those to still declare the
        # server tools, while `with_structured_output` binds only the schema
        # tool. Text-only sidesteps the constraint entirely; the findings are
        # what the coercion step actually needs.
        findings = _text_blocks_only(ai_msg) or "(the tools returned nothing usable)"
        structured = chat.with_structured_output(schema, include_raw=True)
        messages = [*base, AIMessage(content=findings), HumanMessage(content=coerce_instruction)]
        result = structured.invoke(messages)
        parsed, error = result.get("parsed"), result.get("parsing_error")
        tokens = _add_tokens(tokens, result.get("raw"))

        if parsed is None or error is not None:
            retry = [
                *messages,
                HumanMessage(
                    content=(
                        "Your previous response failed validation with this error:\n"
                        f"{error}\nRespond again with output that satisfies the schema."
                    )
                ),
            ]
            result = structured.invoke(retry)
            parsed, error = result.get("parsed"), result.get("parsing_error")
            tokens = _add_tokens(tokens, result.get("raw"))

        if parsed is None or error is not None:
            raise LLMCallError(f"{schema.__name__} web call failed after one retry: {error!r}")

        return parsed, self._usage_info(tier, tokens, searches)

    def _usage_info(self, tier: Tier, tokens: dict[str, int], searches: int = 0) -> UsageInfo:
        """Token cost for this call PLUS `searches` x the per-search web-search
        fee (agents/llm.py's `WEB_SEARCH_COST_PER_SEARCH_USD`). The surcharge
        rides in `est_cost_usd` rather than a separate field so every existing
        consumer -- the cost cap, `StageStats.usage_total`, the pipeline_runs
        usage row, the UI -- counts it without changing shape."""
        model = self.model_for_tier(tier)
        input_tokens, output_tokens = tokens["input_tokens"], tokens["output_tokens"]
        return UsageInfo(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            est_cost_usd=(
                _estimate_cost_usd(model, input_tokens, output_tokens)
                + searches * WEB_SEARCH_COST_PER_SEARCH_USD
            ),
        )


def _add_tokens(acc: dict[str, int], raw: object) -> dict[str, int]:
    usage_metadata = getattr(raw, "usage_metadata", None) or {}
    acc["input_tokens"] += usage_metadata.get("input_tokens", 0)
    acc["output_tokens"] += usage_metadata.get("output_tokens", 0)
    return acc


def _content_blocks(message: object) -> list[dict]:
    """The dict content blocks of an AIMessage, or [] when its content is a
    plain string (LangChain normalizes simple text responses to `str`)."""
    content = getattr(message, "content", None)
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _text_blocks_only(message: object) -> str:
    """Just the model's prose from a tool-loop turn: the `text` of every text
    block, joined. Server-tool blocks (`server_tool_use`,
    `web_search_tool_result`, `web_fetch_tool_result`, ...) are dropped -- see
    `_tools_then_structured` for why they must never reach the second call."""
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content.strip()
    parts = [
        str(block.get("text", ""))
        for block in _content_blocks(message)
        if block.get("type") == "text"
    ]
    return "\n\n".join(part for part in parts if part.strip()).strip()


def _count_searches(message: object, tools: list[dict], tier: Tier) -> int:
    """How many billable `web_search` calls this turn made.

    Preferred source is the response itself: one `server_tool_use` block per
    search. If the response carries no structured blocks at all (content came
    back as a plain string, so we cannot tell), fall back to charging the
    tool's `max_uses` -- deliberately CONSERVATIVE: over-charging the cap
    stops a run early, under-charging lets real spend sail past the cap the
    UI displayed. Returns 0 when `web_search` wasn't even bound (the verifier
    binds `web_fetch` only, which has no per-use fee)."""
    if not any(tool["name"] == "web_search" for tool in tools):
        return 0
    blocks = _content_blocks(message)
    if not blocks:
        charged = web_search_max_uses(tier)
        logger.warning(
            "web: response content was a plain string, so the real search count "
            "is unknowable; conservatively charging max_uses=%d",
            charged,
        )
        return charged
    return sum(
        1
        for block in blocks
        if block.get("type") == "server_tool_use" and block.get("name") == "web_search"
    )


# --------------------------------------------------------------------------
# MockWebBackend -- deterministic, offline, zero cost/tokens.
#
# `find` maps the intent parsed from the prompt to a couple of realistic
# candidate URLs; `verify` accepts good-domain URLs and rejects paywalled/
# login-walled/unrecognized ones -- so the enrich stage's survive-and-reject
# paths both run offline. The SearchPlan/JudgeResult mock builders for the
# (structured) planner/judge live here too, next to the format they read.
# --------------------------------------------------------------------------

# intent -> [(host, path-tag, resource_type)]. Two candidates per intent, all
# on good domains except the past-exams paywall (kept in on purpose so the
# verifier has something real to reject). Hosts are chosen so the good-domain
# heuristic in `_mock_verify` accepts them (`.edu`, khanacademy, 3blue1brown,
# visualgo) -- these are also genuinely strong sources for each intent.
_MOCK_INTENT_PROFILE: dict[IntentType, list[tuple[str, str, str]]] = {
    "alternative_explanation": [
        ("ocw.mit.edu", "notes", "notes"),
        ("cs.stanford.edu", "explained", "article"),
    ],
    "video_lecture": [
        ("www.khanacademy.org", "video", "video"),
        ("www.3blue1brown.com", "lesson", "video"),
    ],
    "worked_examples": [
        ("ocw.mit.edu", "psets", "problem_set"),
        ("web.mit.edu", "examples", "notes"),
    ],
    "interactive_visualization": [
        ("visualgo.net", "viz", "interactive"),
        ("www.cs.usfca.edu", "galles", "interactive"),
    ],
    "university_notes": [
        ("cs.berkeley.edu", "notes", "notes"),
        ("www.cs.cmu.edu", "handout", "notes"),
    ],
    "past_exams": [
        ("www.coursehero.com", "paywall-exam", "past_exam"),  # paywall -> rejected
        ("ocw.mit.edu", "exams", "past_exam"),
    ],
}

# Any host containing one of these is treated as a trustworthy source by the
# mock verifier. `.edu` catches every university host; the rest are the small
# set of established creators the brief calls out.
_GOOD_DOMAIN_TOKENS = (".edu", "khanacademy", "3blue1brown", "visualgo")

_ZERO_USAGE: UsageInfo = {"model": "mock-web", "input_tokens": 0, "output_tokens": 0, "est_cost_usd": 0.0}


def _topic_slug(user: str) -> str:
    """A slug for the topic named in the prompt, falling back to a stable hash
    so a prompt without a `Name:` line still yields deterministic URLs."""
    name = labeled_value(section_body(user, SECTION_TOPIC), "Name")
    if name:
        return slugify(name)
    return "topic-" + hashlib.sha256(user.encode("utf-8")).hexdigest()[:8]


def _parse_intent(user: str) -> IntentType:
    raw = labeled_value(section_body(user, SECTION_SEARCH_INTENT), "intent")
    if raw in _MOCK_INTENT_PROFILE:
        return raw  # type: ignore[return-value]
    # Deterministic fallback so an unrecognized/absent intent still produces
    # candidates instead of raising.
    return "alternative_explanation"


def _mock_find(user: str) -> CandidateList:
    intent = _parse_intent(user)
    slug = _topic_slug(user)
    candidates = [
        Candidate(
            url=f"https://{host}/{slug}-{tag}",
            title=f"{host} — {slug.replace('-', ' ')} ({resource_type})",
            resource_type=resource_type,
            intent=intent,
            claimed_coverage=f"Covers {slug.replace('-', ' ')} for the {intent} intent.",
            why="Established source matching the intent.",
        )
        for host, tag, resource_type in _MOCK_INTENT_PROFILE[intent]
    ]
    return CandidateList(candidates=candidates)


def _host_of(url: str) -> str:
    match = re.match(r"https?://([^/]+)", url)
    return (match.group(1) if match else url).lower()


def _mock_verify(user: str) -> Verification:
    url = labeled_value(section_body(user, SECTION_CANDIDATE), "url") or ""
    topic = labeled_value(section_body(user, SECTION_TOPIC), "Name") or "the topic"
    lowered = url.lower()

    if "paywall" in lowered or "login" in lowered:
        return Verification(
            ok=False,
            accessible=False,
            on_topic=False,
            level_fit="unknown",
            evidence_quote="",
            reason="Behind a paywall or login wall; the page is not freely accessible.",
        )

    host = _host_of(url)
    if any(token in host for token in _GOOD_DOMAIN_TOKENS):
        return Verification(
            ok=True,
            accessible=True,
            on_topic=True,
            level_fit="on_level",
            evidence_quote=f'The page states it covers "{topic}" in depth.',
            reason="Recognized high-quality educational domain; live and on-topic.",
        )

    return Verification(
        ok=False,
        accessible=True,
        on_topic=False,
        level_fit="unknown",
        evidence_quote="",
        reason="Unrecognized source; could not confirm it is on-topic and trustworthy.",
    )


# --- structured mock builders for the planner and judge (LLMBackend mock) ---

# The five base intents give the finder something for each of five distinct
# resource types, so the offline happy path exercises the format-diversity
# path. On a retry round (prior failures present) the planner redirects with a
# genuinely new intent so the "new intents only" re-run has work to do.
_BASE_PLAN_INTENTS: list[IntentType] = [
    "alternative_explanation",
    "video_lecture",
    "worked_examples",
    "interactive_visualization",
    "past_exams",
]
_RETRY_PLAN_INTENTS: list[IntentType] = ["university_notes", "alternative_explanation"]

_INTENT_QUERY_HINT: dict[IntentType, str] = {
    "alternative_explanation": "explained from scratch",
    "video_lecture": "video lecture",
    "worked_examples": "worked examples with solutions",
    "interactive_visualization": "interactive visualization",
    "university_notes": "university lecture notes",
    "past_exams": "past exam questions with answers",
}


def _mock_search_plan(user: str) -> SearchPlan:
    topic = labeled_value(section_body(user, SECTION_TOPIC), "Name") or "this topic"
    retrying = bool(section_body(user, SECTION_PRIOR_FAILURES).strip())
    intents = _RETRY_PLAN_INTENTS if retrying else _BASE_PLAN_INTENTS
    return SearchPlan(
        intents=[
            SearchIntent(
                intent=intent,
                query=f"{topic} {_INTENT_QUERY_HINT[intent]}",
                rationale=f"Students studying {topic} benefit from a {intent} resource.",
            )
            for intent in intents
        ]
    )


# "1. url: https://x | type: notes | intent: university_notes | level: on_level | title: ..."
_JUDGE_LINE_RE = re.compile(
    r"^\s*\d+\.\s*url:\s*(?P<url>\S+)\s*\|\s*type:\s*(?P<type>[^|]+?)\s*\|\s*"
    r"intent:\s*(?P<intent>[^|]+?)\s*\|\s*level:\s*(?P<level>[^|]+?)\s*\|\s*"
    r"title:\s*(?P<title>.+?)\s*$"
)
_UNIFORM_SCORES = {
    "relevance": 0.8,
    "authority": 0.8,
    "recency": 0.7,
    "level_match": 0.8,
    "pedagogical_value": 0.8,
}


def _mock_judge(user: str) -> JudgeResult:
    resources: list[JudgedResource] = []
    body = section_body(user, SECTION_VERIFIED_CANDIDATES)
    for rank, line in enumerate((line for line in body.splitlines() if line.strip()), start=1):
        match = _JUDGE_LINE_RE.match(line)
        if not match:
            continue
        intent = match.group("intent").strip()
        resources.append(
            JudgedResource(
                url=match.group("url").strip(),
                title=match.group("title").strip(),
                resource_type=match.group("type").strip(),
                intent=intent,  # type: ignore[arg-type]
                keep=True,
                rank=rank,
                rationale=f"A solid {intent} resource that reinforces the topic.",
                scores=dict(_UNIFORM_SCORES),
            )
        )
    return JudgeResult(resources=resources)


register_mock_builder(SearchPlan, _mock_search_plan)
register_mock_builder(JudgeResult, _mock_judge)


class MockWebBackend:
    """Deterministic, offline stand-in for `AnthropicWebBackend`: same prompt
    always yields the same candidates/verdict, zero cost/tokens."""

    def find(self, *, system: str, user: str, tier: Tier) -> tuple[CandidateList, UsageInfo]:
        del system, tier  # output depends only on `user`
        return _mock_find(user), dict(_ZERO_USAGE)  # type: ignore[return-value]

    def verify(self, *, system: str, user: str, tier: Tier) -> tuple[Verification, UsageInfo]:
        del system, tier
        return _mock_verify(user), dict(_ZERO_USAGE)  # type: ignore[return-value]

    def model_for_tier(self, tier: Tier) -> str:
        return f"mock-{tier}"


# --------------------------------------------------------------------------
# Backend selection -- same rule as make_backend in llm.py.
# --------------------------------------------------------------------------


def make_web_backend(settings: Settings) -> WebBackend:
    """Pick the real Anthropic web backend or the mock, logging which and why:
    mock if `BSA_MOCK_LLM` is set, or if no Anthropic API key is configured."""
    if settings.mock_llm:
        logger.info("web backend: mock (BSA_MOCK_LLM is set)")
        return MockWebBackend()
    if not settings.anthropic_api_key:
        logger.info("web backend: mock (no Anthropic API key configured)")
        return MockWebBackend()

    logger.info(
        "web backend: anthropic (fast=%s, smart=%s)", settings.fast_model, settings.smart_model
    )
    return AnthropicWebBackend(settings)
