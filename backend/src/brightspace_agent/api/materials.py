"""`GET /api/materials/{id}` (+ `/file`, `/text`): the frontend's material
detail view, the raw blob (streamed, for previewing in an iframe), and its
extracted-text sidecar.

`/file` serves untrusted third-party bytes from this server's own origin,
so how they're delivered is a security decision, not a convenience one --
see `_file_delivery`.
"""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import PlainTextResponse, StreamingResponse

from brightspace_agent.api.deps import get_blob_store, get_session
from brightspace_agent.db.models import Course, Material, MaterialTopic, MediaSource
from brightspace_agent.ingest.store import BlobStore

router = APIRouter(prefix="/api/materials", tags=["materials"])

_CONTENT_DISPOSITION_UNSAFE = re.compile(r'[\r\n"]')


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class MaterialOut(CamelModel):
    id: int
    course_id: int
    title: str
    kind: str
    status: str
    mime: str | None
    size_bytes: int | None
    source_url: str | None
    summary: str | None
    key_terms: list[str]
    topic_ids: list[int]
    # M3.5b: recording linkage, for the frontend's "open recording" action.
    # A plain dict (not a nested CamelModel) on purpose -- the two shapes
    # this can take ({url, status, transcriptMaterialId} vs {url, status,
    # sourceMaterialId}, see `_recording_info`) share no field beyond
    # url/status, and a single model with both id fields would always
    # serialize both keys (one of them null) instead of only the one that
    # applies.
    recording: dict[str, str | int | None] | None = None


def _key_terms(material: Material) -> list[str]:
    try:
        meta = json.loads(material.summary_meta_json or "{}")
    except json.JSONDecodeError:
        return []
    terms = meta.get("key_terms") or []
    return [str(term) for term in terms if str(term).strip()]


def _current_topic_ids(session: Session, material: Material) -> list[int]:
    course = session.get(Course, material.course_id)
    version = course.taxonomy_version if course is not None else 0
    return list(
        session.execute(
            select(MaterialTopic.topic_id).where(
                MaterialTopic.material_id == material.id, MaterialTopic.taxonomy_version == version
            )
        ).scalars().all()
    )


def _get_material_or_404(session: Session, material_id: int) -> Material:
    material = session.get(Material, material_id)
    if material is None:
        raise HTTPException(status_code=404, detail="unknown material")
    return material


def _most_recent_media_source(
    session: Session, *, material_id: int | None, transcript_material_id: int | None
) -> MediaSource | None:
    """The `media_sources` row for the given `material_id`/
    `transcript_material_id` filter, most recently updated first -- exactly
    one of the two kwargs is non-None per call site below."""
    stmt = select(MediaSource)
    if material_id is not None:
        stmt = stmt.where(MediaSource.material_id == material_id)
    else:
        stmt = stmt.where(MediaSource.transcript_material_id == transcript_material_id)
    stmt = stmt.order_by(MediaSource.updated_at.desc(), MediaSource.id.desc()).limit(1)
    return session.execute(stmt).scalar_one_or_none()


def _recording_info(session: Session, material_id: int) -> dict[str, str | int | None] | None:
    """The recording linkage for `material_id`, if it is either end of a
    `media_sources` row (M3.5b): `{url, status, transcriptMaterialId}` if
    it's a recording's own source material, `{url, status,
    sourceMaterialId}` if it's the transcript. A material only ever matches
    one of the two (they come from different flows -- a synced link/page vs.
    an app-created transcript material), but if several `media_sources` rows
    happen to point at the same material, the most recently updated one
    wins.
    """
    as_source = _most_recent_media_source(session, material_id=material_id, transcript_material_id=None)
    if as_source is not None:
        return {
            "url": as_source.url,
            "status": as_source.status,
            "transcriptMaterialId": as_source.transcript_material_id,
        }

    as_transcript = _most_recent_media_source(session, material_id=None, transcript_material_id=material_id)
    if as_transcript is not None:
        return {
            "url": as_transcript.url,
            "status": as_transcript.status,
            "sourceMaterialId": as_transcript.material_id,
        }

    return None


@router.get("/{material_id}", response_model=MaterialOut)
def get_material(material_id: int, session: Session = Depends(get_session)) -> MaterialOut:
    material = _get_material_or_404(session, material_id)
    return MaterialOut(
        id=material.id,
        course_id=material.course_id,
        title=material.title,
        kind=material.kind,
        status=material.status,
        mime=material.mime,
        size_bytes=material.size_bytes,
        source_url=material.source_url,
        summary=material.summary,
        key_terms=_key_terms(material),
        topic_ids=_current_topic_ids(session, material),
        recording=_recording_info(session, material.id),
    )


@router.get("/{material_id}/file")
def get_material_file(
    material_id: int, session: Session = Depends(get_session), blob_store: BlobStore = Depends(get_blob_store)
) -> StreamingResponse:
    material = _get_material_or_404(session, material_id)
    if not material.sha256:
        raise HTTPException(status_code=404, detail="no file for this material")
    blob_path = blob_store.path_for(material.sha256)
    if not blob_path.exists():
        raise HTTPException(status_code=404, detail="no file for this material")

    filename = _CONTENT_DISPOSITION_UNSAFE.sub("", material.title or "material")
    media_type, disposition = _file_delivery(material.mime)
    return StreamingResponse(
        blob_path.open("rb"),
        media_type=media_type,
        headers={
            "Content-Disposition": f'{disposition}; filename="{filename}"',
            # Never let a browser second-guess the declared type back into
            # something scriptable.
            "X-Content-Type-Options": "nosniff",
            # `sandbox` with no tokens: even if something did render, it
            # renders in an opaque origin with scripts, forms and top-level
            # navigation disabled -- so it cannot reach this origin's own
            # endpoints. Applied to every /file response, PDFs included:
            # measured against the same request with no CSP at all, this
            # header made no difference to how Chromium handled an
            # iframe'd PDF.
            "Content-Security-Policy": "sandbox",
        },
    )


def _file_delivery(mime: str | None) -> tuple[str, str]:
    """(media_type, content_disposition_type) for a stored blob.

    Course materials are arbitrary files a tenant served us, and this route
    hands them back on the BACKEND's own origin -- the same origin as
    `GET /api/settings`, which returns the pairing token. Serving a
    `text/html` material inline meant any HTML a course happened to contain
    (or that anyone got into a course) executed with full read access to
    that endpoint.

    So: `application/pdf` alone keeps its real type and renders inline --
    it's the only type the reader ever embeds (see the frontend's
    `chooseReaderMode`), and the PDF viewer is not a script host. Everything
    else is downgraded to `application/octet-stream` and marked
    `attachment`, which the frontend's only other use of this route (an
    `<a download>`) is happy with.
    """
    normalized = (mime or "").split(";")[0].strip().lower()
    if normalized == "application/pdf":
        return "application/pdf", "inline"
    return "application/octet-stream", "attachment"


@router.get("/{material_id}/text")
def get_material_text(
    material_id: int, session: Session = Depends(get_session), blob_store: BlobStore = Depends(get_blob_store)
) -> PlainTextResponse:
    material = _get_material_or_404(session, material_id)
    text = blob_store.read_text(material.sha256) if material.sha256 else None
    if text is None:
        raise HTTPException(status_code=404, detail="no extracted text for this material")
    return PlainTextResponse(text)
