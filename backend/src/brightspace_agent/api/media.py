"""M2.4 media endpoints: the read model for a course's detected recording
sources, the detect/process triggers (course-batch and single-source), and
the per-source edit endpoint (passcode, skip/unskip).

POST/PUT routes here are covered by main.py's CSRF guard (require
`X-BSA-Request: 1`) like every other mutating `/api/*` route -- see
api/enrichment.py's module docstring for the same reasoning. The GET list
stays open to the same browser+CORS rules as the rest of the app.
"""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from sqlalchemy import select
from sqlalchemy.orm import Session

from brightspace_agent.api.deps import get_media_fetcher, get_runner, get_session
from brightspace_agent.db.models import Course, Material, MediaSource
from brightspace_agent.ingest.repo import now_iso
from brightspace_agent.media.detect import classify_url, detect_media_sources
from brightspace_agent.media.fetch import MediaFetcher, MediaFetchError
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
    # Both nullable as of M2.6a: a manually-added URL/channel row (POST
    # .../media/add below) has no backing `materials` row at all.
    material_id: int | None
    material_title: str | None
    platform: str
    url: str
    passcode: str | None  # local single-user app, no masking
    status: str
    error: str | None
    transcript_material_id: int | None
    updated_at: str


class MediaHintOut(CamelModel):
    """A link material that LOOKS like an LTI-embedded recording channel the
    detector structurally cannot see into (its `source_url` is a D2L
    quicklink, not the channel's real URL) -- see `_compute_lti_hints`."""

    material_id: int
    title: str


class MediaListOut(CamelModel):
    sources: list[MediaSourceOut]
    active: bool
    hints: list[MediaHintOut]


def _media_source_out(source: MediaSource, material_title: str | None) -> MediaSourceOut:
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


# `type=lti`/`/lti/` in the source_url (an LTI-embedded tool, which is
# exactly why the detector can't see through it -- the synced value is a D2L
# quicklink, never the tool's own page) AND a title that reads like a
# recording channel. Both checks are case-insensitive; see this task's brief
# for the real-world shape ("Mediasite Channel (Stern)").
_LTI_URL_MARKERS = ("type=lti", "/lti/")
_LTI_HINT_TITLE_RE = re.compile(r"(mediasite|zoom|panopto|echo|yuja|kaltura|recording|lecture)", re.IGNORECASE)


def _compute_lti_hints(session: Session, course_id: int) -> list[MediaHintOut]:
    rows = session.execute(
        select(Material.id, Material.title, Material.source_url).where(
            Material.course_id == course_id, Material.kind == "link", Material.source_url.is_not(None)
        )
    ).all()
    hints: list[MediaHintOut] = []
    for material_id, title, source_url in rows:
        lowered_url = source_url.lower()
        if not any(marker in lowered_url for marker in _LTI_URL_MARKERS):
            continue
        if not _LTI_HINT_TITLE_RE.search(title):
            continue
        hints.append(MediaHintOut(material_id=material_id, title=title))
    return hints


@router.get("/api/courses/{course_id}/media", response_model=MediaListOut)
def list_media_sources(
    course_id: int, session: Session = Depends(get_session), runner: PipelineRunner = Depends(get_runner)
) -> MediaListOut:
    _get_course_or_404(session, course_id)
    rows = session.execute(
        select(MediaSource, Material.title)
        # LEFT (not INNER) join: a manually-added row's material_id is NULL,
        # and it must still appear in the list, just with material_title
        # NULL too -- an INNER join would silently drop it.
        .outerjoin(Material, MediaSource.material_id == Material.id)
        .where(MediaSource.course_id == course_id)
        .order_by(MediaSource.id.desc())
    ).all()
    sources = [_media_source_out(source, title) for source, title in rows]
    hints = _compute_lti_hints(session, course_id)
    return MediaListOut(sources=sources, active=runner.is_active(course_id), hints=hints)


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
# POST /api/courses/{course_id}/media/add -- M2.6a manual URL/channel add.
#
# Exists for exactly the case the detector structurally cannot cover: a
# recording sitting behind an LTI-embedded channel (see this task's brief),
# whose synced link material carries a D2L quicklink as its source_url, never
# the channel's real address. The user pastes the real URL themselves -- a
# single recording's page, or a channel/catalog page that `fetcher.expand`
# turns into one entry per lecture -- and it's classified with the SAME
# `classify_url` the passive detector uses, so the two paths can never
# disagree about what counts as a supported platform.
#
# `fetcher.expand` runs synchronously, in-request: bounded by
# `settings.media_fetch_timeout_s` (1800s by default, same bound `fetch()`
# itself uses -- see media/fetch.py), same tradeoff `detect`'s own endpoint
# already makes for a local single-user app with no request queue to protect.
# --------------------------------------------------------------------------


class MediaAddRequest(CamelModel):
    url: str
    passcode: str | None = None


class MediaAddResponse(CamelModel):
    added: int
    skipped: int
    total: int
    sources: list[MediaSourceOut]


def _require_absolute_http_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=422, detail="url must be an absolute http:// or https:// URL")


def _map_expand_error(exc: MediaFetchError) -> HTTPException:
    status_code = 503 if exc.kind == "not_installed" else 502
    return HTTPException(status_code=status_code, detail=exc.user_message)


def _upsert_manual_media_source(
    session: Session, course_id: int, platform: str, url: str, passcode: str | None
) -> tuple[MediaSource, bool]:
    """Insert or fold `url` into `media_sources` for `course_id` with
    `material_id=NULL` -- the manual-add counterpart of detect.py's
    `_upsert`, which always has a backing material to point at. Same "never
    clobber status/error/transcript progress" rule on an existing row;
    unlike detect.py's fill-only-if-NULL passcode rule (a passively
    re-scanned page can't tell you changed your mind), a request-supplied
    passcode here is a deliberate user action and always overwrites.
    Returns (row, True) if a new row was inserted, (row, False) if an
    existing one was folded into."""
    existing = session.execute(
        select(MediaSource).where(MediaSource.course_id == course_id, MediaSource.url == url)
    ).scalar_one_or_none()
    now = now_iso()

    if existing is None:
        row = MediaSource(
            course_id=course_id, material_id=None, platform=platform, url=url, passcode=passcode,
            created_at=now, updated_at=now,
        )
        session.add(row)
        session.flush()
        return row, True

    if passcode is not None and existing.passcode != passcode:
        existing.passcode = passcode
        existing.updated_at = now
    return existing, False


@router.post("/api/courses/{course_id}/media/add", response_model=MediaAddResponse)
def add_media_url(
    course_id: int,
    payload: MediaAddRequest,
    session: Session = Depends(get_session),
    fetcher: MediaFetcher = Depends(get_media_fetcher),
) -> MediaAddResponse:
    _get_course_or_404(session, course_id)
    _require_absolute_http_url(payload.url)

    try:
        entries = fetcher.expand(payload.url)
    except MediaFetchError as exc:
        raise _map_expand_error(exc) from exc

    total = len(entries)
    skipped = 0
    added = 0
    touched: list[MediaSource] = []

    for entry in entries:
        candidate = classify_url(entry.url)
        if candidate is None:
            skipped += 1
            continue

        passcode = candidate.passcode
        if candidate.platform == "zoom" and payload.passcode is not None:
            passcode = payload.passcode

        row, inserted = _upsert_manual_media_source(session, course_id, candidate.platform, candidate.url, passcode)
        touched.append(row)
        if inserted:
            added += 1

    if total - skipped == 0:
        raise HTTPException(
            status_code=400,
            detail="That URL wasn't recognized as a supported recording platform (Mediasite, Zoom, or Google Drive).",
        )

    session.commit()
    for row in touched:
        session.refresh(row)

    sources = [
        _media_source_out(row, _material_title_or_none(session, row.material_id))
        for row in touched
    ]
    return MediaAddResponse(added=added, skipped=skipped, total=total, sources=sources)


def _material_title_or_none(session: Session, material_id: int | None) -> str | None:
    if material_id is None:
        return None
    material = session.get(Material, material_id)
    return material.title if material is not None else None


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

    fields_set = payload.model_fields_set
    # `status` is typed `Literal["skipped", "detected"] | None` only so an
    # ABSENT field (the "leave it alone" case `fields_set` distinguishes
    # from an explicit value, same as `passcode` below) parses -- the
    # contract itself never allows an explicit `null`, which would
    # otherwise reach `media_sources.status`, a NOT NULL column, and 500 on
    # commit. Rejected here, before touching the row or the active-run
    # guard, since it's a malformed request regardless of server state.
    if "status" in fields_set and payload.status is None:
        raise HTTPException(status_code=422, detail="status cannot be null; use 'skipped' or 'detected'")

    if runner.is_active(source.course_id):
        raise HTTPException(status_code=409, detail="a run is already active for this course")

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

    material = session.get(Material, source.material_id) if source.material_id is not None else None
    return _media_source_out(source, material.title if material is not None else None)
