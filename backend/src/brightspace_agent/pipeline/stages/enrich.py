"""M3 enrich stage: turn ONE course topic into a handful of verified
supplementary web resources, then write them to `enrichment_resources`.

The per-topic pipeline is five steps:

1. **Planner** (smart, structured, no tools) reads the topic and its attached
   materials and emits 3-6 typed search intents grounded in the course's own
   terminology.
2. **Finders** (web tools, one per intent, fanned out) search + fetch and
   propose candidate resources.
3. **Verifiers** (web tools, one per unique URL, fanned out) fetch each
   candidate and gate it on live/accessible/on-topic/level. Only `ok=True`
   survives.
4. **Judge** (smart, structured, no tools, one call) rubric-scores the
   survivors with a format-diversity constraint.
5. **Dedup + reputation bias**: apply each domain's learned keep/dismiss bias
   to the relevance/authority axes, pick a diverse top `target_max`, and
   upsert the rows (existing URLs updated in place, never duplicated).

The course-batch entry point (`run_enrich_stage`) adds a sixth step after
every topic finishes: a **cross-topic dedup pass**. Topics research
independently, so one genuinely good page often lands under several of them;
the pass keeps it on its best-fit topic (marking that row `shared`) and
deletes the un-actioned duplicates elsewhere. See `_dedupe_across_topics`.

Nothing the model returns is trusted as-is: the verifier re-checks every URL,
the judge's rank is advisory (the stage re-ranks after bias), and the kept set
is capped in code. If fewer than `target_min` survive, the planner is
re-invoked **once** with the failure reasons to redirect the searches; after
that the topic is reported thin rather than padded.

Everything here runs offline against MockBackend/MockWebBackend -- this module
depends only on the `LLMBackend`/`WebBackend` protocols, never on langchain.
The result of an unchanged topic is cached in `llm_cache` keyed on a hash of
the topic context, so a re-run is free.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import resources

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from brightspace_agent.agents.llm import LLMBackend, Tier
from brightspace_agent.agents.promptfmt import (
    SECTION_ATTACHED_MATERIALS,
    SECTION_CANDIDATE,
    SECTION_COURSE,
    SECTION_PRIOR_FAILURES,
    SECTION_SEARCH_INTENT,
    SECTION_TOPIC,
    SECTION_VERIFIED_CANDIDATES,
)
from brightspace_agent.agents.schemas import (
    Candidate,
    JudgeResult,
    SearchPlan,
    Verification,
)
from brightspace_agent.agents.web import WebBackend
from brightspace_agent.db.models import (
    Course,
    EnrichmentResource,
    LlmCache,
    Material,
    MaterialTopic,
    Topic,
)
from brightspace_agent.pipeline.reputation import bias_for, domain_of
from brightspace_agent.pipeline.stats import StageStats

logger = logging.getLogger(__name__)

# v2: tier split (fast finders/verifiers) + bounded tool loops -- results
# from the all-smart configuration must not replay from cache as if
# equivalent.
PROMPT_VERSION = "s5.v2"
_STAGE = "enrich"

# TIER DECISION. The plan put finders/verifiers on the fast (haiku) tier and
# only the planner/judge on smart. M3.1 deviated and ran everything smart,
# betting that Sonnet's dynamic-filtering web tools would be worth it. The
# 2026-08-07 live run settled the bet the other way: a server-side tool loop
# re-bills the accumulated context (dominated by fetched pages) on every
# internal iteration, so ONE smart-tier finder call billed ~700k input
# tokens, and one topic committed ~$12 before the cost cap could abort it.
#
# So the plan's tiering is restored, with the loop bounds in agents/web.py
# (`max_uses`, `max_content_tokens`) doing the heavy lifting on cost:
#  - Planner + judge: smart. One call each per topic, no tools, and they are
#    where quality is concentrated (grounded queries in, final ranking out).
#  - Finder + verifier: fast. They use the basic (non-dynamic-filtering)
#    web tool variants, which is what the plan intended. If fast-tier
#    verification degrades link quality, keep/dismiss rates in
#    domain_reputation will show it -- that experiment now runs in reverse.
ENRICH_TIER: Tier = "smart"
PLAN_TIER: Tier = "smart"
JUDGE_TIER: Tier = "smart"
FIND_TIER: Tier = "fast"
VERIFY_TIER: Tier = "fast"

_MAX_CONTEXT_MATERIALS = 15
_FANOUT_CONCURRENCY = 4
_TOPIC_CONCURRENCY = 3  # topics enriched at once in the batch entry point
# The rubric axes the reputation nudge is applied to (relevance = how well it
# fits, authority = how trustworthy the source); the other three axes are
# intrinsic to the resource and untouched by a domain's history.
_BIASED_AXES = ("relevance", "authority")
_ALL_AXES = ("relevance", "authority", "recency", "level_match", "pedagogical_value")

_PROMPTS = resources.files("brightspace_agent.agents.prompts")
_PLANNER_PROMPT = _PROMPTS.joinpath("enrich_planner.md").read_text(encoding="utf-8")
_FINDER_PROMPT = _PROMPTS.joinpath("enrich_finder.md").read_text(encoding="utf-8")
_VERIFIER_PROMPT = _PROMPTS.joinpath("enrich_verifier.md").read_text(encoding="utf-8")
_JUDGE_PROMPT = _PROMPTS.joinpath("enrich_judge.md").read_text(encoding="utf-8")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Topic context
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _TopicContext:
    topic_id: int
    slug: str
    name: str
    description: str
    course_code: str
    course_name: str
    material_lines: tuple[str, ...]

    def cache_sha(self) -> str:
        """A stable fingerprint of everything that feeds the searches, so an
        unchanged topic re-runs for free and a changed one (new name,
        description, or attached materials) re-enriches."""
        canonical = json.dumps(
            {
                "topic": {"slug": self.slug, "name": self.name, "description": self.description},
                "course": {"code": self.course_code, "name": self.course_name},
                "materials": sorted(self.material_lines),
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _gather_context(session: Session, topic_id: int) -> _TopicContext | None:
    topic = session.get(Topic, topic_id)
    if topic is None:
        return None
    course = session.get(Course, topic.course_id)
    if course is None:
        return None

    material_rows = session.execute(
        select(Material.title, Material.summary)
        .join(MaterialTopic, MaterialTopic.material_id == Material.id)
        .where(
            MaterialTopic.topic_id == topic_id,
            MaterialTopic.taxonomy_version == topic.taxonomy_version,
        )
        .order_by(Material.id)
        .limit(_MAX_CONTEXT_MATERIALS)
    ).all()
    material_lines = tuple(
        f"- {title}: {(summary or '').strip().splitlines()[0] if summary else '(no summary)'}"
        for title, summary in material_rows
    )

    return _TopicContext(
        topic_id=topic_id,
        slug=topic.slug,
        name=topic.name,
        description=topic.description or "",
        course_code=course.code or "",
        course_name=course.name,
        material_lines=material_lines,
    )


# --------------------------------------------------------------------------
# Prompt payloads (shared format with the MockWebBackend + mock builders)
# --------------------------------------------------------------------------


def _course_and_topic_block(context: _TopicContext) -> str:
    return (
        f"{SECTION_COURSE}\n"
        f"{context.course_code} — {context.course_name}\n\n"
        f"{SECTION_TOPIC}\n"
        f"Name: {context.name}\n"
        f"Description: {context.description}\n"
    )


def _planner_user(context: _TopicContext, failures: list[str]) -> str:
    materials = "\n".join(context.material_lines) or "(none attached yet)"
    parts = [
        _course_and_topic_block(context),
        f"\n{SECTION_ATTACHED_MATERIALS}\n{materials}\n",
    ]
    if failures:
        parts.append(f"\n{SECTION_PRIOR_FAILURES}\n" + "\n".join(f"- {reason}" for reason in failures) + "\n")
    return "".join(parts)


def _finder_user(context: _TopicContext, intent: str, query: str, rationale: str) -> str:
    return (
        f"{_course_and_topic_block(context)}\n"
        f"{SECTION_SEARCH_INTENT}\n"
        f"intent: {intent}\n"
        f"query: {query}\n"
        f"rationale: {rationale}\n"
    )


def _verifier_user(context: _TopicContext, candidate: Candidate) -> str:
    return (
        f"{SECTION_TOPIC}\n"
        f"Name: {context.name}\n"
        f"Description: {context.description}\n\n"
        f"{SECTION_CANDIDATE}\n"
        f"url: {candidate.url}\n"
        f"title: {candidate.title}\n"
        f"resource_type: {candidate.resource_type}\n"
        f"intent: {candidate.intent}\n"
    )


def _judge_user(context: _TopicContext, survivors: list[_Survivor]) -> str:
    lines = []
    for index, survivor in enumerate(survivors, start=1):
        candidate = survivor.candidate
        verification = survivor.verification
        # Forward the verifier's fetched-page signals -- `level` (its level-fit
        # verdict) and its `reason` -- so the judge scores level_match against
        # what the verifier actually saw on the page, not blind.
        lines.append(
            f"{index}. url: {candidate.url} | type: {candidate.resource_type} | "
            f"intent: {candidate.intent} | level: {verification.level_fit} | "
            f"title: {candidate.title}"
        )
        lines.append(f'   evidence: "{verification.evidence_quote}" | verifier: {verification.reason}')
    return (
        f"{_course_and_topic_block(context)}\n"
        f"{SECTION_VERIFIED_CANDIDATES}\n" + "\n".join(lines) + "\n"
    )


# --------------------------------------------------------------------------
# Cost cap (optimistic, mirrors summarize/classify)
# --------------------------------------------------------------------------


def _over_cap(stats: StageStats, cost_cap_usd: float | None, cost_lock: threading.Lock) -> bool:
    if cost_cap_usd is None:
        return False
    with cost_lock:
        return stats.usage_total["est_cost_usd"] >= cost_cap_usd


def _record_usage(stats: StageStats, usage: dict, cost_lock: threading.Lock) -> None:
    with cost_lock:
        stats.add_usage(usage)


# --------------------------------------------------------------------------
# Per-topic entry point
# --------------------------------------------------------------------------


@dataclass
class _Survivor:
    candidate: Candidate
    verification: Verification


@dataclass
class _Scored:
    url: str
    title: str
    resource_type: str
    intent: str
    rationale: str
    scores: dict[str, float]
    verification: Verification
    biased_score: float = 0.0


@dataclass
class _EnrichState:
    """Mutable per-topic accumulators, so the retry round can extend the first
    round's survivors without re-verifying URLs already seen."""

    survivors: list[_Survivor] = field(default_factory=list)
    seen_urls: set[str] = field(default_factory=set)
    tried_intents: set[tuple[str, str]] = field(default_factory=set)
    reject_reasons: list[str] = field(default_factory=list)


async def run_topic_enrichment(
    session_factory: sessionmaker[Session],
    backend: LLMBackend,
    web_backend: WebBackend,
    topic_id: int,
    *,
    target_min: int = 3,
    target_max: int = 5,
    cost_cap_usd: float | None = None,
    cost_lock: threading.Lock | None = None,
) -> StageStats:
    """Enrich ONE topic. See the module docstring for the pipeline. `cost_cap_usd`
    is the optimistic per-run spend guard (same pattern as summarize/classify):
    once accumulated spend reaches it, remaining paid calls are skipped and
    `stats.aborted` is set. `cost_lock` guards the read-then-add on
    `stats.usage_total`; one is created if not supplied."""
    stats = StageStats()
    lock = cost_lock or threading.Lock()

    with session_factory() as session:
        context = _gather_context(session, topic_id)
        if context is None:
            logger.warning("enrich: topic %s not found; skipping", topic_id)
            return stats

    model = backend.model_for_tier(ENRICH_TIER)
    context_sha = context.cache_sha()

    # Cache: a hit replays the stored resources with no LLM/web calls.
    with session_factory() as session:
        cached = _read_cache(session, context_sha, model)
    if cached is not None:
        with session_factory() as session:
            written = _write_resources(session, topic_id, cached["resources"])
            session.commit()
        stats.cached_hits += 1
        stats.enriched += written
        if cached.get("thin"):
            stats.thin_topics += 1
        return stats

    # --- Round 1: plan -> find -> verify ---
    state = _EnrichState()
    plan = await _plan(backend, context, [], stats, cost_cap_usd, lock)
    if plan is None:  # capped before the first paid call
        return stats
    await _find_and_verify(web_backend, context, plan, state, stats, cost_cap_usd, lock)

    # --- Round 2 (once): if thin, re-plan with the failure reasons ---
    if len(state.survivors) < target_min and not stats.aborted:
        retry_plan = await _plan(backend, context, state.reject_reasons, stats, cost_cap_usd, lock)
        if retry_plan is not None:
            await _find_and_verify(web_backend, context, retry_plan, state, stats, cost_cap_usd, lock)

    thin = len(state.survivors) < target_min

    # --- Judge + reputation bias + write ---
    final = await _judge_and_rank(
        backend, web_backend, context, state.survivors, target_max, stats, cost_cap_usd, lock, session_factory
    )

    with session_factory() as session:
        written = _write_resources(session, topic_id, final, prune=not stats.aborted)
        # NEVER cache an aborted run. `final` after a cost-cap abort is a
        # truncated (often empty) result, and caching it would make every
        # later run a "successful" cache hit replaying that emptiness: raising
        # the cap and pressing Refresh would silently do nothing until the
        # topic's own content happened to change. An uncached abort simply
        # re-runs next time, which is the whole point of the cap being
        # advisory. Same reasoning for `prune` above: an aborted run must not
        # delete last run's still-good suggestions on the strength of a
        # partial result.
        if not stats.aborted:
            _write_cache(session, context_sha, model, {"resources": final, "thin": thin})
        session.commit()

    stats.enriched += written
    if stats.aborted:
        logger.warning(
            "enrich: topic %s hit the cost cap; result not cached so a later run re-enriches it",
            topic_id,
        )
    # "Thin" means "we searched properly and this is genuinely all there is",
    # so a run cut short by the cap is never reported thin -- it didn't finish
    # searching.
    if thin and not stats.aborted:
        stats.thin_topics += 1
        logger.info(
            "enrich: topic %s is thin (%d verified resource(s) after retry); reported honestly",
            topic_id, len(state.survivors),
        )
    return stats


async def _plan(
    backend: LLMBackend,
    context: _TopicContext,
    failures: list[str],
    stats: StageStats,
    cost_cap_usd: float | None,
    lock: threading.Lock,
) -> SearchPlan | None:
    if _over_cap(stats, cost_cap_usd, lock):
        stats.aborted = True
        return None
    plan, usage = await asyncio.to_thread(
        backend.structured_call,
        SearchPlan,
        system=_PLANNER_PROMPT,
        user=_planner_user(context, failures),
        tier=PLAN_TIER,
    )
    _record_usage(stats, usage, lock)
    return plan


async def _find_and_verify(
    web_backend: WebBackend,
    context: _TopicContext,
    plan: SearchPlan,
    state: _EnrichState,
    stats: StageStats,
    cost_cap_usd: float | None,
    lock: threading.Lock,
) -> None:
    """Run finders for the plan's not-yet-tried intents, then verify every new
    unique candidate URL, extending `state` in place."""
    new_intents = [
        intent for intent in plan.intents
        if (intent.intent, intent.query) not in state.tried_intents
    ]
    for intent in new_intents:
        state.tried_intents.add((intent.intent, intent.query))
    if not new_intents:
        return

    semaphore = asyncio.Semaphore(_FANOUT_CONCURRENCY)

    async def _find(intent) -> list[Candidate]:
        async with semaphore:
            if _over_cap(stats, cost_cap_usd, lock):
                stats.aborted = True
                return []
            result, usage = await asyncio.to_thread(
                web_backend.find,
                system=_FINDER_PROMPT,
                user=_finder_user(context, intent.intent, intent.query, intent.rationale),
                tier=FIND_TIER,
            )
            _record_usage(stats, usage, lock)
            return list(result.candidates)

    candidate_batches = await asyncio.gather(*(_find(intent) for intent in new_intents))

    # Dedup by URL across this round and prior rounds; first occurrence wins.
    fresh: list[Candidate] = []
    for candidate in (c for batch in candidate_batches for c in batch):
        if candidate.url in state.seen_urls:
            continue
        state.seen_urls.add(candidate.url)
        fresh.append(candidate)

    async def _verify(candidate: Candidate) -> tuple[Candidate, Verification] | None:
        async with semaphore:
            if _over_cap(stats, cost_cap_usd, lock):
                stats.aborted = True
                return None
            verification, usage = await asyncio.to_thread(
                web_backend.verify,
                system=_VERIFIER_PROMPT,
                user=_verifier_user(context, candidate),
                tier=VERIFY_TIER,
            )
            _record_usage(stats, usage, lock)
            return candidate, verification

    verified = await asyncio.gather(*(_verify(candidate) for candidate in fresh))
    for pair in verified:
        if pair is None:
            continue
        candidate, verification = pair
        # A survivor must clear the gate AND carry the fetched-page evidence the
        # gate exists to enforce: an ok=True verdict with an empty evidence
        # quote hasn't actually established on-topic, so it is rejected here
        # rather than written. (The brief: the verifier gates on FETCHED
        # evidence, not the search snippet.)
        if verification.ok and verification.evidence_quote.strip():
            state.survivors.append(_Survivor(candidate=candidate, verification=verification))
        elif verification.ok:
            state.reject_reasons.append(f"{candidate.intent}: verified but produced no evidence quote")
        elif verification.reason:
            state.reject_reasons.append(f"{candidate.intent}: {verification.reason}")


async def _judge_and_rank(
    backend: LLMBackend,
    web_backend: WebBackend,
    context: _TopicContext,
    survivors: list[_Survivor],
    target_max: int,
    stats: StageStats,
    cost_cap_usd: float | None,
    lock: threading.Lock,
    session_factory: sessionmaker[Session],
) -> list[dict]:
    if not survivors:
        return []
    if _over_cap(stats, cost_cap_usd, lock):
        stats.aborted = True
        return []

    judged, usage = await asyncio.to_thread(
        backend.structured_call,
        JudgeResult,
        system=_JUDGE_PROMPT,
        user=_judge_user(context, survivors),
        tier=JUDGE_TIER,
    )
    _record_usage(stats, usage, lock)

    survivor_by_url = {survivor.candidate.url: survivor for survivor in survivors}

    scored: list[_Scored] = []
    with session_factory() as session:
        for resource in judged.resources:
            if not resource.keep:
                continue
            survivor = survivor_by_url.get(resource.url)
            if survivor is None:
                continue  # judge referenced a URL that wasn't a survivor
            biased_scores, biased_mean = _apply_bias(session, resource.url, resource.scores)
            scored.append(
                _Scored(
                    url=resource.url,
                    title=resource.title,
                    resource_type=resource.resource_type,
                    intent=resource.intent,
                    rationale=resource.rationale,
                    scores=biased_scores,
                    verification=survivor.verification,
                    biased_score=biased_mean,
                )
            )

    selected = _select_diverse(scored, target_max)
    return [
        {
            "url": item.url,
            "title": item.title,
            "resource_type": item.resource_type,
            "intent": item.intent,
            "rationale": item.rationale,
            "scores": item.scores,
            "verification": item.verification.model_dump(),
            "rank": rank,
        }
        for rank, item in enumerate(selected, start=1)
    ]


def _apply_bias(session: Session, url: str, scores: dict) -> tuple[dict[str, float], float]:
    """Nudge the relevance/authority axes by the domain's learned bias, clamp
    every axis to [0, 1], and return (biased_scores, mean-across-all-axes). The
    mean is the ranking key, so a domain students keep outranks an
    equal-rubric domain they dismiss."""
    bias = bias_for(session, domain_of(url))
    biased: dict[str, float] = {}
    for axis in _ALL_AXES:
        value = float(scores.get(axis, 0.5))
        if axis in _BIASED_AXES:
            value += bias
        biased[axis] = min(1.0, max(0.0, value))
    mean = sum(biased.values()) / len(_ALL_AXES)
    return biased, mean


def _select_diverse(scored: list[_Scored], target_max: int) -> list[_Scored]:
    """Pick up to `target_max`, favouring a mix of intents: one pass takes the
    best of each not-yet-seen intent (in score order), a second fills any
    remaining slots by score. The final list is ordered by biased score so the
    student leads with the strongest resource.

    Diversity keys on `intent` (a closed IntentType literal) rather than
    `resource_type` (model-authored free text -- "notes" vs "lecture_notes"
    would defeat the backstop)."""
    ordered = sorted(scored, key=lambda item: (-item.biased_score, item.url))

    selected: list[_Scored] = []
    seen_intents: set[str] = set()
    for item in ordered:
        if len(selected) >= target_max:
            break
        if item.intent not in seen_intents:
            selected.append(item)
            seen_intents.add(item.intent)

    if len(selected) < target_max:
        chosen = {id(item) for item in selected}
        for item in ordered:
            if len(selected) >= target_max:
                break
            if id(item) not in chosen:
                selected.append(item)

    selected.sort(key=lambda item: (-item.biased_score, item.url))
    return selected


# --------------------------------------------------------------------------
# Writing rows + cache
# --------------------------------------------------------------------------


def _is_safe_url(url: str) -> bool:
    """Only `http://` / `https://` URLs may ever be stored.

    Everything in `EnrichmentResource.url` is model-authored, and the frontend
    renders it as an `<a href>` (panels/TopicSupplementary.tsx). A page the
    finder fetched could try to steer the model into emitting a
    `javascript:` (or `data:`) URL -- the one XSS-shaped hole in an otherwise
    React-escaped surface. The Pydantic schema rejects these on the way in
    (agents/schemas.py's `Candidate.url`); this is the last line, and it also
    covers rows replayed from a cache row written before that validator
    existed (which is also why this tolerates a non-string: cache payloads are
    whatever JSON happens to be in the row)."""
    return isinstance(url, str) and url.lower().startswith(("http://", "https://"))


def _write_resources(session: Session, topic_id: int, resources: list[dict], *, prune: bool = True) -> int:
    """Upsert the kept resources for a topic by (topic_id, url): existing rows
    are updated in place (never duplicated), new ones inserted as 'suggested'.
    A row a student has already kept/dismissed keeps that status -- re-running
    enrichment must not silently undo their decision. Returns rows written.

    `prune=True` (the default) also deletes this topic's leftover 'suggested'
    rows that the new result set does NOT contain: without it, a topic whose
    content changed accumulates last run's stale suggestions interleaved with
    the new ones at colliding ranks. Only un-actioned 'suggested' rows are
    ever deleted -- a kept or dismissed row is a student decision and survives
    regardless. Callers pass `prune=False` when the result set is untrustworthy
    (a cost-cap abort's partial output)."""
    written = 0
    kept_urls: set[str] = set()
    for resource in resources:
        if not _is_safe_url(resource["url"]):
            logger.warning(
                "enrich: dropping non-http(s) URL for topic %s (%s)", topic_id, repr(resource["url"])[:80]
            )
            continue
        kept_urls.add(resource["url"])
        existing = session.execute(
            select(EnrichmentResource).where(
                EnrichmentResource.topic_id == topic_id,
                EnrichmentResource.url == resource["url"],
            )
        ).scalar_one_or_none()

        scores_json = json.dumps(resource["scores"])
        verification_json = json.dumps(resource["verification"])
        if existing is None:
            session.add(
                EnrichmentResource(
                    topic_id=topic_id,
                    url=resource["url"],
                    title=resource["title"],
                    resource_type=resource["resource_type"],
                    intent=resource["intent"],
                    rationale=resource["rationale"],
                    scores_json=scores_json,
                    verification_json=verification_json,
                    rank=resource["rank"],
                    status="suggested",
                )
            )
        else:
            existing.title = resource["title"]
            existing.resource_type = resource["resource_type"]
            existing.intent = resource["intent"]
            existing.rationale = resource["rationale"]
            existing.scores_json = scores_json
            existing.verification_json = verification_json
            existing.rank = resource["rank"]
            # status preserved: don't reset a student's keep/dismiss to suggested.
        written += 1

    if prune:
        session.flush()  # so rows added above are visible to the delete below
        stale = session.execute(
            select(EnrichmentResource).where(
                EnrichmentResource.topic_id == topic_id,
                EnrichmentResource.status == "suggested",
                # An empty `kept_urls` (a completed run that found nothing)
                # correctly matches every suggested row: SQLAlchemy expands an
                # empty NOT IN to a true predicate.
                EnrichmentResource.url.notin_(kept_urls),
            )
        ).scalars().all()
        for row in stale:
            logger.debug("enrich: pruning stale suggestion %s for topic %s", row.url, topic_id)
            session.delete(row)

    return written


def enrichment_model(backend: LLMBackend) -> str:
    """The model id this stage's `llm_cache` rows are keyed on. Public so the
    API can look a topic's enrichment state up (see `topic_state`) against the
    same key the stage writes -- including in mock mode, where it is
    'mock-smart' rather than the configured smart model."""
    return backend.model_for_tier(ENRICH_TIER)


def topic_state(session: Session, topic_id: int, model: str) -> dict:
    """`{"searched": bool, "thin": bool}` for `topic_id` AS IT IS NOW.

    This is what lets the UI tell "we haven't looked yet" apart from "we
    looked and there genuinely isn't anything good" -- two states that
    otherwise render as the same empty list. It's derived from this stage's
    own cache row rather than a new column because the cache row already
    answers exactly the right question: it exists only if a run COMPLETED
    (an aborted run deliberately writes none), it is keyed on a hash of the
    topic's current content, so it stops applying the moment the topic
    changes -- which is precisely when "we already searched this" stops being
    true -- and it carries the run's own `thin` verdict.
    """
    context = _gather_context(session, topic_id)
    if context is None:
        return {"searched": False, "thin": False}
    payload = _read_cache(session, context.cache_sha(), model)
    if payload is None:
        return {"searched": False, "thin": False}
    return {"searched": True, "thin": bool(payload.get("thin"))}


def _read_cache(session: Session, context_sha: str, model: str) -> dict | None:
    """The cached enrichment payload ({"resources": [...], "thin": bool}), or
    None if there is no usable row. As in summarize/classify, an unparseable or
    off-shape row is treated as a miss and overwritten on the next run."""
    row = session.execute(
        select(LlmCache).where(
            LlmCache.sha256 == context_sha,
            LlmCache.stage == _STAGE,
            LlmCache.prompt_version == PROMPT_VERSION,
            LlmCache.model == model,
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    try:
        payload = json.loads(row.output_json)
    except json.JSONDecodeError as exc:
        logger.warning("enrich: ignoring unparseable cache row for %s (%s)", context_sha[:12], exc)
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("resources"), list):
        logger.warning("enrich: ignoring off-shape cache row for %s", context_sha[:12])
        return None
    return payload


def _write_cache(session: Session, context_sha: str, model: str, payload: dict) -> None:
    output_json = json.dumps(payload)
    created_at = _now_iso()
    session.execute(
        sqlite_insert(LlmCache)
        .values(
            sha256=context_sha,
            stage=_STAGE,
            prompt_version=PROMPT_VERSION,
            model=model,
            output_json=output_json,
            created_at=created_at,
        )
        .on_conflict_do_update(
            index_elements=["sha256", "stage", "prompt_version", "model"],
            set_={"output_json": output_json, "created_at": created_at},
        )
    )


# --------------------------------------------------------------------------
# Batch entry point (M3.2's runner calls this)
# --------------------------------------------------------------------------


async def run_enrich_stage(
    session_factory: sessionmaker[Session],
    backend: LLMBackend,
    web_backend: WebBackend,
    course_id: int,
    *,
    target_min: int = 3,
    target_max: int = 5,
    cost_cap_usd: float | None = None,
) -> StageStats:
    """Enrich every topic at the course's current taxonomy version, fanned out
    (bounded). Per-topic failure isolates: one topic raising (a web backend
    error, say) increments `stats.failed` and does not abort the others. The
    per-topic results are merged into one `StageStats` for the runner."""
    stats = StageStats()

    with session_factory() as session:
        course = session.get(Course, course_id)
        if course is None:
            raise ValueError(f"no course with id {course_id}")
        version = course.taxonomy_version
        topic_ids = list(
            session.execute(
                select(Topic.id)
                .where(Topic.course_id == course_id, Topic.taxonomy_version == version)
                .order_by(Topic.order_index, Topic.id)
            ).scalars().all()
        )
    stats.taxonomy_version = version
    if not topic_ids:
        logger.info("enrich: course %s has no topics at version %d", course_id, version)
        return stats

    semaphore = asyncio.Semaphore(_TOPIC_CONCURRENCY)

    async def _one(topic_id: int) -> StageStats | None:
        async with semaphore:
            try:
                return await run_topic_enrichment(
                    session_factory,
                    backend,
                    web_backend,
                    topic_id,
                    target_min=target_min,
                    target_max=target_max,
                    cost_cap_usd=cost_cap_usd,
                )
            except Exception as exc:  # noqa: BLE001 -- one topic must not abort the course
                logger.warning(
                    "enrich: topic %s failed (%s: %s); other topics continue",
                    topic_id, type(exc).__name__, exc,
                )
                return None

    results = await asyncio.gather(*(_one(topic_id) for topic_id in topic_ids))
    for result in results:
        if result is None:
            stats.failed += 1
        else:
            _merge_stats(stats, result)

    stats.deduped += _dedupe_across_topics(session_factory, course_id, version)
    return stats


def _dedupe_across_topics(session_factory: sessionmaker[Session], course_id: int, version: int) -> int:
    """Cross-topic dedup, the batch path's closing pass.

    Each topic researches independently, so a genuinely good page (an OCW
    lecture, say) is routinely found under several topics of the same course.
    Left alone the student sees the same link five times and `shared` -- a
    column, an API field and a frontend type -- is constitutively false.

    For every URL present under more than one topic at `version`, keep the row
    on its best-fit topic, mark it `shared`, and delete the duplicates.
    Best-fit is:

      1. a row the student KEPT wins outright -- their decision anchors the
         URL to that topic, and no ranking of ours gets to overrule it;
      2. then live rows ('suggested'), by highest mean rubric score (what
         produced `rank` in the first place);
      3. a DISMISSED row ranks last: a dismissal means "not on this topic",
         never "hide this link everywhere", so it must not win over -- and
         thereby delete -- a live suggestion on another topic;
      4. then lowest topic id, purely so the result is deterministic.

    Only un-actioned 'suggested' duplicates are ever deleted. A kept or
    dismissed row on a losing topic stays exactly as it is (it is just also
    marked `shared`) -- pruning must never destroy a student decision. Note
    `shared` is only ever set, never cleared: it records "this URL was
    contested across topics", which stays true after the losers are gone.

    Returns the number of duplicate rows removed.
    """
    removed = 0
    with session_factory() as session:
        rows = list(
            session.execute(
                select(EnrichmentResource)
                .join(Topic, Topic.id == EnrichmentResource.topic_id)
                .where(Topic.course_id == course_id, Topic.taxonomy_version == version)
            ).scalars().all()
        )
        by_url: dict[str, list[EnrichmentResource]] = {}
        for row in rows:
            by_url.setdefault(row.url, []).append(row)

        for url, group in by_url.items():
            if len({row.topic_id for row in group}) < 2:
                continue
            winner = min(group, key=_best_fit_key)
            removed_here = 0
            for row in group:
                row.shared = 1
                if row is winner or row.status != "suggested":
                    continue
                session.delete(row)
                removed_here += 1
            removed += removed_here
            logger.info(
                "enrich: %s appeared under %d topics; kept on topic %s (%d duplicate(s) removed)",
                url, len(group), winner.topic_id, removed_here,
            )
        session.commit()
    return removed


def _best_fit_key(row: EnrichmentResource) -> tuple[int, float, int]:
    """Sort key for `_dedupe_across_topics` (lower wins): kept first, then
    live suggestions by best mean rubric score, dismissed last (so a
    dismissal never deletes a live suggestion on another topic), then lowest
    topic id as the deterministic tie-break."""
    try:
        scores = json.loads(row.scores_json) if row.scores_json else {}
        values = [float(value) for value in scores.values()]
    except (json.JSONDecodeError, TypeError, ValueError):
        values = []
    mean = sum(values) / len(values) if values else 0.0
    priority = {"kept": 0, "dismissed": 2}.get(row.status, 1)
    return (priority, -mean, row.topic_id)


def _merge_stats(into: StageStats, other: StageStats) -> None:
    into.enriched += other.enriched
    into.thin_topics += other.thin_topics
    into.deduped += other.deduped
    into.cached_hits += other.cached_hits
    into.failed += other.failed
    into.aborted = into.aborted or other.aborted
    into.add_usage(other.usage_total)
