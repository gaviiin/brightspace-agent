"""Tests for the extension-facing ingest API: handshake, toc/diff, streamed
file upload, and completion, all gated behind the pairing-token dependency.
"""

import copy
import hashlib
import json
import tomllib
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from brightspace_agent.db.models import Course, LtiResolution, Material, MaterialTopic, Module, SyncRun, Topic
from brightspace_agent.ingest.diff import infer_kind

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "d2l"

ORG_UNIT_ID = 555
ALL_FILE_TOPIC_IDS = {1001, 1002, 1004, 1005, 1006}
LINK_TOPIC_ID = 1003


def load_toc() -> dict:
    return json.loads((FIXTURES_DIR / "toc_sample.json").read_text())


def find_topic(toc: dict, topic_id: int) -> dict:
    """Recursively find a topic dict by TopicId within a ToC fixture."""
    for module in toc.get("Modules", []):
        for topic in module.get("Topics", []):
            if topic.get("TopicId") == topic_id:
                return topic
        found = find_topic(module, topic_id)
        if found is not None:
            return found
    return None


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BSA_DATA_DIR", str(tmp_path))
    # M2.7: /api/ingest/lti-resolution reuses api/media.py's expand-and-
    # upsert path (MediaFetcher.expand), same as the manual-add endpoint --
    # forces the offline mock fetcher, no real subprocess/network here.
    monkeypatch.setenv("BSA_MOCK_LLM", "1")
    return tmp_path


@pytest.fixture
def app(data_dir):
    from brightspace_agent.main import create_app

    return create_app()


@pytest.fixture
def client(app):
    # Loopback Host, not TestClient's default "testserver" -- see
    # test_health.py's LOOPBACK_BASE_URL for why.
    return TestClient(app, base_url="http://127.0.0.1:8730")


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


def post_toc(client, auth_headers, toc, org_unit_id=ORG_UNIT_ID, extras=None):
    return client.post(
        "/api/ingest/toc",
        headers=auth_headers,
        json={"orgUnitId": org_unit_id, "toc": toc, "extras": extras},
    )


def upload_file(client, auth_headers, sync_run_id, d2l_topic_id, data, *, source_url="https://x/f.pdf",
                 title="A File", content_type="application/pdf", d2l_updated=None):
    headers = {**auth_headers, "X-Source-Url": source_url, "X-Title": title, "Content-Type": content_type}
    if d2l_updated is not None:
        headers["X-D2L-Updated"] = d2l_updated
    return client.post(
        f"/api/ingest/file?syncRunId={sync_run_id}&d2lTopicId={d2l_topic_id}",
        headers=headers,
        content=data,
    )


# --------------------------------------------------------------------------
# 1. Auth
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,body_kwargs",
    [
        ("/api/ingest/handshake", {"json": {"tenantOrigin": "x", "apiVersions": {}, "whoami": {}, "enrollments": []}}),
        ("/api/ingest/toc", {"json": {"orgUnitId": 1, "toc": {}, "extras": None}}),
        ("/api/ingest/file?syncRunId=1&d2lTopicId=1", {"content": b"data"}),
        ("/api/ingest/complete", {"json": {"syncRunId": 1, "errors": []}}),
    ],
)
def test_ingest_routes_require_pairing_token(client, path, body_kwargs):
    no_auth = client.post(path, **body_kwargs)
    assert no_auth.status_code == 401
    assert no_auth.json()["detail"] == "invalid pairing token"

    wrong_auth = client.post(path, headers={"Authorization": "Bearer wrong-token"}, **body_kwargs)
    assert wrong_auth.status_code == 401
    assert wrong_auth.json()["detail"] == "invalid pairing token"


# --------------------------------------------------------------------------
# 2. Handshake
# --------------------------------------------------------------------------


def test_handshake_upserts_courses_and_updates_names_on_repeat(client, auth_headers, db_session_factory):
    resp = client.post(
        "/api/ingest/handshake",
        headers=auth_headers,
        json={
            "tenantOrigin": "https://school.d2l.com",
            "apiVersions": {"le": "1.79"},
            "whoami": {"Identifier": "1"},
            "enrollments": [
                {"orgUnitId": 100, "name": "Course A", "code": "CS100"},
                {"orgUnitId": 200, "name": "Course B", "code": None},
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["knownCourses"]) == 2
    ids_first = {c["orgUnitId"]: c["courseId"] for c in body["knownCourses"]}

    with db_session_factory() as session:
        assert session.execute(select(Course)).scalars().all().__len__() == 2

    resp2 = client.post(
        "/api/ingest/handshake",
        headers=auth_headers,
        json={
            "tenantOrigin": "https://school.d2l.com",
            "apiVersions": {},
            "whoami": {},
            "enrollments": [
                {"orgUnitId": 100, "name": "Course A Renamed", "code": "CS100"},
                {"orgUnitId": 200, "name": "Course B", "code": "BIO200"},
            ],
        },
    )
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert len(body2["knownCourses"]) == 2
    ids_second = {c["orgUnitId"]: c["courseId"] for c in body2["knownCourses"]}
    assert ids_second == ids_first  # upsert, not duplicate rows

    names = {c["orgUnitId"]: c["name"] for c in body2["knownCourses"]}
    assert names[100] == "Course A Renamed"

    with db_session_factory() as session:
        assert session.execute(select(Course)).scalars().all().__len__() == 2


# --------------------------------------------------------------------------
# 3. toc on unknown course
# --------------------------------------------------------------------------


def test_toc_unknown_org_unit_returns_404(client, auth_headers):
    resp = post_toc(client, auth_headers, {"Modules": []}, org_unit_id=99999)
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# 4. toc happy path
# --------------------------------------------------------------------------


def test_toc_happy_path_modules_materials_and_needed(client, auth_headers, db_session_factory):
    course_id = handshake(client, auth_headers)

    resp = post_toc(client, auth_headers, load_toc())
    assert resp.status_code == 200
    body = resp.json()

    assert "syncRunId" in body
    assert isinstance(body["syncRunId"], int)

    needed = body["needed"]
    needed_ids = {item["d2lTopicId"] for item in needed}
    assert needed_ids == ALL_FILE_TOPIC_IDS  # fresh course: every File topic is needed

    toc = load_toc()
    for item in needed:
        assert set(item.keys()) == {"d2lTopicId", "url", "title", "sizeHint", "lastModified"}
        expected_last_modified = find_topic(toc, item["d2lTopicId"])["LastModifiedDate"]
        assert item["lastModified"] == expected_last_modified

    # 1006 has no LastModifiedDate in the fixture -- lastModified should
    # round-trip as null, not be dropped or coerced.
    item_1006 = next(item for item in needed if item["d2lTopicId"] == 1006)
    assert item_1006["lastModified"] is None

    with db_session_factory() as session:
        modules = {m.d2l_module_id: m for m in session.execute(
            select(Module).where(Module.course_id == course_id)
        ).scalars()}

        assert modules.keys() == {100, 200, 210}
        assert modules[100].parent_id is None
        assert modules[100].sort_order == 0
        assert modules[100].title == "Week 1 — Intro"

        assert modules[200].parent_id is None
        assert modules[200].sort_order == 1

        assert modules[210].parent_id == modules[200].id
        assert modules[210].sort_order == 0
        assert modules[210].title == "Labs"

        link_material = session.execute(
            select(Material).where(Material.course_id == course_id, Material.d2l_topic_id == LINK_TOPIC_ID)
        ).scalar_one()
        assert link_material.kind == "link"
        assert link_material.status == "fetched"
        assert link_material.source_url == "https://en.wikipedia.org/wiki/Big_O_notation"
        assert link_material.module_id == modules[100].id  # Link topic 1003 lives in Week 1

        # File topics get a stub material row at /toc time too (module_id
        # set, sha256 still NULL until /file uploads it), so /file can
        # always update in place instead of inserting a second row.
        file_materials = {
            m.d2l_topic_id: m
            for m in session.execute(
                select(Material).where(
                    Material.course_id == course_id, Material.d2l_topic_id.in_(ALL_FILE_TOPIC_IDS)
                )
            ).scalars()
        }
        assert file_materials.keys() == ALL_FILE_TOPIC_IDS
        for material in file_materials.values():
            assert material.sha256 is None
            assert material.status == "fetched"

        assert file_materials[1001].module_id == modules[100].id  # syllabus.pdf, Week 1
        assert file_materials[1001].kind == "syllabus"
        assert file_materials[1004].module_id == modules[200].id  # lecture2.pptx, Week 2
        assert file_materials[1004].kind == "slides"
        assert file_materials[1005].module_id == modules[210].id  # lab1.pdf, Labs
        assert file_materials[1005].kind == "document"


# --------------------------------------------------------------------------
# 5. diff behavior
# --------------------------------------------------------------------------


def test_diff_excludes_unchanged_and_includes_bumped_material(client, auth_headers, db_session_factory):
    course_id = handshake(client, auth_headers)
    toc = load_toc()
    lecture1_last_modified = find_topic(toc, 1002)["LastModifiedDate"]

    with db_session_factory() as session:
        session.add(Material(
            course_id=course_id,
            d2l_topic_id=1002,
            kind="document",
            title="Lecture 1 — Overview",
            sha256="a" * 64,  # actually uploaded, not just a /toc-time stub
            d2l_updated_at=lecture1_last_modified,
        ))
        session.commit()

    resp = post_toc(client, auth_headers, toc)
    needed_ids = {item["d2lTopicId"] for item in resp.json()["needed"]}
    assert 1002 not in needed_ids
    assert needed_ids == ALL_FILE_TOPIC_IDS - {1002}

    with db_session_factory() as session:
        sync_run = session.get(SyncRun, resp.json()["syncRunId"])
        assert json.loads(sync_run.stats_json)["notNeeded"] == 1  # lecture1 skipped

    bumped_toc = copy.deepcopy(toc)
    find_topic(bumped_toc, 1002)["LastModifiedDate"] = "2026-02-01T00:00:00.000Z"

    resp2 = post_toc(client, auth_headers, bumped_toc)
    needed_ids2 = {item["d2lTopicId"] for item in resp2.json()["needed"]}
    assert 1002 in needed_ids2


def test_diff_needed_when_sha256_null_even_with_fresh_d2l_updated_at(client, auth_headers, db_session_factory):
    """A material row can have a non-null d2l_updated_at while sha256 is
    still NULL (a stub row from /toc, or a row whose /file upload failed
    partway through some future flow). That row must stay 'needed'
    regardless of what d2l_updated_at says -- otherwise a failed upload
    becomes permanently invisible to future diffs."""
    course_id = handshake(client, auth_headers)
    toc = load_toc()
    lecture1_last_modified = find_topic(toc, 1002)["LastModifiedDate"]

    with db_session_factory() as session:
        session.add(Material(
            course_id=course_id,
            d2l_topic_id=1002,
            kind="document",
            title="Lecture 1 — Overview",
            sha256=None,  # never actually uploaded
            d2l_updated_at=lecture1_last_modified,  # but d2l_updated_at is set (non-null)
        ))
        session.commit()

    resp = post_toc(client, auth_headers, toc)
    needed_ids = {item["d2lTopicId"] for item in resp.json()["needed"]}
    assert 1002 in needed_ids  # sha256 IS NULL overrides the fresh-looking d2l_updated_at


# --------------------------------------------------------------------------
# 6. file upload
# --------------------------------------------------------------------------


def test_file_upload_stores_blob_and_material(client, auth_headers, db_session_factory, data_dir):
    handshake(client, auth_headers)
    sync_run_id = post_toc(client, auth_headers, load_toc()).json()["syncRunId"]

    data = b"%PDF-1.4 fake syllabus content"
    resp = upload_file(
        client, auth_headers, sync_run_id, 1001, data,
        source_url="https://school.d2l.com/.../syllabus.pdf",
        title="Course Syllabus",
        content_type="application/pdf",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["deduped"] is False
    expected_sha = hashlib.sha256(data).hexdigest()
    assert body["sha256"] == expected_sha

    blob_path = data_dir / "blobs" / expected_sha[:2] / expected_sha
    assert blob_path.exists()
    assert blob_path.read_bytes() == data

    with db_session_factory() as session:
        material = session.get(Material, body["materialId"])
        assert material.sha256 == expected_sha
        assert material.mime == "application/pdf"
        assert material.size_bytes == len(data)
        assert material.title == "Course Syllabus"
        assert material.source_url == "https://school.d2l.com/.../syllabus.pdf"
        assert material.d2l_topic_id == 1001

    # Re-upload identical bytes -> deduped.
    resp2 = upload_file(
        client, auth_headers, sync_run_id, 1001, data,
        source_url="https://school.d2l.com/.../syllabus.pdf", title="Course Syllabus",
    )
    assert resp2.status_code == 200
    assert resp2.json()["deduped"] is True
    assert resp2.json()["materialId"] == body["materialId"]


def test_file_upload_fills_in_toc_time_stub_without_duplicate_row(client, auth_headers, db_session_factory):
    course_id = handshake(client, auth_headers)
    sync_run_id = post_toc(client, auth_headers, load_toc()).json()["syncRunId"]

    with db_session_factory() as session:
        stub = session.execute(
            select(Material).where(Material.course_id == course_id, Material.d2l_topic_id == 1001)
        ).scalar_one()
        stub_id = stub.id
        assert stub.sha256 is None
        assert stub.module_id is not None

    data = b"the actual syllabus bytes"
    resp = upload_file(client, auth_headers, sync_run_id, 1001, data, title="Course Syllabus")
    assert resp.status_code == 200
    assert resp.json()["deduped"] is False
    assert resp.json()["materialId"] == stub_id  # updated in place, not a new row

    with db_session_factory() as session:
        rows = session.execute(
            select(Material).where(Material.course_id == course_id, Material.d2l_topic_id == 1001)
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].id == stub_id
        assert rows[0].sha256 == hashlib.sha256(data).hexdigest()
        assert rows[0].module_id is not None


def test_file_upload_decodes_percent_encoded_title(client, auth_headers, db_session_factory):
    handshake(client, auth_headers)
    sync_run_id = post_toc(client, auth_headers, load_toc()).json()["syncRunId"]

    title = "Café Notes.pdf"
    resp = upload_file(
        client, auth_headers, sync_run_id, 1001, b"bytes",
        title=quote(title),
    )
    assert resp.status_code == 200

    with db_session_factory() as session:
        material = session.get(Material, resp.json()["materialId"])
        assert material.title == title


def test_file_upload_unknown_sync_run_404(client, auth_headers):
    resp = upload_file(client, auth_headers, 999999, 1001, b"data")
    assert resp.status_code == 404


def test_file_upload_completed_sync_run_409(client, auth_headers):
    handshake(client, auth_headers)
    sync_run_id = post_toc(client, auth_headers, load_toc()).json()["syncRunId"]
    client.post("/api/ingest/complete", headers=auth_headers, json={"syncRunId": sync_run_id, "errors": []})

    resp = upload_file(client, auth_headers, sync_run_id, 1001, b"data")
    assert resp.status_code == 409


# --------------------------------------------------------------------------
# 7. status rule
# --------------------------------------------------------------------------


def test_file_upload_status_rule_preserves_progress_unless_bytes_change(client, auth_headers, db_session_factory):
    handshake(client, auth_headers)
    sync_run_id = post_toc(client, auth_headers, load_toc()).json()["syncRunId"]

    data = b"original bytes"
    resp = upload_file(client, auth_headers, sync_run_id, 1001, data)
    material_id = resp.json()["materialId"]

    with db_session_factory() as session:
        material = session.get(Material, material_id)
        material.status = "summarized"
        material.summary = "a summary"
        session.commit()

    # Same bytes re-uploaded: status/summary untouched.
    upload_file(client, auth_headers, sync_run_id, 1001, data)
    with db_session_factory() as session:
        material = session.get(Material, material_id)
        assert material.status == "summarized"
        assert material.summary == "a summary"

    # Different bytes: status resets, summary cleared.
    upload_file(client, auth_headers, sync_run_id, 1001, b"different bytes")
    with db_session_factory() as session:
        material = session.get(Material, material_id)
        assert material.status == "fetched"
        assert material.summary is None


def _seed_taxonomy_and_assignment(session, course_id, material_id, *, version: int) -> None:
    """Put the course on taxonomy `version` with one topic, and file
    `material_id` under it at both `version` and an older one -- so a test
    can tell "cleared the current version" apart from "wiped the history"."""
    course = session.get(Course, course_id)
    course.taxonomy_version = version
    topic = Topic(
        course_id=course_id, taxonomy_version=version, slug="intro", name="Intro",
        description="d", order_index=0, created_by="agent",
    )
    session.add(topic)
    session.flush()
    for row_version in (version - 1, version):
        session.add(
            MaterialTopic(
                material_id=material_id, topic_id=topic.id, taxonomy_version=row_version,
                confidence=0.9, rationale="r", method="llm", review_status="auto",
            )
        )
    session.commit()


def _assignment_versions(db_session_factory, material_id) -> list[int]:
    with db_session_factory() as session:
        return sorted(
            session.execute(
                select(MaterialTopic.taxonomy_version).where(MaterialTopic.material_id == material_id)
            ).scalars().all()
        )


def test_file_upload_changed_bytes_clears_stale_classifications(client, auth_headers, db_session_factory):
    """Changed content must not keep the topics its old content earned.

    S3's worklist is "summarized materials with no material_topics row at
    the current taxonomy version", so a row left behind by a re-upload means
    the material is never re-classified -- it keeps stale topics forever.
    Only the CURRENT version's rows go: older versions are history the
    taxonomy editor still reads.
    """
    handshake(client, auth_headers)
    sync_run_id = post_toc(client, auth_headers, load_toc()).json()["syncRunId"]
    data = b"original bytes"
    material_id = upload_file(client, auth_headers, sync_run_id, 1001, data).json()["materialId"]

    with db_session_factory() as session:
        course_id = session.get(Material, material_id).course_id
        _seed_taxonomy_and_assignment(session, course_id, material_id, version=1)

    # Same bytes: nothing is thrown away.
    upload_file(client, auth_headers, sync_run_id, 1001, data)
    assert _assignment_versions(db_session_factory, material_id) == [0, 1]

    # Different bytes: the current version's assignment goes with the
    # summary; version 0 (history) survives.
    upload_file(client, auth_headers, sync_run_id, 1001, b"different bytes")
    assert _assignment_versions(db_session_factory, material_id) == [0]


# --------------------------------------------------------------------------
# 8. complete
# --------------------------------------------------------------------------


def test_complete_finalizes_with_stats_and_is_idempotent(client, auth_headers):
    handshake(client, auth_headers)
    sync_run_id = post_toc(client, auth_headers, load_toc()).json()["syncRunId"]

    data1 = b"file one bytes"
    data2 = b"file two bytes longer"
    upload_file(client, auth_headers, sync_run_id, 1001, data1)
    upload_file(client, auth_headers, sync_run_id, 1002, data2)

    resp = client.post("/api/ingest/complete", headers=auth_headers, json={"syncRunId": sync_run_id, "errors": []})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "complete"
    assert body["stats"]["files"] == 2
    assert body["stats"]["bytes"] == len(data1) + len(data2)
    assert body["stats"]["errors"] == []
    assert body["stats"]["notNeeded"] == 0  # fresh course: nothing was already up to date

    resp2 = client.post(
        "/api/ingest/complete",
        headers=auth_headers,
        json={"syncRunId": sync_run_id, "errors": [{"d2lTopicId": 1, "message": "ignored, run already finished"}]},
    )
    assert resp2.status_code == 200
    assert resp2.json() == body  # second call is a no-op, returns existing finalized state


def test_complete_with_errors_marks_failed(client, auth_headers):
    handshake(client, auth_headers)
    sync_run_id = post_toc(client, auth_headers, load_toc()).json()["syncRunId"]

    resp = client.post(
        "/api/ingest/complete",
        headers=auth_headers,
        json={"syncRunId": sync_run_id, "errors": [{"d2lTopicId": 1001, "message": "boom"}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert body["stats"]["errors"] == [{"d2lTopicId": 1001, "message": "boom"}]


def test_complete_unknown_sync_run_404(client, auth_headers):
    resp = client.post("/api/ingest/complete", headers=auth_headers, json={"syncRunId": 999999, "errors": []})
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# 9. extras
# --------------------------------------------------------------------------


def test_toc_extras_create_announcement_and_assignment_materials(client, auth_headers, db_session_factory, data_dir):
    course_id = handshake(client, auth_headers)
    resp = post_toc(
        client, auth_headers, load_toc(),
        extras={
            "news": [{"id": 1, "title": "Midterm moved", "html": "<p>Midterm moved to Friday</p>"}],
            "dropbox": [{"id": 2, "name": "Homework 1", "instructionsText": "Submit as a single PDF."}],
        },
    )
    assert resp.status_code == 200

    with db_session_factory() as session:
        announcement = session.execute(
            select(Material).where(Material.course_id == course_id, Material.source_url == "d2l:news:1")
        ).scalar_one()
        assert announcement.kind == "announcement"
        assert announcement.status == "extracted"
        assert announcement.title == "Midterm moved"
        assert announcement.d2l_topic_id is None
        announcement_sha = announcement.sha256

        assignment = session.execute(
            select(Material).where(Material.course_id == course_id, Material.source_url == "d2l:dropbox:2")
        ).scalar_one()
        assert assignment.kind == "assignment"
        assert assignment.status == "extracted"
        assert assignment.title == "Homework 1"
        assignment_sha = assignment.sha256

    from brightspace_agent.ingest.store import BlobStore

    store = BlobStore(blobs_dir=data_dir / "blobs", text_dir=data_dir / "text")
    announcement_text = store.read_text(announcement_sha)
    assert announcement_text is not None
    assert "Midterm moved to Friday" in announcement_text

    assignment_text = store.read_text(assignment_sha)
    assert assignment_text == "Submit as a single PDF."


def test_toc_extras_upsert_on_repeat_call(client, auth_headers, db_session_factory):
    course_id = handshake(client, auth_headers)
    extras = {"news": [{"id": 1, "title": "Original", "html": "<p>Original</p>"}], "dropbox": None}
    post_toc(client, auth_headers, load_toc(), extras=extras)

    extras["news"][0]["title"] = "Updated"
    extras["news"][0]["html"] = "<p>Updated</p>"
    post_toc(client, auth_headers, load_toc(), extras=extras)

    with db_session_factory() as session:
        materials = session.execute(
            select(Material).where(Material.course_id == course_id, Material.source_url == "d2l:news:1")
        ).scalars().all()
        assert len(materials) == 1
        assert materials[0].title == "Updated"


def test_toc_extras_status_rule_preserves_progress_unless_content_changes(
    client, auth_headers, db_session_factory
):
    """Mirrors test_file_upload_status_rule_preserves_progress_unless_bytes_change:
    every /toc call re-sends whatever extras are currently posted (that's
    the extension's real behavior, not a special "re-sync" case), so an
    unchanged announcement/assignment must not be knocked back to
    'extracted' -- otherwise every ordinary re-sync would silently discard
    pipeline progress on any course with news/dropbox extras."""
    course_id = handshake(client, auth_headers)
    extras = {"news": [{"id": 1, "title": "Midterm moved", "html": "<p>Midterm moved to Friday</p>"}], "dropbox": None}
    post_toc(client, auth_headers, load_toc(), extras=extras)

    with db_session_factory() as session:
        material = session.execute(
            select(Material).where(Material.course_id == course_id, Material.source_url == "d2l:news:1")
        ).scalar_one()
        material_id = material.id
        material.status = "summarized"
        material.summary = "a summary"
        session.commit()

    # Identical extras re-posted (the ordinary case, on every re-sync):
    # status/summary untouched.
    post_toc(client, auth_headers, load_toc(), extras=extras)
    with db_session_factory() as session:
        material = session.get(Material, material_id)
        assert material.status == "summarized"
        assert material.summary == "a summary"

    # Content actually changed: status resets, summary cleared.
    extras["news"][0]["html"] = "<p>Midterm moved to Monday instead</p>"
    post_toc(client, auth_headers, load_toc(), extras=extras)
    with db_session_factory() as session:
        material = session.get(Material, material_id)
        assert material.status == "extracted"
        assert material.summary is None


def test_changed_extras_body_clears_stale_classifications_at_the_current_version(
    client, auth_headers, db_session_factory
):
    """The upsert_text_material half of the stale-classification fix (see
    test_file_upload_changed_bytes_clears_stale_classifications)."""
    course_id = handshake(client, auth_headers)
    extras = {"news": [{"id": 1, "title": "Midterm moved", "html": "<p>Friday</p>"}], "dropbox": None}
    post_toc(client, auth_headers, load_toc(), extras=extras)

    with db_session_factory() as session:
        material = session.execute(
            select(Material).where(Material.course_id == course_id, Material.source_url == "d2l:news:1")
        ).scalar_one()
        material_id = material.id
        _seed_taxonomy_and_assignment(session, course_id, material_id, version=1)

    # Unchanged extras: the assignment survives, like the summary does.
    post_toc(client, auth_headers, load_toc(), extras=extras)
    assert _assignment_versions(db_session_factory, material_id) == [0, 1]

    extras["news"][0]["html"] = "<p>Monday instead</p>"
    post_toc(client, auth_headers, load_toc(), extras=extras)
    assert _assignment_versions(db_session_factory, material_id) == [0]


def test_toc_malformed_extras_items_are_skipped_not_422(client, auth_headers, db_session_factory):
    """Extras are a best-effort side channel; the ToC is the request.

    Regression for the extras wire-shape mismatch: `extras` used to be typed
    `list[NewsExtra]`, so ONE item pydantic couldn't parse -- e.g. a raw
    PascalCase Valence object, which is exactly what a real tenant returns
    -- rejected the whole /toc body. No sync run, no module tree, no file
    diff: the course's entire sync failed on an announcement. Bad items are
    now skipped per item and counted, and everything else still lands.
    """
    course_id = handshake(client, auth_headers)
    resp = post_toc(
        client, auth_headers, load_toc(),
        extras={
            "news": [
                {"Id": 9, "Title": "Raw Valence shape", "Body": {"Html": "<p>nope</p>"}},  # unmapped
                {"id": 1, "title": "Midterm moved", "html": "<p>Midterm moved to Friday</p>"},  # good
                {"id": "not-an-int", "title": "x", "html": "y"},  # wrong types
                "not even an object",
            ],
            "dropbox": [
                {"id": 2, "name": "Homework 1", "instructionsText": "Submit as a single PDF."},  # good
                {"name": "missing its id"},
            ],
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    # The ToC half of the request was processed normally.
    assert {item["d2lTopicId"] for item in body["needed"]} == ALL_FILE_TOPIC_IDS

    with db_session_factory() as session:
        source_urls = set(
            session.execute(
                select(Material.source_url).where(
                    Material.course_id == course_id, Material.source_url.like("d2l:%")
                )
            ).scalars().all()
        )
        assert source_urls == {"d2l:news:1", "d2l:dropbox:2"}

        sync_run = session.get(SyncRun, body["syncRunId"])
        assert json.loads(sync_run.stats_json)["extrasSkipped"] == 4


# --------------------------------------------------------------------------
# 10. incremental-sync contract (end-to-end)
# --------------------------------------------------------------------------


def test_incremental_sync_contract_upload_with_lastmodified_then_resync_skips_until_bumped(
    client, auth_headers, db_session_factory
):
    """The full incremental-sync loop: /toc hands back a needed item's
    lastModified, the extension is expected to echo it back as
    X-D2L-Updated on /file, and a subsequent identical /toc must then treat
    that topic as not-needed -- unless the fixture's LastModifiedDate
    actually moved forward, in which case it's needed again. This is the
    contract that was broken before lastModified was wired through
    NeededItem: without it, d2l_updated_at never gets set and every re-sync
    re-downloads everything."""
    handshake(client, auth_headers)
    toc = load_toc()

    # 1. First /toc: topic 1001 is needed, and the needed item carries
    #    lastModified straight from the fixture's LastModifiedDate.
    resp1 = post_toc(client, auth_headers, toc)
    sync_run_id = resp1.json()["syncRunId"]
    needed1 = {item["d2lTopicId"]: item for item in resp1.json()["needed"]}
    assert 1001 in needed1
    last_modified = needed1[1001]["lastModified"]
    assert last_modified == find_topic(toc, 1001)["LastModifiedDate"]
    assert last_modified is not None

    # 2. /file upload, echoing lastModified back as X-D2L-Updated exactly as
    #    the extension's sync-engine/backend-client are expected to.
    upload_resp = upload_file(
        client, auth_headers, sync_run_id, 1001, b"syllabus bytes",
        title="Course Syllabus", d2l_updated=last_modified,
    )
    assert upload_resp.status_code == 200

    with db_session_factory() as session:
        material = session.execute(
            select(Material).where(Material.course_id.in_(
                select(Course.id).where(Course.d2l_org_unit_id == ORG_UNIT_ID)
            ), Material.d2l_topic_id == 1001)
        ).scalar_one()
        assert material.d2l_updated_at == last_modified
        assert material.sha256 is not None

    # 3. Second /toc with the identical fixture: 1001 must now be excluded
    #    from needed -- this is the incremental-sync payoff.
    resp2 = post_toc(client, auth_headers, toc)
    needed_ids2 = {item["d2lTopicId"] for item in resp2.json()["needed"]}
    assert 1001 not in needed_ids2

    # 4. Bump 1001's LastModifiedDate in the fixture: it must become needed
    #    again.
    bumped_toc = copy.deepcopy(toc)
    find_topic(bumped_toc, 1001)["LastModifiedDate"] = "2026-03-01T00:00:00.000Z"
    resp3 = post_toc(client, auth_headers, bumped_toc)
    needed_ids3 = {item["d2lTopicId"] for item in resp3.json()["needed"]}
    assert 1001 in needed_ids3


# --------------------------------------------------------------------------
# 11. infer_kind unit cases
# --------------------------------------------------------------------------


def test_infer_kind_syllabus_title_overrides_extension():
    assert infer_kind("Course Syllabus", "https://x/y/syllabus.docx") == "syllabus"


def test_infer_kind_pptx_is_slides():
    assert infer_kind("Lecture 2 Slides", "https://x/y/lecture2.pptx") == "slides"


def test_infer_kind_vtt_is_transcript():
    assert infer_kind("Lecture 1 Captions", "https://x/y/lecture1.vtt") == "transcript"


def test_infer_kind_pdf_is_document():
    assert infer_kind("Reading Packet", "https://x/y/packet.pdf") == "document"


def test_infer_kind_mp4_is_video():
    assert infer_kind("Lecture Recording", "https://x/y/lecture.mp4") == "video"


def test_infer_kind_unknown_extension_is_other():
    assert infer_kind("Mystery File", "https://x/y/file.xyz") == "other"


# --------------------------------------------------------------------------
# 12. M2.7 zero-paste discovery: GET /api/ingest/lti-candidates,
# POST /api/ingest/lti-resolution.
#
# The D2L ToC only ever gives us the LTI quicklink stub -- the real
# Mediasite/Zoom URL behind it only materializes once a logged-in browser
# performs the launch. The extension does that (background tab), then
# reports the final URL here. Candidates = the same LTI-hint heuristic
# api/media.py's drawer hints use (LTI-marker source_url + a recording-
# sounding title), minus materials already resolved/unrecognized.
# --------------------------------------------------------------------------

LTI_LAUNCH_URL = "/d2l/common/dialogs/quickLink/quickLink.d2l?ou=524044&type=lti&rcode=abc123"


def _add_link_material(db_session_factory, course_id, *, title, source_url):
    with db_session_factory() as session:
        material = Material(
            course_id=course_id, kind="link", title=title, source_url=source_url, status="fetched",
        )
        session.add(material)
        session.commit()
        return material.id


def _add_lti_resolution(
    db_session_factory, course_id, material_id, *,
    status, final_url=None, platform=None, error=None, launch_url=LTI_LAUNCH_URL,
):
    with db_session_factory() as session:
        row = LtiResolution(
            course_id=course_id, material_id=material_id, launch_url=launch_url,
            final_url=final_url, platform=platform, status=status, error=error,
            created_at="2026-01-01T00:00:00+00:00", updated_at="2026-01-01T00:00:00+00:00",
        )
        session.add(row)
        session.commit()
        return row.id


# -- 12a. lti-candidates GET -------------------------------------------------


def test_lti_candidates_seeded_link_material_appears(client, auth_headers, db_session_factory):
    course_id = handshake(client, auth_headers)
    material_id = _add_link_material(
        db_session_factory, course_id, title="Mediasite Channel (Stern)", source_url=LTI_LAUNCH_URL,
    )

    resp = client.get(f"/api/ingest/lti-candidates?orgUnitId={ORG_UNIT_ID}", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["courseId"] == course_id
    assert len(body["candidates"]) == 1
    candidate = body["candidates"][0]
    assert candidate["materialId"] == material_id
    assert candidate["title"] == "Mediasite Channel (Stern)"
    assert candidate["launchUrl"] == LTI_LAUNCH_URL


def test_lti_candidates_non_lti_link_excluded(client, auth_headers, db_session_factory):
    course_id = handshake(client, auth_headers)
    _add_link_material(
        db_session_factory, course_id, title="Zoom Recordings", source_url="https://zoom.us/some/plain/link",
    )

    resp = client.get(f"/api/ingest/lti-candidates?orgUnitId={ORG_UNIT_ID}", headers=auth_headers)

    assert resp.json()["candidates"] == []


def test_lti_candidates_resolved_material_excluded(client, auth_headers, db_session_factory):
    course_id = handshake(client, auth_headers)
    material_id = _add_link_material(
        db_session_factory, course_id, title="Mediasite Channel", source_url=LTI_LAUNCH_URL,
    )
    _add_lti_resolution(
        db_session_factory, course_id, material_id, status="resolved",
        final_url="https://mediasite.example.edu/Mediasite/Play/xyz", platform="mediasite",
    )

    resp = client.get(f"/api/ingest/lti-candidates?orgUnitId={ORG_UNIT_ID}", headers=auth_headers)

    assert resp.json()["candidates"] == []


def test_lti_candidates_unrecognized_material_excluded(client, auth_headers, db_session_factory):
    course_id = handshake(client, auth_headers)
    material_id = _add_link_material(
        db_session_factory, course_id, title="Mediasite Channel", source_url=LTI_LAUNCH_URL,
    )
    _add_lti_resolution(
        db_session_factory, course_id, material_id, status="unrecognized",
        final_url="https://example.com/some/landing/page",
    )

    resp = client.get(f"/api/ingest/lti-candidates?orgUnitId={ORG_UNIT_ID}", headers=auth_headers)

    assert resp.json()["candidates"] == []


def test_lti_candidates_failed_material_still_listed(client, auth_headers, db_session_factory):
    """Transient launch failures (e.g. the tab got closed) must retry on the
    next sync -- unlike resolved/unrecognized, a failed row does NOT remove
    the material from the candidate list."""
    course_id = handshake(client, auth_headers)
    material_id = _add_link_material(
        db_session_factory, course_id, title="Mediasite Channel", source_url=LTI_LAUNCH_URL,
    )
    _add_lti_resolution(db_session_factory, course_id, material_id, status="failed", error="tab closed")

    resp = client.get(f"/api/ingest/lti-candidates?orgUnitId={ORG_UNIT_ID}", headers=auth_headers)

    candidates = resp.json()["candidates"]
    assert len(candidates) == 1
    assert candidates[0]["materialId"] == material_id


def test_lti_candidates_unknown_org_unit_404(client, auth_headers):
    resp = client.get("/api/ingest/lti-candidates?orgUnitId=99999", headers=auth_headers)
    assert resp.status_code == 404


def test_lti_candidates_requires_pairing_token(client):
    no_auth = client.get(f"/api/ingest/lti-candidates?orgUnitId={ORG_UNIT_ID}")
    assert no_auth.status_code == 401

    wrong_auth = client.get(
        f"/api/ingest/lti-candidates?orgUnitId={ORG_UNIT_ID}", headers={"Authorization": "Bearer wrong-token"}
    )
    assert wrong_auth.status_code == 401


# -- 12b. lti-resolution POST -------------------------------------------------


def _resolve(client, auth_headers, *, org_unit_id=ORG_UNIT_ID, material_id, final_url, error=None):
    return client.post(
        "/api/ingest/lti-resolution",
        headers=auth_headers,
        json={"orgUnitId": org_unit_id, "materialId": material_id, "finalUrl": final_url, "error": error},
    )


def test_lti_resolution_recognized_url_expands_and_upserts_media_sources(
    client, auth_headers, db_session_factory
):
    course_id = handshake(client, auth_headers)
    material_id = _add_link_material(
        db_session_factory, course_id, title="Mediasite Channel", source_url=LTI_LAUNCH_URL,
    )

    # A catalog/channel-shaped URL: recognized by classify_url directly (the
    # "/mediasite/catalog/" path marker) AND matched by MockMediaFetcher's
    # "mock-channel" substring, so expand() fans it out into three entries.
    resp = _resolve(
        client, auth_headers, material_id=material_id,
        final_url="https://mediasite.example.edu/Mediasite/Catalog/mock-channel",
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "resolved"
    assert body["platform"] == "mediasite"
    assert body["added"] == 3
    assert body["total"] == 3

    with db_session_factory() as session:
        from brightspace_agent.db.models import MediaSource

        rows = list(session.execute(select(MediaSource).where(MediaSource.course_id == course_id)).scalars().all())
        assert len(rows) == 3
        assert all(row.platform == "mediasite" for row in rows)

        resolution = session.execute(
            select(LtiResolution).where(LtiResolution.material_id == material_id)
        ).scalar_one()
        assert resolution.status == "resolved"
        assert resolution.platform == "mediasite"
        assert resolution.final_url == "https://mediasite.example.edu/Mediasite/Catalog/mock-channel"
        assert resolution.launch_url == LTI_LAUNCH_URL


def test_lti_resolution_unrecognized_url_stores_unrecognized_and_final_url(
    client, auth_headers, db_session_factory
):
    course_id = handshake(client, auth_headers)
    material_id = _add_link_material(
        db_session_factory, course_id, title="Mediasite Channel", source_url=LTI_LAUNCH_URL,
    )

    resp = _resolve(
        client, auth_headers, material_id=material_id, final_url="https://example.com/some/landing/page",
    )

    assert resp.status_code == 200
    assert resp.json() == {"status": "unrecognized"}

    with db_session_factory() as session:
        resolution = session.execute(
            select(LtiResolution).where(LtiResolution.material_id == material_id)
        ).scalar_one()
        assert resolution.status == "unrecognized"
        assert resolution.final_url == "https://example.com/some/landing/page"
        assert resolution.platform is None
        assert resolution.course_id == course_id


def test_lti_resolution_null_final_url_stores_failed_and_error(client, auth_headers, db_session_factory):
    course_id = handshake(client, auth_headers)
    material_id = _add_link_material(
        db_session_factory, course_id, title="Mediasite Channel", source_url=LTI_LAUNCH_URL,
    )

    resp = _resolve(client, auth_headers, material_id=material_id, final_url=None, error="tab closed")

    assert resp.status_code == 200
    assert resp.json() == {"status": "failed"}

    with db_session_factory() as session:
        resolution = session.execute(
            select(LtiResolution).where(LtiResolution.material_id == material_id)
        ).scalar_one()
        assert resolution.status == "failed"
        assert resolution.final_url is None
        assert resolution.error == "tab closed"


def test_lti_resolution_javascript_scheme_final_url_stores_failed_never_touches_media_sources(
    client, auth_headers, db_session_factory
):
    """A javascript:-shaped URL that is otherwise Zoom-host-shaped must never
    reach classify_url/media_sources -- same defense-in-depth as
    api/media.py's manual-add `_require_absolute_http_url` guard, just
    non-raising here since this endpoint always returns 200 with a status
    field rather than a 422."""
    course_id = handshake(client, auth_headers)
    material_id = _add_link_material(
        db_session_factory, course_id, title="Mediasite Channel", source_url=LTI_LAUNCH_URL,
    )

    resp = _resolve(client, auth_headers, material_id=material_id, final_url="javascript://zoom.us/rec/share/x")

    assert resp.status_code == 200
    assert resp.json() == {"status": "failed"}

    with db_session_factory() as session:
        from brightspace_agent.db.models import MediaSource

        assert session.execute(select(MediaSource)).scalars().all() == []
        resolution = session.execute(
            select(LtiResolution).where(LtiResolution.material_id == material_id)
        ).scalar_one()
        assert resolution.status == "failed"


def test_lti_resolution_re_post_overwrites_no_duplicate_row(client, auth_headers, db_session_factory):
    course_id = handshake(client, auth_headers)
    material_id = _add_link_material(
        db_session_factory, course_id, title="Mediasite Channel", source_url=LTI_LAUNCH_URL,
    )

    first = _resolve(client, auth_headers, material_id=material_id, final_url="https://example.com/landing")
    assert first.json() == {"status": "unrecognized"}

    second = _resolve(
        client, auth_headers, material_id=material_id,
        final_url="https://mediasite.example.edu/Mediasite/Play/xyz",
    )
    assert second.status_code == 200
    assert second.json()["status"] == "resolved"

    with db_session_factory() as session:
        rows = list(
            session.execute(select(LtiResolution).where(LtiResolution.material_id == material_id)).scalars().all()
        )
        assert len(rows) == 1  # not duplicated -- UNIQUE(material_id) upsert
        assert rows[0].status == "resolved"
        assert rows[0].final_url == "https://mediasite.example.edu/Mediasite/Play/xyz"


def test_lti_resolution_non_candidate_material_id_404(client, auth_headers, db_session_factory):
    course_id = handshake(client, auth_headers)
    # A plain (non-LTI) link material -- never a candidate in the first place.
    material_id = _add_link_material(
        db_session_factory, course_id, title="Syllabus", source_url="https://zoom.us/some/plain/link",
    )

    resp = _resolve(
        client, auth_headers, material_id=material_id, final_url="https://mediasite.example.edu/Mediasite/Play/xyz",
    )

    assert resp.status_code == 404


def test_lti_resolution_unknown_material_id_404(client, auth_headers):
    handshake(client, auth_headers)
    resp = _resolve(client, auth_headers, material_id=999999, final_url="https://example.com/x")
    assert resp.status_code == 404


def test_lti_resolution_unknown_org_unit_404(client, auth_headers, db_session_factory):
    resp = client.post(
        "/api/ingest/lti-resolution",
        headers=auth_headers,
        json={"orgUnitId": 99999, "materialId": 1, "finalUrl": "https://example.com/x", "error": None},
    )
    assert resp.status_code == 404


def test_lti_resolution_requires_pairing_token(client, db_session_factory):
    resp = client.post(
        "/api/ingest/lti-resolution",
        json={"orgUnitId": ORG_UNIT_ID, "materialId": 1, "finalUrl": None, "error": None},
    )
    assert resp.status_code == 401


def test_lti_resolution_resolved_material_disappears_from_candidates(client, auth_headers, db_session_factory):
    """The payoff, end to end: a resolved material is no longer offered as a
    candidate on the next sync's GET."""
    course_id = handshake(client, auth_headers)
    material_id = _add_link_material(
        db_session_factory, course_id, title="Mediasite Channel", source_url=LTI_LAUNCH_URL,
    )

    _resolve(
        client, auth_headers, material_id=material_id,
        final_url="https://mediasite.example.edu/Mediasite/Play/xyz",
    )

    resp = client.get(f"/api/ingest/lti-candidates?orgUnitId={ORG_UNIT_ID}", headers=auth_headers)
    assert resp.json()["candidates"] == []


# -- 12c. lti-resolution POST: expand_and_upsert_media failure must still --
# leave a durable `failed` row (fix-wave item 1). On a DEFAULT install (no
# `--group media`), every recognized Mediasite/Zoom channel hits the
# not_installed 503 below -- if that path left no row, the drawer would say
# "Will resolve automatically on your next sync" forever while the extension
# re-launches a background tab on every single sync.


def test_lti_resolution_expand_not_installed_leaves_failed_row_and_503(
    client, app, auth_headers, db_session_factory, monkeypatch
):
    from brightspace_agent.media.fetch import MediaFetchError

    def boom(url):
        raise MediaFetchError("not_installed", "yt-dlp is not installed. Run `uv sync --group media` to install it.")

    monkeypatch.setattr(app.state.media_fetcher, "expand", boom)

    course_id = handshake(client, auth_headers)
    material_id = _add_link_material(
        db_session_factory, course_id, title="Mediasite Channel", source_url=LTI_LAUNCH_URL,
    )

    resp = _resolve(
        client, auth_headers, material_id=material_id,
        final_url="https://mediasite.example.edu/Mediasite/Play/xyz",
    )

    assert resp.status_code == 503
    assert "yt-dlp is not installed" in resp.json()["detail"]

    with db_session_factory() as session:
        from brightspace_agent.db.models import MediaSource

        assert session.execute(select(MediaSource)).scalars().all() == []
        resolution = session.execute(
            select(LtiResolution).where(LtiResolution.material_id == material_id)
        ).scalar_one()
        assert resolution.status == "failed"
        assert resolution.final_url == "https://mediasite.example.edu/Mediasite/Play/xyz"
        assert resolution.error is not None
        assert "yt-dlp is not installed" in resolution.error

    # Visible in the drawer's hints resolution state, not just the raw row.
    hints = client.get(f"/api/courses/{course_id}/media").json()["hints"]
    hint = next(h for h in hints if h["materialId"] == material_id)
    assert hint["resolution"]["status"] == "failed"
    assert "yt-dlp is not installed" in hint["resolution"]["error"]


def test_lti_resolution_expand_nothing_classified_leaves_failed_row_and_400(
    client, app, auth_headers, db_session_factory, monkeypatch
):
    from brightspace_agent.media.fetch import ExpandedEntry

    def expand_to_unclassifiable(url):
        return [ExpandedEntry(url="https://example.com/some/random/page", title=None)]

    monkeypatch.setattr(app.state.media_fetcher, "expand", expand_to_unclassifiable)

    course_id = handshake(client, auth_headers)
    material_id = _add_link_material(
        db_session_factory, course_id, title="Mediasite Channel", source_url=LTI_LAUNCH_URL,
    )

    resp = _resolve(
        client, auth_headers, material_id=material_id,
        # Classifies at the top-level classify_url gate, so expand() runs --
        # its (mocked) entries are what fails to classify.
        final_url="https://mediasite.example.edu/Mediasite/Play/xyz",
    )

    assert resp.status_code == 400
    assert "recognized" in resp.json()["detail"].lower()

    with db_session_factory() as session:
        from brightspace_agent.db.models import MediaSource

        assert session.execute(select(MediaSource)).scalars().all() == []
        resolution = session.execute(
            select(LtiResolution).where(LtiResolution.material_id == material_id)
        ).scalar_one()
        assert resolution.status == "failed"
        assert resolution.final_url == "https://mediasite.example.edu/Mediasite/Play/xyz"
        assert resolution.error is not None
        assert "recognized" in resolution.error.lower()


def test_lti_resolution_failed_expand_row_overwritten_by_later_success(
    client, app, auth_headers, db_session_factory, monkeypatch
):
    """A `failed` row from an expand failure is exactly as retryable as any
    other `failed` row -- a later successful re-resolution overwrites it."""
    from brightspace_agent.media.fetch import MediaFetchError

    real_expand = app.state.media_fetcher.expand

    def boom(url):
        raise MediaFetchError("not_installed", "yt-dlp is not installed.")

    monkeypatch.setattr(app.state.media_fetcher, "expand", boom)

    course_id = handshake(client, auth_headers)
    material_id = _add_link_material(
        db_session_factory, course_id, title="Mediasite Channel", source_url=LTI_LAUNCH_URL,
    )

    first = _resolve(
        client, auth_headers, material_id=material_id,
        final_url="https://mediasite.example.edu/Mediasite/Play/xyz",
    )
    assert first.status_code == 503

    # The failure itself must have landed a durable 'failed' row -- this is
    # the crux of the fix, not just a side effect re-checked below.
    with db_session_factory() as session:
        after_failure = session.execute(
            select(LtiResolution).where(LtiResolution.material_id == material_id)
        ).scalar_one()
        assert after_failure.status == "failed"

    # The material must still be offered as a candidate (failed rows retry).
    candidates = client.get(f"/api/ingest/lti-candidates?orgUnitId={ORG_UNIT_ID}", headers=auth_headers).json()
    assert [c["materialId"] for c in candidates["candidates"]] == [material_id]

    monkeypatch.setattr(app.state.media_fetcher, "expand", real_expand)

    second = _resolve(
        client, auth_headers, material_id=material_id,
        final_url="https://mediasite.example.edu/Mediasite/Play/xyz",
    )
    assert second.status_code == 200
    assert second.json()["status"] == "resolved"

    with db_session_factory() as session:
        rows = list(
            session.execute(select(LtiResolution).where(LtiResolution.material_id == material_id)).scalars().all()
        )
        assert len(rows) == 1
        assert rows[0].status == "resolved"
