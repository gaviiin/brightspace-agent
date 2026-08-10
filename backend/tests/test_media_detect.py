"""Tests for the M2.1 recording-URL detector (media/detect.py): classifying
Mediasite/Zoom/Google Drive URLs out of already-synced materials, and the
/api/ingest/complete wire-up that runs it at sync completion.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from brightspace_agent.db.models import Course, Material, MediaSource
from brightspace_agent.db.session import init_db
from brightspace_agent.ingest.store import BlobStore
from brightspace_agent.media.detect import DetectStats, detect_media_sources

# --------------------------------------------------------------------------
# Fixtures -- direct DB/blob-store setup, no HTTP, matching
# test_summarize_stage.py's pattern -- except the blob store has to line up
# with BSA_DATA_DIR (via Settings()), not an arbitrary tmp_path: unlike
# run_summarize_stage, detect_media_sources takes only (session_factory,
# course_id) -- no blob_store parameter -- so it builds its own from
# Settings() internally (see detect.py's `_open_blob_store`). `data_dir`
# below is what test_ingest_api.py's own fixture of the same name does.
# --------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BSA_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def db(data_dir):
    return init_db(data_dir / "brightspace.db")


@pytest.fixture
def session_factory(db):
    return db[1]


@pytest.fixture
def blob_store(data_dir):
    return BlobStore(blobs_dir=data_dir / "blobs", text_dir=data_dir / "text")


@pytest.fixture
def course_id(session_factory):
    with session_factory() as session:
        course = Course(d2l_org_unit_id=1, tenant_origin="school.d2l.com", name="Intro to CS")
        session.add(course)
        session.commit()
        return course.id


def _add_link_material(session_factory, course_id, *, source_url, title="A Link", d2l_topic_id=None):
    with session_factory() as session:
        material = Material(
            course_id=course_id,
            d2l_topic_id=d2l_topic_id,
            kind="link",
            title=title,
            source_url=source_url,
            status="fetched",
        )
        session.add(material)
        session.commit()
        return material.id


def _add_html_material(session_factory, blob_store, course_id, *, html, title="A Page", d2l_topic_id=None):
    sha256, size = blob_store.put_bytes(html.encode("utf-8"))
    with session_factory() as session:
        material = Material(
            course_id=course_id,
            d2l_topic_id=d2l_topic_id,
            kind="other",
            title=title,
            sha256=sha256,
            mime="text/html",
            size_bytes=size,
            status="fetched",
        )
        session.add(material)
        session.commit()
        return material.id


def _rows(session_factory, course_id) -> list[MediaSource]:
    with session_factory() as session:
        return list(
            session.execute(select(MediaSource).where(MediaSource.course_id == course_id)).scalars().all()
        )


# --------------------------------------------------------------------------
# Link materials
# --------------------------------------------------------------------------


def test_link_material_mediasite_url_detected(session_factory, blob_store, course_id):
    _add_link_material(
        session_factory, course_id,
        source_url="https://media.school.edu/Mediasite/Play/abc123def456",
        d2l_topic_id=1,
    )

    stats = detect_media_sources(session_factory, course_id)

    rows = _rows(session_factory, course_id)
    assert len(rows) == 1
    assert rows[0].platform == "mediasite"
    assert rows[0].url == "https://media.school.edu/Mediasite/Play/abc123def456"
    assert rows[0].passcode is None
    assert isinstance(stats, DetectStats)
    assert stats.added == 1
    assert stats.found == 1
    assert stats.scanned_materials == 1


def test_zoom_link_pwd_query_param_copied_to_passcode(session_factory, blob_store, course_id):
    _add_link_material(
        session_factory, course_id,
        source_url="https://zoom.us/rec/share/xyz789?pwd=Secret99",
        d2l_topic_id=1,
    )

    detect_media_sources(session_factory, course_id)

    rows = _rows(session_factory, course_id)
    assert len(rows) == 1
    assert rows[0].platform == "zoom"
    # URL is stored verbatim -- the query string (including ?pwd=) stays.
    assert rows[0].url == "https://zoom.us/rec/share/xyz789?pwd=Secret99"
    assert rows[0].passcode == "Secret99"


def test_non_recording_links_are_ignored(session_factory, blob_store, course_id):
    _add_link_material(session_factory, course_id, source_url="https://youtube.com/watch?v=abc123", d2l_topic_id=1)
    _add_link_material(session_factory, course_id, source_url="https://example.com/notes.html", d2l_topic_id=2)

    stats = detect_media_sources(session_factory, course_id)

    assert _rows(session_factory, course_id) == []
    assert stats.found == 0
    assert stats.added == 0
    assert stats.scanned_materials == 2


# --------------------------------------------------------------------------
# HTML page materials
# --------------------------------------------------------------------------


def test_html_page_zoom_link_with_nearby_passcode_text(session_factory, blob_store, course_id):
    html = (
        "<html><body>"
        "<p>Join the recording: "
        '<a href="https://uni.zoom.us/rec/share/abcDEF123">watch here</a>. '
        "Passcode: aBc123!</p>"
        "</body></html>"
    )
    _add_html_material(session_factory, blob_store, course_id, html=html, d2l_topic_id=1)

    detect_media_sources(session_factory, course_id)

    rows = _rows(session_factory, course_id)
    assert len(rows) == 1
    assert rows[0].platform == "zoom"
    assert rows[0].url == "https://uni.zoom.us/rec/share/abcDEF123"
    assert rows[0].passcode == "aBc123!"


def test_html_page_non_recording_hrefs_ignored(session_factory, blob_store, course_id):
    html = (
        "<html><body>"
        '<a href="https://youtube.com/watch?v=zzz">unrelated video</a>'
        '<a href="/relative/path">relative link</a>'
        "</body></html>"
    )
    _add_html_material(session_factory, blob_store, course_id, html=html, d2l_topic_id=1)

    stats = detect_media_sources(session_factory, course_id)

    assert _rows(session_factory, course_id) == []
    assert stats.scanned_materials == 1
    assert stats.found == 0


# --------------------------------------------------------------------------
# Google Drive normalization/dedup
# --------------------------------------------------------------------------


def test_drive_two_forms_of_same_id_dedup_to_one_row(session_factory, blob_store, course_id):
    _add_link_material(
        session_factory, course_id,
        source_url="https://drive.google.com/file/d/1AbCdEfGhIjK/view?usp=sharing",
        d2l_topic_id=1,
    )
    html = '<html><body><a href="https://drive.google.com/uc?id=1AbCdEfGhIjK&export=download">download</a></body></html>'
    _add_html_material(session_factory, blob_store, course_id, html=html, d2l_topic_id=2)

    stats = detect_media_sources(session_factory, course_id)

    rows = _rows(session_factory, course_id)
    assert len(rows) == 1
    assert rows[0].platform == "gdrive"
    assert rows[0].url == "https://drive.google.com/file/d/1AbCdEfGhIjK/view"
    assert stats.found == 2  # two candidate URLs seen
    assert stats.added == 1  # but only one new row


def test_drive_usercontent_host_with_id_param(session_factory, blob_store, course_id):
    _add_link_material(
        session_factory, course_id,
        source_url="https://drive.usercontent.google.com/download?id=99ZzYy&export=download",
        d2l_topic_id=1,
    )

    detect_media_sources(session_factory, course_id)

    rows = _rows(session_factory, course_id)
    assert len(rows) == 1
    assert rows[0].url == "https://drive.google.com/file/d/99ZzYy/view"


def test_drive_folders_are_out_of_scope(session_factory, blob_store, course_id):
    _add_link_material(
        session_factory, course_id,
        source_url="https://drive.google.com/drive/folders/1FoLdEr",
        d2l_topic_id=1,
    )

    detect_media_sources(session_factory, course_id)

    assert _rows(session_factory, course_id) == []


# --------------------------------------------------------------------------
# Re-detection: upsert semantics
# --------------------------------------------------------------------------


def test_redetect_no_duplicates_status_untouched_passcode_filled_only_when_null(
    session_factory, blob_store, course_id
):
    material_id = _add_link_material(
        session_factory, course_id, source_url="https://zoom.us/rec/share/samepath", d2l_topic_id=1
    )

    detect_media_sources(session_factory, course_id)
    rows = _rows(session_factory, course_id)
    assert len(rows) == 1
    assert rows[0].passcode is None
    row_id = rows[0].id
    original_created_at = rows[0].created_at

    # Simulate downstream progress: mark the row 'done' by hand.
    with session_factory() as session:
        row = session.get(MediaSource, row_id)
        row.status = "done"
        session.commit()

    # A second material surfaces the SAME url with a nearby passcode --
    # re-detection must fill the passcode in without disturbing status.
    html = (
        f'<html><body><a href="https://zoom.us/rec/share/samepath">watch</a> '
        "Passcode: NewPass1</body></html>"
    )
    _add_html_material(session_factory, blob_store, course_id, html=html, d2l_topic_id=2)

    stats = detect_media_sources(session_factory, course_id)

    rows = _rows(session_factory, course_id)
    assert len(rows) == 1  # no duplicate row
    assert rows[0].id == row_id
    assert rows[0].status == "done"  # untouched
    assert rows[0].passcode == "NewPass1"  # filled in, was NULL
    assert rows[0].created_at == original_created_at
    assert stats.added == 0  # nothing NEW was inserted this round

    # A third round with the same candidate must not overwrite the
    # already-filled passcode, and must not touch status either.
    detect_media_sources(session_factory, course_id)
    rows = _rows(session_factory, course_id)
    assert len(rows) == 1
    assert rows[0].status == "done"
    assert rows[0].passcode == "NewPass1"
    assert material_id  # the original link material is still what it was


# --------------------------------------------------------------------------
# /api/ingest/complete wire-up -- reuses the `data_dir` fixture above so the
# app's own blob store and detect_media_sources's internally-built one
# (Settings()-derived) resolve to the same directory.
# --------------------------------------------------------------------------


@pytest.fixture
def app(data_dir, monkeypatch):
    monkeypatch.setenv("BSA_MOCK_LLM", "1")
    from brightspace_agent.main import create_app

    return create_app()


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    return TestClient(app, base_url="http://127.0.0.1:8730")


@pytest.fixture
def pairing_token(data_dir):
    import tomllib

    config = tomllib.loads((data_dir / "config.toml").read_text())
    return config["pairing_token"]


@pytest.fixture
def auth_headers(pairing_token):
    return {"Authorization": f"Bearer {pairing_token}"}


@pytest.fixture
def api_db_session_factory(data_dir):
    from brightspace_agent.db.session import init_db as _init_db

    _, factory = _init_db(data_dir / "brightspace.db")
    return factory


def _handshake(client, auth_headers, org_unit_id=555):
    resp = client.post(
        "/api/ingest/handshake",
        headers=auth_headers,
        json={
            "tenantOrigin": "https://school.d2l.com",
            "apiVersions": {},
            "whoami": {},
            "enrollments": [{"orgUnitId": org_unit_id, "name": "Intro to CS", "code": "CS101"}],
        },
    )
    return resp.json()["knownCourses"][0]["courseId"]


def _start_sync_run(client, auth_headers, org_unit_id=555):
    resp = client.post(
        "/api/ingest/toc",
        headers=auth_headers,
        json={"orgUnitId": org_unit_id, "toc": {"Modules": []}, "extras": None},
    )
    return resp.json()["syncRunId"]


def test_complete_wireup_reports_media_detected(client, auth_headers, api_db_session_factory):
    course_id = _handshake(client, auth_headers)
    sync_run_id = _start_sync_run(client, auth_headers)

    _add_link_material(
        api_db_session_factory, course_id,
        source_url="https://media.school.edu/Mediasite/Play/wireuptest",
        d2l_topic_id=1,
    )

    resp = client.post(
        "/api/ingest/complete", headers=auth_headers, json={"syncRunId": sync_run_id, "errors": []}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["mediaDetected"] == 1

    with api_db_session_factory() as session:
        rows = session.execute(select(MediaSource).where(MediaSource.course_id == course_id)).scalars().all()
    assert len(rows) == 1
    assert rows[0].platform == "mediasite"


def test_complete_detection_failure_does_not_fail_the_request(
    client, auth_headers, api_db_session_factory, monkeypatch
):
    _handshake(client, auth_headers)
    sync_run_id = _start_sync_run(client, auth_headers)

    def _boom(session_factory, course_id):
        raise RuntimeError("detector exploded")

    monkeypatch.setattr("brightspace_agent.api.ingest.detect_media_sources", _boom)

    resp = client.post(
        "/api/ingest/complete", headers=auth_headers, json={"syncRunId": sync_run_id, "errors": []}
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "complete"
