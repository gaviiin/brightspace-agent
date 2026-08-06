"""DB upsert helpers for the ingest API.

Split out of `api/ingest.py` so the HTTP layer stays focused on request/
response concerns; this module owns the read-modify-write upsert logic for
courses, modules, materials, and sync runs. All functions `flush()` (not
`commit()`) -- the caller (an ingest.py endpoint) owns the transaction
boundary and commits once, after everything for that request is staged.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from brightspace_agent.db.models import Course, Material, Module, SyncRun
from brightspace_agent.ingest.diff import ModuleRef, TocEntry, infer_kind
from brightspace_agent.ingest.store import BlobStore


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Courses
# --------------------------------------------------------------------------


def get_course_by_org_unit(session: Session, org_unit_id: int) -> Course | None:
    return session.execute(
        select(Course).where(Course.d2l_org_unit_id == org_unit_id)
    ).scalar_one_or_none()


def upsert_course_enrollment(
    session: Session, tenant_origin: str, org_unit_id: int, name: str, code: str | None
) -> Course:
    course = get_course_by_org_unit(session, org_unit_id)
    if course is None:
        course = Course(d2l_org_unit_id=org_unit_id, tenant_origin=tenant_origin, name=name, code=code)
        session.add(course)
    else:
        course.tenant_origin = tenant_origin
        course.name = name
        course.code = code
    session.flush()
    return course


# --------------------------------------------------------------------------
# Modules
# --------------------------------------------------------------------------


def _upsert_module(session: Session, course_id: int, module_ref: ModuleRef, parent_id: int | None) -> Module:
    module = session.execute(
        select(Module).where(
            Module.course_id == course_id, Module.d2l_module_id == module_ref.d2l_module_id
        )
    ).scalar_one_or_none()
    if module is None:
        module = Module(
            course_id=course_id,
            d2l_module_id=module_ref.d2l_module_id,
            parent_id=parent_id,
            title=module_ref.title,
            sort_order=module_ref.sort_order,
        )
        session.add(module)
    else:
        module.parent_id = parent_id
        module.title = module_ref.title
        module.sort_order = module_ref.sort_order
    session.flush()
    return module


def upsert_modules_from_entries(session: Session, course_id: int, entries: list[TocEntry]) -> dict[int, Module]:
    """Upsert every module referenced by any entry's `module_chain`,
    preserving parent relationships and sibling order. Modules with no
    topics anywhere in their subtree are not represented in any chain and
    so are not upserted (the ToC walk only reaches modules via topics).

    Returns d2l_module_id -> Module, so callers can resolve a topic's
    immediate parent module (see `resolve_module_id`)."""
    by_d2l_id: dict[int, Module] = {}
    for entry in entries:
        parent_db_id: int | None = None
        for module_ref in entry.module_chain:
            existing = by_d2l_id.get(module_ref.d2l_module_id)
            if existing is not None:
                parent_db_id = existing.id
                continue
            module = _upsert_module(session, course_id, module_ref, parent_db_id)
            by_d2l_id[module_ref.d2l_module_id] = module
            parent_db_id = module.id
    return by_d2l_id


def resolve_module_id(entry: TocEntry, modules_by_d2l_id: dict[int, Module]) -> int | None:
    """The DB id of `entry`'s immediate parent module, if it has one and it
    was resolved by `upsert_modules_from_entries`."""
    if not entry.module_chain:
        return None
    immediate = entry.module_chain[-1]
    module = modules_by_d2l_id.get(immediate.d2l_module_id)
    return module.id if module else None


# --------------------------------------------------------------------------
# Materials -- Link topics (created eagerly at /toc time)
# --------------------------------------------------------------------------


def upsert_link_material(
    session: Session, course_id: int, entry: TocEntry, module_id: int | None = None
) -> Material:
    material = session.execute(
        select(Material).where(Material.course_id == course_id, Material.d2l_topic_id == entry.topic_id)
    ).scalar_one_or_none()
    if material is None:
        material = Material(
            course_id=course_id,
            d2l_topic_id=entry.topic_id,
            module_id=module_id,
            kind="link",
            title=entry.title,
            source_url=entry.url,
            status="fetched",
        )
        session.add(material)
    else:
        material.module_id = module_id
        material.kind = "link"
        material.title = entry.title
        material.source_url = entry.url
        material.status = "fetched"
    session.flush()
    return material


# --------------------------------------------------------------------------
# Materials -- File topics (stub row created eagerly at /toc time, so /file
# always has an existing row to update in place -- see upsert_file_material
# below for the fetch-time half)
# --------------------------------------------------------------------------


def upsert_file_stub_material(
    session: Session, course_id: int, entry: TocEntry, module_id: int | None = None
) -> Material:
    """Ensure a materials row exists for a File-type ToC entry.

    Only refreshes metadata that's safe to touch without clobbering fetch
    state (module, title, source_url) -- sha256/mime/size_bytes/status/
    summary/error are left exactly as they are for a row that's already
    been fetched via /file. For a genuinely new row, sha256 stays NULL
    until /file uploads it; `compute_needed`'s "stored d2l_updated_at is
    null" rule already treats that as needed, so this doesn't change
    diff behavior.
    """
    material = session.execute(
        select(Material).where(Material.course_id == course_id, Material.d2l_topic_id == entry.topic_id)
    ).scalar_one_or_none()
    if material is None:
        material = Material(
            course_id=course_id,
            d2l_topic_id=entry.topic_id,
            module_id=module_id,
            kind=infer_kind(entry.title, entry.url),
            title=entry.title,
            source_url=entry.url,
            status="fetched",
        )
        session.add(material)
    else:
        material.module_id = module_id
        material.title = entry.title
        material.source_url = entry.url
    session.flush()
    return material


# --------------------------------------------------------------------------
# Materials -- extras (news/dropbox, keyed by a synthetic source_url)
# --------------------------------------------------------------------------


def upsert_text_material(
    session: Session,
    blob_store: BlobStore,
    course_id: int,
    *,
    source_url: str,
    title: str,
    kind: str,
    body: str,
    mime: str,
) -> Material:
    sha256, size = blob_store.put_bytes(body.encode("utf-8"))
    blob_store.write_text(sha256, body)

    material = session.execute(
        select(Material).where(Material.course_id == course_id, Material.source_url == source_url)
    ).scalar_one_or_none()
    sha_unchanged = material is not None and material.sha256 == sha256

    if material is None:
        material = Material(course_id=course_id, d2l_topic_id=None, source_url=source_url, title=title, kind=kind)
        session.add(material)

    material.title = title
    material.kind = kind
    material.sha256 = sha256
    material.mime = mime
    material.size_bytes = size
    material.fetched_at = now_iso()

    if not sha_unchanged:
        # Bytes actually changed (or this is a brand new material): reset
        # pipeline progress, matching upsert_file_material's rule. Without
        # this, every /toc call that carries extras (news/dropbox) -- which
        # is every sync, since the extension always resends whatever's
        # currently posted -- would unconditionally knock an
        # already-summarized announcement/assignment back to 'extracted'
        # even though its content hasn't changed at all, silently dropping
        # it out of the taxonomy prompt's material list and out of
        # classify's worklist (both keyed on status=='summarized').
        material.status = "extracted"
        material.summary = None
        material.error = None

    session.flush()
    return material


# --------------------------------------------------------------------------
# Materials -- File topics, fetch-time half: /file upserts the byte-level
# fields onto the row `upsert_file_stub_material` already created at /toc
# time. `material is None` should no longer fire in the normal flow (a stub
# always exists by the time /file runs), but the create branch is kept for
# robustness -- e.g. a /file call against a course that predates this stub
# behavior, or any other path that reaches /file without a prior /toc.
# --------------------------------------------------------------------------


def upsert_file_material(
    session: Session,
    course_id: int,
    d2l_topic_id: int,
    *,
    sha256: str,
    mime: str | None,
    size_bytes: int,
    source_url: str,
    title: str,
    d2l_updated_at: str | None,
) -> Material:
    material = session.execute(
        select(Material).where(Material.course_id == course_id, Material.d2l_topic_id == d2l_topic_id)
    ).scalar_one_or_none()

    sha_unchanged = material is not None and material.sha256 == sha256

    if material is None:
        material = Material(
            course_id=course_id,
            d2l_topic_id=d2l_topic_id,
            kind=infer_kind(title, source_url),
            title=title,
            status="fetched",
        )
        session.add(material)

    material.sha256 = sha256
    material.mime = mime
    material.size_bytes = size_bytes
    material.source_url = source_url
    material.title = title
    material.d2l_updated_at = d2l_updated_at
    material.fetched_at = now_iso()

    if not sha_unchanged:
        # Bytes actually changed (or this is a brand new material): reset
        # pipeline progress. Unchanged bytes leave status/summary/error as
        # they are, so re-syncing never throws away downstream work.
        material.status = "fetched"
        material.summary = None
        material.error = None

    session.flush()
    return material


# --------------------------------------------------------------------------
# Sync runs
# --------------------------------------------------------------------------


def create_sync_run(session: Session, course_id: int, not_needed: int, *, source: str = "extension") -> SyncRun:
    sync_run = SyncRun(
        course_id=course_id,
        source=source,
        started_at=now_iso(),
        status="running",
        stats_json=json.dumps({"files": 0, "bytes": 0, "notNeeded": not_needed}),
    )
    session.add(sync_run)
    session.flush()
    return sync_run


def record_file_upload_stats(session: Session, sync_run: SyncRun, size_bytes: int) -> None:
    stats = json.loads(sync_run.stats_json or "{}")
    stats["files"] = stats.get("files", 0) + 1
    stats["bytes"] = stats.get("bytes", 0) + size_bytes
    sync_run.stats_json = json.dumps(stats)
    session.flush()


def finalize_sync_run(session: Session, sync_run: SyncRun, errors: list[dict]) -> dict:
    """Finalize `sync_run` with `errors` unless it's already finished, in
    which case this is a no-op and the existing finalized state is
    returned (idempotent-safe against a duplicate /complete call)."""
    if sync_run.finished_at is None:
        stats = json.loads(sync_run.stats_json or "{}")
        stats["errors"] = errors
        sync_run.status = "complete" if not errors else "failed"
        sync_run.finished_at = now_iso()
        sync_run.stats_json = json.dumps(stats)
        session.flush()
    return json.loads(sync_run.stats_json or "{}")


# --------------------------------------------------------------------------
# Modules -- zip-import path (Task 13, ingest/zip_import.py). A zip has no
# D2L module id to key on, so one is derived deterministically (and always
# negative, since real D2L module ids are always positive) from the
# folder's own path within the zip -- re-uploading the same zip resolves to
# the exact same module rows instead of creating duplicates.
# --------------------------------------------------------------------------


def zip_module_d2l_id(path: str) -> int:
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()
    return -(int(digest[:8], 16) + 1)


def upsert_zip_module(
    session: Session, course_id: int, *, path: str, parent_id: int | None, title: str, sort_order: int
) -> Module:
    d2l_module_id = zip_module_d2l_id(path)
    module = session.execute(
        select(Module).where(Module.course_id == course_id, Module.d2l_module_id == d2l_module_id)
    ).scalar_one_or_none()
    if module is None:
        module = Module(
            course_id=course_id,
            d2l_module_id=d2l_module_id,
            parent_id=parent_id,
            title=title,
            sort_order=sort_order,
        )
        session.add(module)
    else:
        module.parent_id = parent_id
        module.title = title
        module.sort_order = sort_order
    session.flush()
    return module


# --------------------------------------------------------------------------
# Materials -- zip-import path: identity is `source_url` ("zip:{path within
# the archive}"), since there's no D2L topic id to key on -- mirrors
# upsert_text_material's use of a synthetic source_url as identity. Content
# dedupe is still sha256/blob-store based; re-uploading the same zip
# resolves every entry back to the same row instead of duplicating it.
# --------------------------------------------------------------------------


def upsert_zip_material(
    session: Session,
    course_id: int,
    *,
    module_id: int | None,
    source_url: str,
    title: str,
    kind: str,
    sha256: str,
    mime: str | None,
    size_bytes: int,
) -> Material:
    material = session.execute(
        select(Material).where(Material.course_id == course_id, Material.source_url == source_url)
    ).scalar_one_or_none()
    sha_unchanged = material is not None and material.sha256 == sha256

    if material is None:
        material = Material(course_id=course_id, source_url=source_url, title=title, status="fetched")
        session.add(material)

    material.module_id = module_id
    material.kind = kind
    material.title = title
    material.sha256 = sha256
    material.mime = mime
    material.size_bytes = size_bytes
    material.fetched_at = now_iso()

    if not sha_unchanged:
        # Bytes actually changed (or this is a brand new material): reset
        # pipeline progress, matching upsert_file_material's rule.
        material.status = "fetched"
        material.summary = None
        material.error = None

    session.flush()
    return material
