"""`GET /api/materials/{id}` (+ `/file`, `/text`): the frontend's material
detail view, the raw blob (streamed, for previewing in an iframe), and its
extracted-text sidecar.
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
    return StreamingResponse(
        blob_path.open("rb"),
        media_type=material.mime or "application/octet-stream",
        # inline (not attachment): the frontend previews this in an iframe.
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.get("/{material_id}/text")
def get_material_text(
    material_id: int, session: Session = Depends(get_session), blob_store: BlobStore = Depends(get_blob_store)
) -> PlainTextResponse:
    material = _get_material_or_404(session, material_id)
    text = blob_store.read_text(material.sha256) if material.sha256 else None
    if text is None:
        raise HTTPException(status_code=404, detail="no extracted text for this material")
    return PlainTextResponse(text)
