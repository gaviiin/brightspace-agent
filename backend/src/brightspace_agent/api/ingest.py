"""The extension-facing ingest API: handshake -> toc (diff) -> per-file
streaming upload -> complete. All routes require the pairing token (see
`deps.require_pairing_token`, applied at the router level below).

Wire format is camelCase JSON (`CamelModel`); this module only handles HTTP
concerns (request/response shapes, header parsing, streaming the upload to
disk) -- DB upsert logic lives in `ingest/repo.py`, ToC parsing/diffing in
`ingest/diff.py`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from email.header import decode_header
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from sqlalchemy.orm import Session

from brightspace_agent.api.deps import get_blob_store, get_session, require_pairing_token
from brightspace_agent.db.models import SyncRun
from brightspace_agent.ingest import repo
from brightspace_agent.ingest.diff import compute_needed, is_file_topic, parse_toc
from brightspace_agent.ingest.store import BlobStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ingest", dependencies=[Depends(require_pairing_token)])


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


# --------------------------------------------------------------------------
# /handshake
# --------------------------------------------------------------------------


class EnrollmentIn(CamelModel):
    org_unit_id: int
    name: str
    code: str | None = None


class HandshakeRequest(CamelModel):
    tenant_origin: str
    api_versions: dict[str, Any] = Field(default_factory=dict)
    whoami: dict[str, Any] = Field(default_factory=dict)
    enrollments: list[EnrollmentIn] = Field(default_factory=list)


class KnownCourse(CamelModel):
    org_unit_id: int
    name: str
    course_id: int


class HandshakeResponse(CamelModel):
    known_courses: list[KnownCourse]


@router.post("/handshake", response_model=HandshakeResponse)
def handshake(payload: HandshakeRequest, session: Session = Depends(get_session)) -> HandshakeResponse:
    # apiVersions/whoami aren't stored anywhere yet -- just logged so a real
    # sync's tenant/capability info is visible for debugging.
    logger.info(
        "handshake tenant_origin=%s api_versions=%s whoami=%s",
        payload.tenant_origin, payload.api_versions, payload.whoami,
    )

    known_courses = []
    for enrollment in payload.enrollments:
        course = repo.upsert_course_enrollment(
            session, payload.tenant_origin, enrollment.org_unit_id, enrollment.name, enrollment.code
        )
        known_courses.append(
            KnownCourse(org_unit_id=course.d2l_org_unit_id, name=course.name, course_id=course.id)
        )
    session.commit()
    return HandshakeResponse(known_courses=known_courses)


# --------------------------------------------------------------------------
# /toc
# --------------------------------------------------------------------------


class NewsExtra(CamelModel):
    id: int
    title: str
    html: str


class DropboxExtra(CamelModel):
    id: int
    name: str
    instructions_text: str | None = None


class Extras(CamelModel):
    news: list[NewsExtra] | None = None
    dropbox: list[DropboxExtra] | None = None


class TocRequest(CamelModel):
    org_unit_id: int
    toc: dict[str, Any]
    extras: Extras | None = None


class NeededItemOut(CamelModel):
    # Explicit alias: pydantic's to_camel("d2l_topic_id") -> "d2LTopicId"
    # (str.title() treats the digit in "d2l" as a word boundary), not the
    # "d2lTopicId" the wire contract requires.
    d2l_topic_id: int = Field(alias="d2lTopicId")
    url: str
    title: str
    size_hint: int | None = None
    # Explicit alias too (belt-and-suspenders with the d2l_topic_id case
    # above): to_camel("last_modified") does produce "lastModified" with no
    # digit-boundary quirk, but the wire contract is pinned here rather than
    # left to alias-generator inference so a future pydantic/to_camel change
    # can't silently rename this field out from under the extension.
    last_modified: str | None = Field(default=None, alias="lastModified")


class TocResponse(CamelModel):
    sync_run_id: int
    needed: list[NeededItemOut]


@router.post("/toc", response_model=TocResponse)
def ingest_toc(
    payload: TocRequest,
    session: Session = Depends(get_session),
    blob_store: BlobStore = Depends(get_blob_store),
) -> TocResponse:
    course = repo.get_course_by_org_unit(session, payload.org_unit_id)
    if course is None:
        raise HTTPException(status_code=404, detail="unknown course; run handshake first")

    course.toc_json = json.dumps(payload.toc)
    course.last_synced_at = repo.now_iso()

    entries = parse_toc(payload.toc)
    modules_by_d2l_id = repo.upsert_modules_from_entries(session, course.id, entries)

    for entry in entries:
        module_id = repo.resolve_module_id(entry, modules_by_d2l_id)
        if entry.type_identifier == "Link":
            repo.upsert_link_material(session, course.id, entry, module_id)
        elif is_file_topic(entry):
            # Stub the material row now (module/title/source_url; sha256
            # stays NULL until /file uploads it) so the upload endpoint
            # always updates an existing row instead of creating one, and
            # so File materials carry module_id like Link materials do.
            repo.upsert_file_stub_material(session, course.id, entry, module_id)

    file_topic_count = sum(1 for e in entries if is_file_topic(e))
    needed_entries = compute_needed(session, course.id, entries)
    not_needed_count = file_topic_count - len(needed_entries)

    if payload.extras is not None:
        for news in payload.extras.news or []:
            repo.upsert_text_material(
                session, blob_store, course.id,
                source_url=f"d2l:news:{news.id}", title=news.title,
                kind="announcement", body=news.html, mime="text/html",
            )
        for dropbox in payload.extras.dropbox or []:
            repo.upsert_text_material(
                session, blob_store, course.id,
                source_url=f"d2l:dropbox:{dropbox.id}", title=dropbox.name,
                kind="assignment", body=dropbox.instructions_text or "", mime="text/plain",
            )

    sync_run = repo.create_sync_run(session, course.id, not_needed_count)
    session.commit()

    return TocResponse(
        sync_run_id=sync_run.id,
        needed=[
            NeededItemOut(
                d2l_topic_id=i.d2l_topic_id,
                url=i.url,
                title=i.title,
                size_hint=i.size_hint,
                last_modified=i.last_modified,
            )
            for i in needed_entries
        ],
    )


# --------------------------------------------------------------------------
# /file
# --------------------------------------------------------------------------


class FileUploadResponse(CamelModel):
    material_id: int
    sha256: str
    deduped: bool


@router.post("/file", response_model=FileUploadResponse)
async def upload_file(
    request: Request,
    sync_run_id: int = Query(alias="syncRunId"),
    d2l_topic_id: int = Query(alias="d2lTopicId"),
    session: Session = Depends(get_session),
    blob_store: BlobStore = Depends(get_blob_store),
) -> FileUploadResponse:
    sync_run = session.get(SyncRun, sync_run_id)
    if sync_run is None:
        raise HTTPException(status_code=404, detail="unknown sync run")
    if sync_run.status != "running":
        raise HTTPException(status_code=409, detail="sync run is not running")

    source_url = request.headers.get("X-Source-Url")
    raw_title = request.headers.get("X-Title")
    if not source_url or not raw_title:
        raise HTTPException(status_code=422, detail="X-Source-Url and X-Title headers are required")
    title = _decode_header_value(raw_title)
    content_type = request.headers.get("Content-Type")
    d2l_updated_at = request.headers.get("X-D2L-Updated")

    sha256, size, deduped = await _spool_hash_and_store(request, blob_store)

    material = repo.upsert_file_material(
        session,
        course_id=sync_run.course_id,
        d2l_topic_id=d2l_topic_id,
        sha256=sha256,
        mime=content_type,
        size_bytes=size,
        source_url=source_url,
        title=title,
        d2l_updated_at=d2l_updated_at,
    )
    repo.record_file_upload_stats(session, sync_run, size)
    session.commit()

    return FileUploadResponse(material_id=material.id, sha256=sha256, deduped=deduped)


async def _spool_hash_and_store(request: Request, blob_store: BlobStore) -> tuple[str, int, bool]:
    """Stream the request body to a local temp file in chunks (never
    buffering the whole thing in memory), hash it, then hand it to the
    blob store. Returns (sha256, size, deduped)."""
    tmp_fd, tmp_name = tempfile.mkstemp(dir=blob_store.blobs_dir)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "wb") as tmp_file:
            async for chunk in request.stream():
                tmp_file.write(chunk)

        sha256, size = _hash_file(tmp_path)
        deduped = blob_store.exists(sha256)
        with tmp_path.open("rb") as f:
            blob_store.put_stream(f)
    finally:
        tmp_path.unlink(missing_ok=True)

    return sha256, size, deduped


def _hash_file(path: Path, chunk_size: int = 1024 * 1024) -> tuple[str, int]:
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while chunk := f.read(chunk_size):
            hasher.update(chunk)
            size += len(chunk)
    return hasher.hexdigest(), size


def _decode_header_value(value: str) -> str:
    """Decode an X-Title header that may be RFC 2047 encoded-word syntax
    (e.g. "=?UTF-8?B?...?=") or percent-encoded UTF-8 (e.g. "Caf%C3%A9").

    HTTP header values are ASCII-only, so a non-ASCII title has to arrive
    encoded one of these two ways. We try RFC 2047 first via the stdlib
    (which safely no-ops on plain ASCII text), then percent-decoding,
    falling back to the raw value if neither changes anything -- so a
    plain ASCII title always passes through untouched.
    """
    if not value:
        return value

    try:
        parts = decode_header(value)
        decoded = "".join(
            part.decode(encoding or "utf-8") if isinstance(part, bytes) else part
            for part, encoding in parts
        )
        if decoded != value:
            return decoded
    except (LookupError, ValueError):
        pass

    try:
        decoded = unquote(value, encoding="utf-8", errors="strict")
        if decoded != value:
            return decoded
    except (UnicodeDecodeError, ValueError):
        pass

    return value


# --------------------------------------------------------------------------
# /complete
# --------------------------------------------------------------------------


class ErrorItem(CamelModel):
    # See NeededItemOut for why this needs an explicit alias.
    d2l_topic_id: int | None = Field(default=None, alias="d2lTopicId")
    message: str


class CompleteRequest(CamelModel):
    sync_run_id: int
    errors: list[ErrorItem] = Field(default_factory=list)


class CompleteResponse(CamelModel):
    status: str
    stats: dict[str, Any]


@router.post("/complete", response_model=CompleteResponse)
def complete_sync(
    payload: CompleteRequest, request: Request, session: Session = Depends(get_session)
) -> CompleteResponse:
    sync_run = session.get(SyncRun, payload.sync_run_id)
    if sync_run is None:
        raise HTTPException(status_code=404, detail="unknown sync run")

    errors = [error.model_dump(by_alias=True) for error in payload.errors]
    stats = repo.finalize_sync_run(session, sync_run, errors)
    session.commit()

    # Task 9 hook: the frontend's SSE feed learns about sync completion the
    # same way it learns about pipeline progress -- one shared bus.
    request.app.state.event_bus.publish(
        {
            "type": "sync",
            "courseId": sync_run.course_id,
            "syncRunId": sync_run.id,
            "status": sync_run.status,
        }
    )

    return CompleteResponse(status=sync_run.status, stats=stats)
