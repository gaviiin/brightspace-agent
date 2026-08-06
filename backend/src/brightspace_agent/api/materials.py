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
from brightspace_agent.db.models import Course, Material, MaterialTopic
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
