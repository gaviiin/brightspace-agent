"""`GET /api/courses` and `GET /api/courses/{id}`: the frontend's course
list/detail read model -- course metadata, material counts by status, and a
compact summary of the course's latest pipeline run (if any). Plus
`GET /api/courses/{id}/runs`: the sync/pipeline run history behind the
workspace's Runs drawer.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from brightspace_agent.api.deps import get_runner, get_session
from brightspace_agent.db.models import Course, Material, PipelineRun, SyncRun
from brightspace_agent.pipeline.runner import PipelineRunner

router = APIRouter(prefix="/api/courses", tags=["courses"])


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class MaterialCountsOut(CamelModel):
    total: int
    summarized: int
    failed: int


class PipelineSummaryOut(CamelModel):
    status: str
    stage: str | None


class CourseOut(CamelModel):
    id: int
    org_unit_id: int
    name: str
    code: str | None
    term: str | None
    taxonomy_version: int
    last_synced_at: str | None
    material_counts: MaterialCountsOut
    pipeline: PipelineSummaryOut | None


def _material_counts(session: Session, course_id: int) -> MaterialCountsOut:
    rows = session.execute(
        select(Material.status, func.count(Material.id))
        .where(Material.course_id == course_id)
        .group_by(Material.status)
    ).all()
    by_status = {status: count for status, count in rows}
    return MaterialCountsOut(
        total=sum(by_status.values()),
        summarized=by_status.get("summarized", 0),
        failed=by_status.get("failed", 0),
    )


def _pipeline_summary(runner: PipelineRunner, course_id: int) -> PipelineSummaryOut | None:
    status = runner.status(course_id)
    if not status["stages"]:
        return None
    latest = status["stages"][-1]
    return PipelineSummaryOut(status=latest["status"], stage=latest["stage"])


def _course_out(session: Session, runner: PipelineRunner, course: Course) -> CourseOut:
    return CourseOut(
        id=course.id,
        org_unit_id=course.d2l_org_unit_id,
        name=course.name,
        code=course.code,
        term=course.term,
        taxonomy_version=course.taxonomy_version,
        last_synced_at=course.last_synced_at,
        material_counts=_material_counts(session, course.id),
        pipeline=_pipeline_summary(runner, course.id),
    )


@router.get("", response_model=list[CourseOut])
def list_courses(
    session: Session = Depends(get_session), runner: PipelineRunner = Depends(get_runner)
) -> list[CourseOut]:
    courses = list(session.execute(select(Course).order_by(Course.id)).scalars().all())
    return [_course_out(session, runner, course) for course in courses]


@router.get("/{course_id}", response_model=CourseOut)
def get_course(
    course_id: int, session: Session = Depends(get_session), runner: PipelineRunner = Depends(get_runner)
) -> CourseOut:
    course = session.get(Course, course_id)
    if course is None:
        raise HTTPException(status_code=404, detail="unknown course")
    return _course_out(session, runner, course)


# --------------------------------------------------------------------------
# GET /api/courses/{id}/runs -- sync + pipeline run history
# --------------------------------------------------------------------------

_RUNS_LIMIT = 10
_ERRORS_SHOWN = 5


class SyncErrorOut(CamelModel):
    # Explicit alias: to_camel("d2l_topic_id") mangles the digit ("d2LTopicId").
    d2l_topic_id: int | None = Field(default=None, alias="d2lTopicId")
    message: str


class SyncRunOut(CamelModel):
    id: int
    source: str
    status: str
    started_at: str
    finished_at: str | None
    files: int
    bytes: int
    not_needed: int
    error_count: int
    # Capped at `_ERRORS_SHOWN`; `error_count` always carries the full total.
    errors: list[SyncErrorOut]


class PipelineRunOut(CamelModel):
    id: int
    stage: str
    status: str
    started_at: str
    finished_at: str | None
    input_tokens: int
    output_tokens: int
    est_cost_usd: float
    error: str | None


class RunsOut(CamelModel):
    sync_runs: list[SyncRunOut]
    pipeline_runs: list[PipelineRunOut]


def _parse_json_dict(raw: str | None) -> dict:
    """stats_json/usage_json defensively: rows written by older versions or
    interrupted runs must render as zeros, never 500 the endpoint."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _int_or_zero(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _sync_run_out(run: SyncRun) -> SyncRunOut:
    stats = _parse_json_dict(run.stats_json)
    raw_errors = stats.get("errors")
    errors = raw_errors if isinstance(raw_errors, list) else []
    parsed_errors = []
    for entry in errors[:_ERRORS_SHOWN]:
        if not isinstance(entry, dict):
            continue
        parsed_errors.append(
            SyncErrorOut(
                d2lTopicId=entry.get("d2lTopicId") if isinstance(entry.get("d2lTopicId"), int) else None,
                message=str(entry.get("message", "")),
            )
        )
    return SyncRunOut(
        id=run.id,
        source=run.source,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        files=_int_or_zero(stats.get("files")),
        bytes=_int_or_zero(stats.get("bytes")),
        not_needed=_int_or_zero(stats.get("notNeeded")),
        error_count=len(errors),
        errors=parsed_errors,
    )


def _pipeline_run_out(run: PipelineRun) -> PipelineRunOut:
    usage = _parse_json_dict(run.usage_json)
    cost = usage.get("est_cost_usd")
    return PipelineRunOut(
        id=run.id,
        stage=run.stage,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        input_tokens=_int_or_zero(usage.get("input_tokens")),
        output_tokens=_int_or_zero(usage.get("output_tokens")),
        est_cost_usd=float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else 0.0,
        error=run.error,
    )


@router.get("/{course_id}/runs", response_model=RunsOut)
def get_course_runs(course_id: int, session: Session = Depends(get_session)) -> RunsOut:
    if session.get(Course, course_id) is None:
        raise HTTPException(status_code=404, detail="unknown course")
    sync_runs = session.execute(
        select(SyncRun).where(SyncRun.course_id == course_id).order_by(SyncRun.id.desc()).limit(_RUNS_LIMIT)
    ).scalars().all()
    pipeline_runs = session.execute(
        select(PipelineRun)
        .where(PipelineRun.course_id == course_id)
        .order_by(PipelineRun.id.desc())
        .limit(_RUNS_LIMIT)
    ).scalars().all()
    return RunsOut(
        sync_runs=[_sync_run_out(run) for run in sync_runs],
        pipeline_runs=[_pipeline_run_out(run) for run in pipeline_runs],
    )
