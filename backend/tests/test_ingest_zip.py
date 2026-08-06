"""Tests for the manual zip-import fallback (POST /api/ingest/zip),
deferred from Task 3 and built in Task 13: multipart zip upload -> modules
(directories) + materials (files), pairing-token auth, dedupe on re-upload,
and skip behavior for unsafe/oversized entries.
"""

from __future__ import annotations

import hashlib
import io
import tomllib
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from brightspace_agent.db.models import Course, Material, MaterialTopic, Module, SyncRun, Topic

ORG_UNIT_ID = 555


# --------------------------------------------------------------------------
# Fixtures (mirrors test_ingest_api.py's setup)
# --------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BSA_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def app(data_dir):
    from brightspace_agent.main import create_app

    return create_app()


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def pairing_token(data_dir):
    config = tomllib.loads((data_dir / "config.toml").read_text())
    return config["pairing_token"]


@pytest.fixture
def auth_headers(pairing_token):
    return {"Authorization": f"Bearer {pairing_token}"}


@pytest.fixture
def db_session_factory(data_dir):
    from brightspace_agent.db.session import init_db

    _, session_factory = init_db(data_dir / "brightspace.db")
    return session_factory


def handshake(client, auth_headers, org_unit_id=ORG_UNIT_ID, name="Intro to CS", code="CS101"):
    resp = client.post(
        "/api/ingest/handshake",
        headers=auth_headers,
        json={
            "tenantOrigin": "https://school.d2l.com",
            "apiVersions": {"le": "1.79", "lp": "1.35"},
            "whoami": {"Identifier": "999", "UniqueName": "gavin"},
            "enrollments": [{"orgUnitId": org_unit_id, "name": name, "code": code}],
        },
    )
    assert resp.status_code == 200
    return resp.json()["knownCourses"][0]["courseId"]


def _make_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, data in entries.items():
            zf.writestr(path, data)
    return buf.getvalue()


def post_zip(client, auth_headers, zip_bytes, org_unit_id=ORG_UNIT_ID, filename="course.zip"):
    return client.post(
        "/api/ingest/zip",
        headers=auth_headers,
        data={"orgUnitId": str(org_unit_id)},
        files={"file": (filename, zip_bytes, "application/zip")},
    )


# --------------------------------------------------------------------------
# 1. Auth
# --------------------------------------------------------------------------


def test_zip_import_requires_pairing_token(client):
    zip_bytes = _make_zip({"a.txt": b"hi"})

    no_auth = client.post(
        "/api/ingest/zip", data={"orgUnitId": str(ORG_UNIT_ID)}, files={"file": ("x.zip", zip_bytes)}
    )
    assert no_auth.status_code == 401
    assert no_auth.json()["detail"] == "invalid pairing token"

    wrong_auth = client.post(
        "/api/ingest/zip",
        headers={"Authorization": "Bearer wrong-token"},
        data={"orgUnitId": str(ORG_UNIT_ID)},
        files={"file": ("x.zip", zip_bytes)},
    )
    assert wrong_auth.status_code == 401
    assert wrong_auth.json()["detail"] == "invalid pairing token"


# --------------------------------------------------------------------------
# 2. Unknown course
# --------------------------------------------------------------------------


def test_zip_import_unknown_org_unit_404s(client, auth_headers):
    zip_bytes = _make_zip({"a.txt": b"hi"})
    resp = post_zip(client, auth_headers, zip_bytes, org_unit_id=99999)
    assert resp.status_code == 404


def test_zip_import_bad_zip_400s(client, auth_headers):
    handshake(client, auth_headers)
    resp = post_zip(client, auth_headers, b"not actually a zip file")
    assert resp.status_code == 400


# --------------------------------------------------------------------------
# 3. Happy path: 2 top-level dirs, a nested dir, pdf+txt+junk
# --------------------------------------------------------------------------


def test_zip_import_walks_modules_and_materials_correctly(client, auth_headers, db_session_factory, data_dir):
    course_id = handshake(client, auth_headers)

    pdf_bytes = b"%PDF-1.4 fake syllabus content"
    txt_bytes = b"lecture notes as plain text"
    junk_bytes = b"some unrecognized binary blob"
    entries = {
        "week1/syllabus.pdf": pdf_bytes,
        "week1/notes.txt": txt_bytes,
        "week2/labs/lab1.xyz": junk_bytes,
    }
    resp = post_zip(client, auth_headers, _make_zip(entries))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "complete"
    assert body["stats"]["modules"] == 3  # week1, week2, week2/labs
    assert body["stats"]["files"] == 3
    assert body["stats"]["bytes"] == len(pdf_bytes) + len(txt_bytes) + len(junk_bytes)
    assert body["stats"]["errors"] == []

    with db_session_factory() as session:
        sync_run = session.execute(select(SyncRun).where(SyncRun.course_id == course_id)).scalar_one()
        assert sync_run.source == "zip"
        assert sync_run.status == "complete"

        modules = {m.title: m for m in session.execute(select(Module).where(Module.course_id == course_id)).scalars()}
        assert modules.keys() == {"week1", "week2", "labs"}
        assert modules["week1"].parent_id is None
        assert modules["week2"].parent_id is None
        assert modules["week1"].sort_order == 0
        assert modules["week2"].sort_order == 1
        assert modules["labs"].parent_id == modules["week2"].id
        assert modules["labs"].sort_order == 0

        materials = {
            m.title: m for m in session.execute(select(Material).where(Material.course_id == course_id)).scalars()
        }
        assert materials.keys() == {"syllabus.pdf", "notes.txt", "lab1.xyz"}

        syllabus = materials["syllabus.pdf"]
        assert syllabus.kind == "syllabus"
        assert syllabus.module_id == modules["week1"].id
        assert syllabus.source_url == "zip:week1/syllabus.pdf"
        assert syllabus.status == "fetched"
        assert syllabus.sha256 == hashlib.sha256(pdf_bytes).hexdigest()

        notes = materials["notes.txt"]
        assert notes.kind == "document"
        assert notes.module_id == modules["week1"].id

        junk = materials["lab1.xyz"]
        assert junk.kind == "other"
        assert junk.module_id == modules["labs"].id

        # Blobs actually stored, content-addressed.
        blob_path = data_dir / "blobs" / syllabus.sha256[:2] / syllabus.sha256
        assert blob_path.read_bytes() == pdf_bytes


# --------------------------------------------------------------------------
# 4. Re-upload the same zip -> dedupe, no duplicate rows
# --------------------------------------------------------------------------


def test_zip_import_reupload_same_zip_dedupes(client, auth_headers, db_session_factory):
    handshake(client, auth_headers)
    entries = {"week1/a.pdf": b"aaa", "week1/b.txt": b"bbb", "week2/nested/c.pdf": b"ccc"}
    zip_bytes = _make_zip(entries)

    first = post_zip(client, auth_headers, zip_bytes)
    assert first.status_code == 200

    with db_session_factory() as session:
        module_count_1 = len(session.execute(select(Module)).scalars().all())
        material_count_1 = len(session.execute(select(Material)).scalars().all())
        material_ids_1 = {m.id for m in session.execute(select(Material)).scalars()}

    second = post_zip(client, auth_headers, zip_bytes)
    assert second.status_code == 200
    assert second.json()["stats"]["files"] == 3  # every entry still "processed"...

    with db_session_factory() as session:
        module_count_2 = len(session.execute(select(Module)).scalars().all())
        material_count_2 = len(session.execute(select(Material)).scalars().all())
        material_ids_2 = {m.id for m in session.execute(select(Material)).scalars()}

    # ...but resolves back to the exact same rows, not duplicates.
    assert module_count_2 == module_count_1 == 3
    assert material_count_2 == material_count_1 == 3
    assert material_ids_2 == material_ids_1


def test_zip_import_changed_entry_bytes_clears_stale_classifications(
    client, auth_headers, db_session_factory
):
    """The upsert_zip_material half of the stale-classification fix: a
    re-uploaded zip whose entry actually changed must drop that material's
    topic assignments at the CURRENT taxonomy version (S3's worklist skips
    anything that already has rows there), while leaving older versions --
    the taxonomy editor's history -- alone. Unchanged entries keep
    everything, exactly as they keep their summary.
    """
    course_id = handshake(client, auth_headers)
    post_zip(client, auth_headers, _make_zip({"week1/a.txt": b"original"}))

    with db_session_factory() as session:
        material = session.execute(select(Material)).scalars().one()
        material_id = material.id
        course = session.get(Course, course_id)
        course.taxonomy_version = 1
        topic = Topic(
            course_id=course_id, taxonomy_version=1, slug="intro", name="Intro",
            description="d", order_index=0, created_by="agent",
        )
        session.add(topic)
        session.flush()
        for version in (0, 1):
            session.add(
                MaterialTopic(
                    material_id=material_id, topic_id=topic.id, taxonomy_version=version,
                    confidence=0.9, rationale="r", method="llm", review_status="auto",
                )
            )
        session.commit()

    def versions() -> list[int]:
        with db_session_factory() as session:
            return sorted(
                session.execute(
                    select(MaterialTopic.taxonomy_version).where(MaterialTopic.material_id == material_id)
                ).scalars().all()
            )

    post_zip(client, auth_headers, _make_zip({"week1/a.txt": b"original"}))
    assert versions() == [0, 1]

    post_zip(client, auth_headers, _make_zip({"week1/a.txt": b"rewritten content"}))
    assert versions() == [0]


# --------------------------------------------------------------------------
# 5. Skips: unsafe paths and oversized entries, recorded as errors
# --------------------------------------------------------------------------


def test_zip_import_skips_unsafe_paths_with_errors_in_stats(client, auth_headers, db_session_factory):
    course_id = handshake(client, auth_headers)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("good.txt", b"totally fine")
        zf.writestr("../escape.txt", b"zip-slip attempt")
        zf.writestr("/etc/passwd", b"absolute path attempt")

    resp = post_zip(client, auth_headers, buf.getvalue())
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"  # any per-entry error marks the run failed
    assert body["stats"]["files"] == 1
    paths_with_errors = {e["path"] for e in body["stats"]["errors"]}
    assert paths_with_errors == {"../escape.txt", "/etc/passwd"}

    with db_session_factory() as session:
        materials = session.execute(select(Material).where(Material.course_id == course_id)).scalars().all()
        assert len(materials) == 1
        assert materials[0].title == "good.txt"


def test_zip_import_skips_oversized_entries(client, auth_headers, monkeypatch):
    handshake(client, auth_headers)
    from brightspace_agent.ingest import zip_import

    monkeypatch.setattr(zip_import, "MAX_ENTRY_SIZE", 10)

    resp = post_zip(
        client, auth_headers, _make_zip({"small.txt": b"tiny", "big.txt": b"this is way more than 10 bytes"})
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["stats"]["files"] == 1
    assert body["status"] == "failed"
    assert body["stats"]["errors"][0]["path"] == "big.txt"
    assert "too large" in body["stats"]["errors"][0]["message"]


def test_zip_import_ignores_macos_noise_entries(client, auth_headers, db_session_factory):
    course_id = handshake(client, auth_headers)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("real.txt", b"actual content")
        zf.writestr("__MACOSX/._real.txt", b"apple resource fork junk")
        zf.writestr(".DS_Store", b"finder metadata junk")

    resp = post_zip(client, auth_headers, buf.getvalue())
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "complete"
    assert body["stats"]["files"] == 1
    assert body["stats"]["errors"] == []

    with db_session_factory() as session:
        materials = session.execute(select(Material).where(Material.course_id == course_id)).scalars().all()
        assert len(materials) == 1
        assert materials[0].title == "real.txt"
