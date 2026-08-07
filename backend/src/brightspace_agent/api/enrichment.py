"""M3.2 enrichment endpoints: the read model for one topic's suggested
resources, the topic/course enrich-run triggers, the keep/dismiss feedback
endpoint (feeds `domain_reputation`), a no-LLM/no-web dry-run cost estimate,
and a minimal per-topic status poll.

POST/PUT routes here are covered by main.py's CSRF guard (require
`X-BSA-Request: 1`) like every other mutating `/api/*` route -- the dry-run
and read endpoints are GETs and stay open to the same browser+CORS rules as
the rest of the app (see api/pipeline.py's module docstring for the same
reasoning, and api/events.py's for why GETs don't need the header).
"""

from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from sqlalchemy import select
from sqlalchemy.orm import Session

from brightspace_agent.agents.llm import WEB_SEARCH_COST_PER_SEARCH_USD, Tier, _estimate_cost_usd
from brightspace_agent.agents.web import web_search_max_uses
from brightspace_agent.api.deps import get_runner, get_session
from brightspace_agent.db.models import Course, EnrichmentResource, Topic
from brightspace_agent.pipeline.reputation import domain_of, record_feedback
from brightspace_agent.pipeline.stages.enrich import ENRICH_TIER, enrichment_model, topic_state
from brightspace_agent.pipeline.runner import PipelineRunner, RunActiveError, TopicNotFoundError

router = APIRouter(tags=["enrichment"])


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


def _get_topic_or_404(session: Session, topic_id: int) -> Topic:
    topic = session.get(Topic, topic_id)
    if topic is None:
        raise HTTPException(status_code=404, detail="unknown topic")
    return topic


def _get_course_or_404(session: Session, course_id: int) -> Course:
    course = session.get(Course, course_id)
    if course is None:
        raise HTTPException(status_code=404, detail="unknown course")
    return course


# --------------------------------------------------------------------------
# GET /api/topics/{topic_id}/enrichment
# --------------------------------------------------------------------------


class EnrichmentResourceOut(CamelModel):
    id: int
    url: str
    title: str | None
    resource_type: str | None
    intent: str | None
    rationale: str | None
    scores: dict
    verification: dict
    rank: int | None
    shared: bool
    status: str


class EnrichmentMetaOut(CamelModel):
    suggested: int
    kept: int
    dismissed: int
    # Has enrichment actually COMPLETED for this topic's current content
    # (pipeline/stages/enrich.py's `topic_state`), and did it come back with
    # nothing good? Without these, an empty list is ambiguous -- never
    # searched, searched and found nothing, or searched and the student
    # dismissed everything all render identically -- and the UI has to
    # pretend it doesn't know which. `thin` is only meaningful when
    # `searched` is true.
    searched: bool = False
    thin: bool = False


class TopicEnrichmentOut(CamelModel):
    topic_id: int
    resources: list[EnrichmentResourceOut]
    meta: EnrichmentMetaOut


def _resource_out(row: EnrichmentResource) -> EnrichmentResourceOut:
    return EnrichmentResourceOut(
        id=row.id,
        url=row.url,
        title=row.title,
        resource_type=row.resource_type,
        intent=row.intent,
        rationale=row.rationale,
        scores=json.loads(row.scores_json) if row.scores_json else {},
        verification=json.loads(row.verification_json) if row.verification_json else {},
        rank=row.rank,
        shared=bool(row.shared),
        status=row.status,
    )


@router.get("/api/topics/{topic_id}/enrichment", response_model=TopicEnrichmentOut)
def get_topic_enrichment(
    topic_id: int, session: Session = Depends(get_session), runner: PipelineRunner = Depends(get_runner)
) -> TopicEnrichmentOut:
    _get_topic_or_404(session, topic_id)
    rows = list(
        session.execute(
            select(EnrichmentResource)
            .where(EnrichmentResource.topic_id == topic_id)
            .order_by(EnrichmentResource.rank, EnrichmentResource.id)
        ).scalars().all()
    )
    resources = [_resource_out(row) for row in rows]
    # The model id comes from the runner's own backend (not Settings) so this
    # matches the key the stage wrote -- 'mock-smart' in mock mode.
    state = topic_state(session, topic_id, enrichment_model(runner.backend))
    meta = EnrichmentMetaOut(
        suggested=sum(1 for row in rows if row.status == "suggested"),
        kept=sum(1 for row in rows if row.status == "kept"),
        dismissed=sum(1 for row in rows if row.status == "dismissed"),
        searched=state["searched"],
        thin=state["thin"],
    )
    return TopicEnrichmentOut(topic_id=topic_id, resources=resources, meta=meta)


# --------------------------------------------------------------------------
# POST /api/topics/{topic_id}/enrich, POST /api/courses/{course_id}/enrich
# --------------------------------------------------------------------------


class EnrichRunResponse(CamelModel):
    run_token: int


@router.post("/api/topics/{topic_id}/enrich", response_model=EnrichRunResponse)
async def start_topic_enrichment(
    topic_id: int, session: Session = Depends(get_session), runner: PipelineRunner = Depends(get_runner)
) -> EnrichRunResponse:
    topic = _get_topic_or_404(session, topic_id)
    try:
        run_token = runner.start_enrichment(topic.course_id, topic_id=topic_id)
    except TopicNotFoundError as exc:
        # Covers a topic id from a taxonomy version the course has since
        # moved past -- exists in the DB (so the lookup above found it) but
        # isn't enrichable through this path anymore.
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RunActiveError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return EnrichRunResponse(run_token=run_token)


@router.post("/api/courses/{course_id}/enrich", response_model=EnrichRunResponse)
async def start_course_enrichment(
    course_id: int, session: Session = Depends(get_session), runner: PipelineRunner = Depends(get_runner)
) -> EnrichRunResponse:
    _get_course_or_404(session, course_id)
    try:
        run_token = runner.start_enrichment(course_id)
    except RunActiveError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return EnrichRunResponse(run_token=run_token)


# --------------------------------------------------------------------------
# PUT /api/enrichment/{resource_id}: keep/dismiss/suggested feedback
# --------------------------------------------------------------------------


class EnrichmentStatusUpdate(CamelModel):
    status: Literal["kept", "dismissed", "suggested"]


@router.put("/api/enrichment/{resource_id}", response_model=EnrichmentResourceOut)
def update_enrichment_status(
    resource_id: int, payload: EnrichmentStatusUpdate, session: Session = Depends(get_session)
) -> EnrichmentResourceOut:
    resource = session.get(EnrichmentResource, resource_id)
    if resource is None:
        raise HTTPException(status_code=404, detail="unknown enrichment resource")

    # Only an actual transition INTO kept/dismissed counts as feedback --
    # setting the same status again (or moving to/from 'suggested') must not
    # touch domain_reputation. This is intentionally a tally of feedback
    # EVENTS, not a mirror of current status: flipping kept -> dismissed ->
    # kept again records three real signals, one per transition.
    if payload.status != resource.status and payload.status in ("kept", "dismissed"):
        record_feedback(session, domain_of(resource.url), kept=(payload.status == "kept"))

    resource.status = payload.status
    session.commit()
    session.refresh(resource)
    return _resource_out(resource)


# --------------------------------------------------------------------------
# GET /api/courses/{course_id}/enrich/dry-run
# --------------------------------------------------------------------------

# Token estimates for the dry-run cost estimate (see the M3.2 brief): rough
# per-call (input, output) tokens, priced via agents.llm's cost table for the
# configured SMART model -- planner, finder, verifier, and judge are all
# "smart"-tier calls (pipeline/stages/enrich.py's `_TIER`). These are the
# brief's own order-of-magnitude figures, not measured averages:
#   - planner: ~2k in / ~1k out, one call per topic.
#   - finder: ~3k in / ~500 out for the tool loop's own tokens, one call per
#     search intent, PLUS the per-search web_search fee below.
#   - verifier: ~2k in, one call per candidate -- approximated here as one
#     per intent (i.e. every finder candidate gets verified), which is an
#     upper bound since not every finder call necessarily proposes one.
#     web_fetch (all the verifier binds) carries no per-use fee.
#   - judge: ~4k in / ~2k out, one call per topic.
# `_ASSUMED_INTENTS_PER_TOPIC` (5) is the brief's "assume ~5 intents/topic".
#
# The per-search fee is the same constant the runtime cap charges
# (agents/llm.py's WEB_SEARCH_COST_PER_SEARCH_USD), multiplied by the same
# conservative upper bound the runtime falls back to -- the web_search tool's
# own `max_uses` (agents/web.py). Both numbers deliberately come from one
# place so this estimate and the cap it is supposed to preview cannot drift
# apart. It IS an upper bound: a finder that answers after two searches is
# billed for two, not eight, so a real run usually costs less than shown.
#
# This whole estimate reads the DB only -- no LLM or web call is made.
_ASSUMED_INTENTS_PER_TOPIC = 5
_PLANNER_TOKENS = (2_000, 1_000)
_FINDER_TOKENS = (3_000, 500)
_VERIFIER_TOKENS = (2_000, 300)
_JUDGE_TOKENS = (4_000, 2_000)


def _topics_needing_enrichment(session: Session, course: Course) -> list[int]:
    """Current-version topics with no 'suggested'/'kept' resource row yet --
    i.e. never (successfully) enriched, or fully dismissed since. A topic
    that already has at least one suggested/kept row is not re-counted here
    even though re-running would still be allowed (re-enrichment is cheap:
    pipeline/stages/enrich.py's llm_cache makes an unchanged topic free) --
    this estimate is specifically "what would it cost to enrich what hasn't
    been touched yet", not "what would a full re-run of the course cost"."""
    topic_ids = list(
        session.execute(
            select(Topic.id).where(Topic.course_id == course.id, Topic.taxonomy_version == course.taxonomy_version)
        ).scalars().all()
    )
    if not topic_ids:
        return []
    already_enriched = set(
        session.execute(
            select(EnrichmentResource.topic_id)
            .where(
                EnrichmentResource.topic_id.in_(topic_ids),
                EnrichmentResource.status.in_(("suggested", "kept")),
            )
            .distinct()
        ).scalars().all()
    )
    return [tid for tid in topic_ids if tid not in already_enriched]


def _per_topic_estimate(model: str, tier: Tier = ENRICH_TIER) -> tuple[int, float, int]:
    """`(calls, est_cost_usd, web_searches)` for enriching one topic."""
    calls = 1 + _ASSUMED_INTENTS_PER_TOPIC + _ASSUMED_INTENTS_PER_TOPIC + 1  # planner + finders + verifiers + judge
    searches = _ASSUMED_INTENTS_PER_TOPIC * web_search_max_uses(tier)
    cost = (
        _estimate_cost_usd(model, *_PLANNER_TOKENS)
        + _ASSUMED_INTENTS_PER_TOPIC * _estimate_cost_usd(model, *_FINDER_TOKENS)
        + _ASSUMED_INTENTS_PER_TOPIC * _estimate_cost_usd(model, *_VERIFIER_TOKENS)
        + _estimate_cost_usd(model, *_JUDGE_TOKENS)
        + searches * WEB_SEARCH_COST_PER_SEARCH_USD
    )
    return calls, cost, searches


class EnrichDryRunResponse(CamelModel):
    topics_needing_enrichment: int
    calls_per_topic: int
    est_cost_per_topic_usd: float
    total_est_cost_usd: float
    # Upper bound on billable web searches per topic (finders x the
    # web_search tool's max_uses) -- surfaced so the estimate can say WHY it
    # is what it is, since at ~$0.01 a search this dominates the token cost.
    web_searches_per_topic: int


@router.get("/api/courses/{course_id}/enrich/dry-run", response_model=EnrichDryRunResponse)
def dry_run_enrichment(
    course_id: int, session: Session = Depends(get_session), runner: PipelineRunner = Depends(get_runner)
) -> EnrichDryRunResponse:
    course = _get_course_or_404(session, course_id)
    topics = _topics_needing_enrichment(session, course)
    calls_per_topic, est_cost_per_topic, searches_per_topic = _per_topic_estimate(runner.settings.smart_model)
    return EnrichDryRunResponse(
        topics_needing_enrichment=len(topics),
        calls_per_topic=calls_per_topic,
        est_cost_per_topic_usd=est_cost_per_topic,
        total_est_cost_usd=len(topics) * est_cost_per_topic,
        web_searches_per_topic=searches_per_topic,
    )


# --------------------------------------------------------------------------
# GET /api/topics/{topic_id}/enrich/status
# --------------------------------------------------------------------------


class EnrichStatusOut(CamelModel):
    active: bool
    last_run: dict | None


@router.get("/api/topics/{topic_id}/enrich/status", response_model=EnrichStatusOut)
def get_topic_enrich_status(
    topic_id: int, session: Session = Depends(get_session), runner: PipelineRunner = Depends(get_runner)
) -> EnrichStatusOut:
    topic = _get_topic_or_404(session, topic_id)
    status = runner.enrichment_status(topic.course_id)
    return EnrichStatusOut(active=status["active"], last_run=status["lastRun"])
