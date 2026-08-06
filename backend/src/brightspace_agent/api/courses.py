"""`GET /api/courses` and `GET /api/courses/{id}`: the frontend's course
list/detail read model -- course metadata, material counts by status, and a
compact summary of the course's latest pipeline run (if any).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from brightspace_agent.api.deps import get_runner, get_session
from brightspace_agent.db.models import Course, Material
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
