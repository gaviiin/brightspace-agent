"""Pipeline control endpoints: start a run, poll its status, and estimate
its cost without spending anything.

`run` and `dry-run` are POSTs and therefore covered by the CSRF guard in
main.py (require `X-BSA-Request: 1`) -- both can trigger real LLM spend
(run) or at least real work (dry-run reads the DB), and browsers only send
custom headers after a CORS preflight, which a drive-by page can't fake.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from brightspace_agent.agents.llm import _estimate_cost_usd
from brightspace_agent.api.deps import get_runner, get_session
from brightspace_agent.config import Settings
from brightspace_agent.db.models import Course, Material, MaterialTopic
from brightspace_agent.pipeline.runner import PipelineRunner, RunActiveError

router = APIRouter(prefix="/api/courses", tags=["pipeline"])

# Token estimates for the dry-run cost estimate (see the Task 9 brief):
# per-call (input, output) tokens, priced via agents.llm's cost table for
# the configured fast/smart models (Settings.fast_model/smart_model).
_SUMMARIZE_TOKENS = (4_000, 300)
_TAXONOMY_TOKENS = (30_000, 2_000)
_CLASSIFY_TOKENS = (2_000, 300)


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


def _get_course_or_404(session: Session, course_id: int) -> Course:
    course = session.get(Course, course_id)
    if course is None:
        raise HTTPException(status_code=404, detail="unknown course")
    return course


# --------------------------------------------------------------------------
# POST /{course_id}/pipeline/run
# --------------------------------------------------------------------------


class PipelineRunRequest(CamelModel):
    stages: list[str] | None = None
    # Opt-in to letting S2 re-propose over a taxonomy the student has
    # edited by hand (see pipeline/stages/taxonomy.py's `force`). Default
    # False, so the ordinary "Run pipeline" button can never silently
    # revert a user's taxonomy edit; forcing is API-only for now.
    force_taxonomy: bool = False


class PipelineRunResponse(CamelModel):
    run_token: int


@router.post("/{course_id}/pipeline/run", response_model=PipelineRunResponse)
async def start_pipeline_run(
    course_id: int,
    payload: PipelineRunRequest | None = None,
    session: Session = Depends(get_session),
    runner: PipelineRunner = Depends(get_runner),
) -> PipelineRunResponse:
    _get_course_or_404(session, course_id)
    stages = payload.stages if payload is not None else None
    force_taxonomy = payload.force_taxonomy if payload is not None else False
    try:
        run_token = runner.start(course_id, stages, force_taxonomy=force_taxonomy)
    except RunActiveError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return PipelineRunResponse(run_token=run_token)


# --------------------------------------------------------------------------
# GET /{course_id}/pipeline/status
# --------------------------------------------------------------------------


class StageStatusOut(CamelModel):
    stage: str
    status: str
    started_at: str
    finished_at: str | None
    usage: dict | None


class PipelineStatusResponse(CamelModel):
    active: bool
    stages: list[StageStatusOut]


@router.get("/{course_id}/pipeline/status", response_model=PipelineStatusResponse)
def get_pipeline_status(
    course_id: int, session: Session = Depends(get_session), runner: PipelineRunner = Depends(get_runner)
) -> PipelineStatusResponse:
    _get_course_or_404(session, course_id)
    status = runner.status(course_id)
    return PipelineStatusResponse(
        active=status["active"],
        stages=[
            StageStatusOut(
                stage=s["stage"], status=s["status"], started_at=s["startedAt"],
                finished_at=s["finishedAt"], usage=s["usage"],
            )
            for s in status["stages"]
        ],
    )


# --------------------------------------------------------------------------
# POST /{course_id}/pipeline/dry-run
# --------------------------------------------------------------------------


class StageDryRunOut(CamelModel):
    calls: int
    est_cost_usd: float


class DryRunResponse(CamelModel):
    by_stage: dict[str, StageDryRunOut]
    total_est_cost_usd: float


def _dry_run_counts(session: Session, course: Course) -> dict[str, int]:
    """Call counts from DB state alone -- no LLM calls, no `llm_cache`
    lookups (a cache hit still counts as a "call" here on purpose: this is
    an upper-bound spend estimate, not a cache-aware one)."""
    fetched_needing_extract = session.execute(
        select(func.count(Material.id)).where(
            Material.course_id == course.id, Material.status == "fetched", Material.sha256.is_not(None)
        )
    ).scalar_one()
    extracted_needing_summary = session.execute(
        select(func.count(Material.id)).where(
            Material.course_id == course.id, Material.status == "extracted", Material.summary.is_(None)
        )
    ).scalar_one()
    summarize_calls = fetched_needing_extract + extracted_needing_summary

    any_summarized = (
        session.execute(
            select(Material.id).where(Material.course_id == course.id, Material.status == "summarized").limit(1)
        ).first()
        is not None
    )
    taxonomy_calls = 1 if any_summarized else 0

    already_assigned_at_current_version = (
        select(MaterialTopic.id)
        .where(MaterialTopic.material_id == Material.id, MaterialTopic.taxonomy_version == course.taxonomy_version)
        .exists()
    )
    classify_calls = session.execute(
        select(func.count(Material.id)).where(
            Material.course_id == course.id,
            Material.status == "summarized",
            ~already_assigned_at_current_version,
        )
    ).scalar_one()

    return {"summarize": summarize_calls, "taxonomy": taxonomy_calls, "classify": classify_calls}


def _dry_run_response(session: Session, course: Course, settings: Settings) -> DryRunResponse:
    """Priced via the *configured* models (`Settings.fast_model`/
    `smart_model`), not whatever the currently active `LLMBackend` instance
    reports -- offline dev runs on `MockBackend`, whose `model_for_tier()`
    deliberately returns fake names with no cost-table entry (its calls are
    genuinely free), but the estimate here is "what would a real run cost",
    which only the configured model names answer. No LLM backend is
    consulted at all: this function doesn't touch `backend`.

    This total is an *upper-bound estimate* in more than one sense: besides
    the cache-agnostic call counting `_dry_run_counts` already documents, the
    actual run this estimate precedes is only capped at
    `Settings.max_cost_usd_per_run` optimistically (see that field's
    docstring) -- real spend can run a little past both numbers.
    """
    calls = _dry_run_counts(session, course)
    fast_model = settings.fast_model
    smart_model = settings.smart_model

    per_call_cost = {
        "summarize": _estimate_cost_usd(fast_model, *_SUMMARIZE_TOKENS),
        "taxonomy": _estimate_cost_usd(smart_model, *_TAXONOMY_TOKENS),
        "classify": _estimate_cost_usd(fast_model, *_CLASSIFY_TOKENS),
    }
    by_stage = {
        stage: StageDryRunOut(calls=calls[stage], est_cost_usd=calls[stage] * per_call_cost[stage])
        for stage in ("summarize", "taxonomy", "classify")
    }
    return DryRunResponse(
        by_stage=by_stage, total_est_cost_usd=sum(s.est_cost_usd for s in by_stage.values())
    )


@router.post("/{course_id}/pipeline/dry-run", response_model=DryRunResponse)
def dry_run_pipeline(
    course_id: int, session: Session = Depends(get_session), runner: PipelineRunner = Depends(get_runner)
) -> DryRunResponse:
    course = _get_course_or_404(session, course_id)
    return _dry_run_response(session, course, runner.settings)
