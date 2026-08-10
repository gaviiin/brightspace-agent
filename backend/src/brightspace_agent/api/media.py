"""M2.4 media endpoints: the read model for a course's detected recording
sources, the detect/process triggers (course-batch and single-source), and
the per-source edit endpoint (passcode, skip/unskip).

POST/PUT routes here are covered by main.py's CSRF guard (require
`X-BSA-Request: 1`) like every other mutating `/api/*` route -- see
api/enrichment.py's module docstring for the same reasoning. The GET list
stays open to the same browser+CORS rules as the rest of the app.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from sqlalchemy import select
from sqlalchemy.orm import Session

from brightspace_agent.api.deps import get_runner, get_session
from brightspace_agent.db.models import Course, Material, MediaSource
from brightspace_agent.ingest.repo import now_iso
from brightspace_agent.media.detect import detect_media_sources
from brightspace_agent.pipeline.runner import (
    MediaSourceNotFoundError,
    NoMediaToProcessError,
    PipelineRunner,
    RunActiveError,
)

router = APIRouter(tags=["media"])

# Status values a `PUT /api/media/{source_id}` status transition may start
# FROM (see update_media_source: the target is restricted to detected/
# skipped by MediaSourceUpdate's Literal already -- this is the other half,
# "not while the job itself owns the row").
_EDITABLE_STATUSES = {"detected", "failed", "skipped"}


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


def _get_course_or_404(session: Session, course_id: int) -> Course:
    course = session.get(Course, course_id)
    if course is None:
        raise HTTPException(status_code=404, detail="unknown course")
    return course


def _get_media_source_or_404(session: Session, source_id: int) -> MediaSource:
    source = session.get(MediaSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="unknown media source")
    return source


# --------------------------------------------------------------------------
# GET /api/courses/{course_id}/media
# --------------------------------------------------------------------------


class MediaSourceOut(CamelModel):
    id: int
    material_id: int
    material_title: str
    platform: str
    url: str
    passcode: str | None  # local single-user app, no masking
    status: str
    error: str | None
    transcript_material_id: int | None
    updated_at: str


class MediaListOut(CamelModel):
    sources: list[MediaSourceOut]
    active: bool


def _media_source_out(source: MediaSource, material_title: str) -> MediaSourceOut:
    return MediaSourceOut(
        id=source.id,
        material_id=source.material_id,
        material_title=material_title,
        platform=source.platform,
        url=source.url,
        passcode=source.passcode,
        status=source.status,
        error=source.error,
        transcript_material_id=source.transcript_material_id,
        updated_at=source.updated_at,
    )


@router.get("/api/courses/{course_id}/media", response_model=MediaListOut)
def list_media_sources(
    course_id: int, session: Session = Depends(get_session), runner: PipelineRunner = Depends(get_runner)
) -> MediaListOut:
    _get_course_or_404(session, course_id)
    rows = session.execute(
        select(MediaSource, Material.title)
        .join(Material, MediaSource.material_id == Material.id)
        .where(MediaSource.course_id == course_id)
        .order_by(MediaSource.id.desc())
    ).all()
    sources = [_media_source_out(source, title) for source, title in rows]
    return MediaListOut(sources=sources, active=runner.is_active(course_id))


# --------------------------------------------------------------------------
# POST /api/courses/{course_id}/media/detect
# --------------------------------------------------------------------------


class DetectResponse(CamelModel):
    scanned_materials: int
    found: int
    added: int


@router.post("/api/courses/{course_id}/media/detect", response_model=DetectResponse)
def detect_course_media(course_id: int, request: Request, session: Session = Depends(get_session)) -> DetectResponse:
    _get_course_or_404(session, course_id)
    stats = detect_media_sources(request.app.state.session_factory, course_id)
    return DetectResponse(scanned_materials=stats.scanned_materials, found=stats.found, added=stats.added)


# --------------------------------------------------------------------------
# POST /api/courses/{course_id}/media/process, POST /api/media/{source_id}/process
# --------------------------------------------------------------------------


class MediaRunResponse(CamelModel):
    run_token: int


@router.post("/api/courses/{course_id}/media/process", response_model=MediaRunResponse)
async def start_course_media(
    course_id: int, session: Session = Depends(get_session), runner: PipelineRunner = Depends(get_runner)
) -> MediaRunResponse:
    _get_course_or_404(session, course_id)
    try:
        run_token = runner.start_media(course_id)
    except RunActiveError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except NoMediaToProcessError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return MediaRunResponse(run_token=run_token)


@router.post("/api/media/{source_id}/process", response_model=MediaRunResponse)
async def start_single_media(
    source_id: int, session: Session = Depends(get_session), runner: PipelineRunner = Depends(get_runner)
) -> MediaRunResponse:
    source = _get_media_source_or_404(session, source_id)
    try:
        run_token = runner.start_media(source.course_id, [source_id])
    except RunActiveError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MediaSourceNotFoundError as exc:  # pragma: no cover -- defensive; source
        # already resolved above, this only fires on a same-request race.
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except NoMediaToProcessError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return MediaRunResponse(run_token=run_token)


# --------------------------------------------------------------------------
# PUT /api/media/{source_id}: passcode + skip/unskip
# --------------------------------------------------------------------------


class MediaSourceUpdate(CamelModel):
    # Both fields default to "not provided" -- `model_fields_set` (checked
    # below) is what distinguishes an absent field from an explicit `null`,
    # since `passcode: null` must clear the stored passcode rather than be
    # indistinguishable from "leave it alone".
    passcode: str | None = None
    status: Literal["skipped", "detected"] | None = None


@router.put("/api/media/{source_id}", response_model=MediaSourceOut)
def update_media_source(
    source_id: int,
    payload: MediaSourceUpdate,
    session: Session = Depends(get_session),
    runner: PipelineRunner = Depends(get_runner),
) -> MediaSourceOut:
    source = _get_media_source_or_404(session, source_id)

    if runner.is_active(source.course_id):
        raise HTTPException(status_code=409, detail="a run is already active for this course")

    fields_set = payload.model_fields_set
    if "status" in fields_set:
        if source.status not in _EDITABLE_STATUSES:
            raise HTTPException(
                status_code=409, detail=f"cannot change status while the source is '{source.status}'"
            )
        source.status = payload.status

    if "passcode" in fields_set:
        source.passcode = payload.passcode

    if fields_set:
        source.updated_at = now_iso()
        session.commit()
        session.refresh(source)

    material = session.get(Material, source.material_id)
    return _media_source_out(source, material.title if material is not None else "")
