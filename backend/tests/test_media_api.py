"""Tests for the M2.4 media API (api/media.py): the detected-sources list
+ active flag, the detect trigger, the course-batch and single-source
process triggers (CSRF + active-run guard + worklist rules), the passcode/
skip PUT endpoint, and the SSE event sequence for a media run.

Against a real (BSA_MOCK_LLM=1, which also forces the mock media fetcher/
transcriber) FastAPI app -- no network access, no subprocess, no API key.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from brightspace_agent.db.models import Course, Material, MediaSource, PipelineRun

CSRF_HEADERS = {"X-BSA-Request": "1"}


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BSA_DATA_DIR", str(tmp_path))
    # Forces MockBackend/MockWebBackend/MockMediaFetcher/MockTranscriber
    # regardless of the host environment -- no real subprocess or network
    # anywhere in this module.
    monkeypatch.setenv("BSA_MOCK_LLM", "1")
    return tmp_path


@pytest.fixture
def app(data_dir):
    from brightspace_agent.main import create_app

    return create_app()


@pytest.fixture
def client(app):
    # `with` (not a bare TestClient(app)) keeps one portal/event loop alive
    # across calls within one test -- required for the active-run-guard and
    # wait-for-idle tests, which rely on the background media task surviving
    # between sequential requests (see test_frontend_api.py's client
    # fixture docstring for the full explanation).
    with TestClient(app, base_url="http://127.0.0.1:8733") as test_client:
        yield test_client


@pytest.fixture
def db_session_factory(app):
    return app.state.session_factory


# --------------------------------------------------------------------------
# Seeding helpers
# --------------------------------------------------------------------------


def _add_course(db_session_factory, *, org_unit_id=1, name="Intro to CS", code="CS100") -> int:
    with db_session_factory() as session:
        course = Course(d2l_org_unit_id=org_unit_id, tenant_origin="school.d2l.com", name=name, code=code)
        session.add(course)
        session.commit()
        return course.id


def _add_material(db_session_factory, course_id, *, title="Lecture Recording", kind="link", source_url=None) -> int:
    with db_session_factory() as session:
        material = Material(
            course_id=course_id, kind=kind, title=title, source_url=source_url, status="fetched",
        )
        session.add(material)
        session.commit()
        return material.id


def _add_media_source(
    db_session_factory, course_id, material_id, *, platform="zoom", url, passcode=None, status="detected",
    error=None,
) -> int:
    with db_session_factory() as session:
        row = MediaSource(
            course_id=course_id, material_id=material_id, platform=platform, url=url, passcode=passcode,
            status=status, error=error,
            created_at="2026-01-01T00:00:00+00:00", updated_at="2026-01-01T00:00:00+00:00",
        )
        session.add(row)
        session.commit()
        return row.id


def _pipeline_runs(db_session_factory, course_id: int) -> list[PipelineRun]:
    with db_session_factory() as session:
        rows = list(
            session.execute(
                select(PipelineRun).where(PipelineRun.course_id == course_id).order_by(PipelineRun.id)
            ).scalars().all()
        )
        for row in rows:
            session.expunge(row)
        return rows


def _wait_for_media_idle(client: TestClient, course_id: int, *, timeout_s: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout_s
    body = None
    while time.monotonic() < deadline:
        body = client.get(f"/api/courses/{course_id}/media").json()
        if not body["active"]:
            return body
        time.sleep(0.02)
    raise AssertionError(f"media still active after {timeout_s}s: {body}")


def _wait_for_pipeline_idle(client: TestClient, course_id: int, *, timeout_s: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout_s
    body = None
    while time.monotonic() < deadline:
        body = client.get(f"/api/courses/{course_id}/pipeline/status").json()
        if not body["active"] and body["stages"]:
            return body
        time.sleep(0.02)
    raise AssertionError(f"pipeline still active after {timeout_s}s: {body}")


# --------------------------------------------------------------------------
# (1) full happy path: captions + audio sources -> both done, transcripts
# --------------------------------------------------------------------------


def test_full_happy_path_captions_and_audio_sources(client, app, db_session_factory):
    course_id = _add_course(db_session_factory)
    captions_material = _add_material(
        db_session_factory, course_id, title="Lecture 1", source_url="https://zoom.us/rec/share/mock-captions",
    )
    captions_source = _add_media_source(
        db_session_factory, course_id, captions_material, url="https://zoom.us/rec/share/mock-captions",
    )
    audio_material = _add_material(
        db_session_factory, course_id, title="Lecture 2",
        source_url="https://mediasite.example.edu/Mediasite/Play/xyz",
    )
    audio_source = _add_media_source(
        db_session_factory, course_id, audio_material, platform="mediasite",
        url="https://mediasite.example.edu/Mediasite/Play/xyz",
    )

    resp = client.post(f"/api/courses/{course_id}/media/process", headers=CSRF_HEADERS)
    assert resp.status_code == 200
    assert isinstance(resp.json()["runToken"], int)

    body = _wait_for_media_idle(client, course_id)
    by_id = {s["id"]: s for s in body["sources"]}
    assert by_id[captions_source]["status"] == "done"
    assert by_id[audio_source]["status"] == "done"
    assert by_id[captions_source]["transcriptMaterialId"] is not None
    assert by_id[audio_source]["transcriptMaterialId"] is not None
    assert by_id[captions_source]["error"] is None
    assert by_id[audio_source]["error"] is None

    with db_session_factory() as session:
        transcripts = list(
            session.execute(
                select(Material).where(Material.course_id == course_id, Material.kind == "transcript")
            ).scalars().all()
        )
        assert len(transcripts) == 2
        for material in transcripts:
            assert material.status == "extracted"
            text = app.state.blob_store.read_text(material.sha256)
            assert text and text.strip()

    runs = _pipeline_runs(db_session_factory, course_id)
    media_runs = [r for r in runs if r.stage == "media"]
    assert len(media_runs) == 1
    assert media_runs[0].status == "complete"
    assert media_runs[0].error is None
    assert media_runs[0].finished_at is not None
    usage = json.loads(media_runs[0].usage_json)
    assert usage == {
        "input_tokens": 0, "output_tokens": 0, "est_cost_usd": 0.0,
        "sources": 2, "done": 2, "failed": 0, "captions": 1, "transcribed": 1,
    }

    media_dir = app.state.settings.media_dir
    for source_id in (captions_source, audio_source):
        source_dir = media_dir / str(source_id)
        assert not source_dir.exists() or not any(source_dir.iterdir())  # attempt dirs cleaned up


# --------------------------------------------------------------------------
# (2) failure isolation
# --------------------------------------------------------------------------


def test_failure_isolation_one_fails_one_succeeds(client, db_session_factory):
    course_id = _add_course(db_session_factory)
    good_material = _add_material(
        db_session_factory, course_id, title="Good", source_url="https://zoom.us/rec/share/mock-captions",
    )
    good_source = _add_media_source(
        db_session_factory, course_id, good_material, url="https://zoom.us/rec/share/mock-captions",
    )
    bad_material = _add_material(
        db_session_factory, course_id, title="Bad",
        source_url="https://zoom.us/rec/share/mock-fail-wrong_passcode",
    )
    bad_source = _add_media_source(
        db_session_factory, course_id, bad_material, url="https://zoom.us/rec/share/mock-fail-wrong_passcode",
    )

    resp = client.post(f"/api/courses/{course_id}/media/process", headers=CSRF_HEADERS)
    assert resp.status_code == 200

    body = _wait_for_media_idle(client, course_id)
    by_id = {s["id"]: s for s in body["sources"]}
    assert by_id[good_source]["status"] == "done"
    assert by_id[bad_source]["status"] == "failed"
    assert by_id[bad_source]["error"] == "wrong_passcode: mock wrong_passcode"

    runs = _pipeline_runs(db_session_factory, course_id)
    media_run = [r for r in runs if r.stage == "media"][0]
    assert media_run.status == "complete"
    usage = json.loads(media_run.usage_json)
    assert usage["sources"] == 2
    assert usage["done"] == 1
    assert usage["failed"] == 1


def test_ingest_failure_after_a_successful_fetch_counts_as_failed_only(client, app, db_session_factory, monkeypatch):
    """A source whose fetch succeeds but whose `ingest_transcript` call then
    raises must be counted once, as failed -- never ALSO credited into
    counts["captions"]/["transcribed"], which would break the
    done == captions + transcribed invariant `usage_json` implies."""
    from brightspace_agent.pipeline import runner as runner_module

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated ingest failure")

    monkeypatch.setattr(runner_module, "ingest_transcript", _boom)

    course_id = _add_course(db_session_factory)
    material_id = _add_material(db_session_factory, course_id, source_url="https://zoom.us/rec/share/mock-captions")
    source_id = _add_media_source(
        db_session_factory, course_id, material_id, url="https://zoom.us/rec/share/mock-captions",
    )

    resp = client.post(f"/api/courses/{course_id}/media/process", headers=CSRF_HEADERS)
    assert resp.status_code == 200

    body = _wait_for_media_idle(client, course_id)
    assert body["sources"][0]["status"] == "failed"
    assert body["sources"][0]["error"].startswith("internal:")

    runs = _pipeline_runs(db_session_factory, course_id)
    media_run = [r for r in runs if r.stage == "media"][0]
    usage = json.loads(media_run.usage_json)
    assert usage["done"] == 0
    assert usage["failed"] == 1
    assert usage["captions"] == 0  # not credited despite the fetch itself succeeding
    assert usage["transcribed"] == 0


def test_attempt_dir_mkdir_failure_for_one_source_is_isolated(client, app, db_session_factory, monkeypatch):
    """An OS-level failure building the attempt dir (ENOSPC, permission
    denied on media_dir, ...) for ONE source must be handled the same way
    as any other per-source failure: that source ends 'failed' with an
    'internal: ...' error, and the batch continues to -- and completes --
    the remaining sources, rather than escaping to the batch handler and
    abandoning the rest."""
    import pathlib

    course_id = _add_course(db_session_factory)
    bad_material = _add_material(db_session_factory, course_id, source_url="https://zoom.us/rec/share/mock-captions")
    bad_source = _add_media_source(
        db_session_factory, course_id, bad_material, url="https://zoom.us/rec/share/mock-captions",
    )
    good_material = _add_material(
        db_session_factory, course_id, source_url="https://zoom.us/rec/share/mock-captions-2",
    )
    good_source = _add_media_source(
        db_session_factory, course_id, good_material, url="https://zoom.us/rec/share/mock-captions-2",
    )

    media_dir = app.state.settings.media_dir
    bad_source_dir = media_dir / str(bad_source)
    original_mkdir = pathlib.Path.mkdir

    def flaky_mkdir(self, *args, **kwargs):
        try:
            self.relative_to(bad_source_dir)
        except ValueError:
            return original_mkdir(self, *args, **kwargs)
        raise OSError("simulated ENOSPC")

    monkeypatch.setattr(pathlib.Path, "mkdir", flaky_mkdir)

    resp = client.post(f"/api/courses/{course_id}/media/process", headers=CSRF_HEADERS)
    assert resp.status_code == 200

    body = _wait_for_media_idle(client, course_id)
    by_id = {s["id"]: s for s in body["sources"]}
    assert by_id[bad_source]["status"] == "failed"
    assert by_id[bad_source]["error"].startswith("internal:")
    assert by_id[good_source]["status"] == "done"

    runs = _pipeline_runs(db_session_factory, course_id)
    media_run = [r for r in runs if r.stage == "media"][0]
    assert media_run.status == "complete"
    usage = json.loads(media_run.usage_json)
    assert usage["done"] == 1
    assert usage["failed"] == 1


# --------------------------------------------------------------------------
# (3) keep_media
# --------------------------------------------------------------------------


def test_keep_media_false_default_deletes_attempt_dir(client, app, db_session_factory):
    course_id = _add_course(db_session_factory)
    material_id = _add_material(db_session_factory, course_id, source_url="https://zoom.us/rec/share/mock-captions")
    source_id = _add_media_source(
        db_session_factory, course_id, material_id, url="https://zoom.us/rec/share/mock-captions",
    )

    client.post(f"/api/courses/{course_id}/media/process", headers=CSRF_HEADERS)
    _wait_for_media_idle(client, course_id)

    source_dir = app.state.settings.media_dir / str(source_id)
    assert not source_dir.exists() or not any(source_dir.iterdir())


def test_keep_media_true_keeps_attempt_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BSA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BSA_MOCK_LLM", "1")
    monkeypatch.setenv("BSA_KEEP_MEDIA", "1")
    from brightspace_agent.main import create_app

    keep_app = create_app()
    with TestClient(keep_app, base_url="http://127.0.0.1:8734") as keep_client:
        session_factory = keep_app.state.session_factory
        course_id = _add_course(session_factory)
        material_id = _add_material(session_factory, course_id, source_url="https://zoom.us/rec/share/mock-captions")
        source_id = _add_media_source(
            session_factory, course_id, material_id, url="https://zoom.us/rec/share/mock-captions",
        )

        keep_client.post(f"/api/courses/{course_id}/media/process", headers=CSRF_HEADERS)
        _wait_for_media_idle(keep_client, course_id)

        source_dir = keep_app.state.settings.media_dir / str(source_id)
        attempt_dirs = list(source_dir.iterdir())
        assert len(attempt_dirs) == 1
        assert any(attempt_dirs[0].iterdir())


# --------------------------------------------------------------------------
# (4) active-run mutual exclusion, both directions
# --------------------------------------------------------------------------


def test_media_process_409_while_pipeline_run_active(client, db_session_factory):
    course_id = _add_course(db_session_factory)

    first = client.post(f"/api/courses/{course_id}/pipeline/run", json={}, headers=CSRF_HEADERS)
    assert first.status_code == 200

    second = client.post(f"/api/courses/{course_id}/media/process", headers=CSRF_HEADERS)
    assert second.status_code == 409


def test_pipeline_run_409_while_media_process_active(client, db_session_factory):
    course_id = _add_course(db_session_factory)
    material_id = _add_material(db_session_factory, course_id, source_url="https://zoom.us/rec/share/mock-captions")
    _add_media_source(db_session_factory, course_id, material_id, url="https://zoom.us/rec/share/mock-captions")

    first = client.post(f"/api/courses/{course_id}/media/process", headers=CSRF_HEADERS)
    assert first.status_code == 200

    second = client.post(f"/api/courses/{course_id}/pipeline/run", json={}, headers=CSRF_HEADERS)
    assert second.status_code == 409


# --------------------------------------------------------------------------
# (5) GET list shape + active flag; 404 unknown course
# --------------------------------------------------------------------------


def test_get_media_list_shape(client, db_session_factory):
    course_id = _add_course(db_session_factory)
    material_id = _add_material(db_session_factory, course_id, source_url="https://zoom.us/rec/share/abc")
    source_id = _add_media_source(
        db_session_factory, course_id, material_id, url="https://zoom.us/rec/share/abc", passcode="s3cret",
    )

    resp = client.get(f"/api/courses/{course_id}/media")
    assert resp.status_code == 200
    body = resp.json()
    assert body["active"] is False
    assert len(body["sources"]) == 1
    source = body["sources"][0]
    assert set(source) == {
        "id", "materialId", "materialTitle", "platform", "url", "passcode", "status", "error",
        "transcriptMaterialId", "updatedAt",
    }
    assert source["id"] == source_id
    assert source["materialId"] == material_id
    assert source["materialTitle"] == "Lecture Recording"
    assert source["passcode"] == "s3cret"
    assert source["status"] == "detected"
    assert source["error"] is None
    assert source["transcriptMaterialId"] is None


def test_get_media_sorted_newest_first(client, db_session_factory):
    course_id = _add_course(db_session_factory)
    m1 = _add_material(db_session_factory, course_id, source_url="https://zoom.us/rec/share/a")
    s1 = _add_media_source(db_session_factory, course_id, m1, url="https://zoom.us/rec/share/a")
    m2 = _add_material(db_session_factory, course_id, source_url="https://zoom.us/rec/share/b")
    s2 = _add_media_source(db_session_factory, course_id, m2, url="https://zoom.us/rec/share/b")

    body = client.get(f"/api/courses/{course_id}/media").json()
    assert [s["id"] for s in body["sources"]] == [s2, s1]


def test_get_media_unknown_course_404(client):
    resp = client.get("/api/courses/999999/media")
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# (6) detect endpoint: stats + CSRF
# --------------------------------------------------------------------------


def test_detect_requires_csrf_and_returns_stats(client, db_session_factory):
    course_id = _add_course(db_session_factory)
    _add_material(db_session_factory, course_id, kind="link", source_url="https://zoom.us/rec/share/xyz")

    no_header = client.post(f"/api/courses/{course_id}/media/detect")
    assert no_header.status_code == 403

    resp = client.post(f"/api/courses/{course_id}/media/detect", headers=CSRF_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["scannedMaterials"] == 1
    assert body["found"] == 1
    assert body["added"] == 1

    with db_session_factory() as session:
        rows = list(session.execute(select(MediaSource).where(MediaSource.course_id == course_id)).scalars().all())
        assert len(rows) == 1
        assert rows[0].platform == "zoom"


def test_detect_unknown_course_404(client):
    resp = client.post("/api/courses/999999/media/detect", headers=CSRF_HEADERS)
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# (7) POST course/media/process: CSRF + 400 when nothing to process + 404
# --------------------------------------------------------------------------


def test_course_process_requires_csrf(client, db_session_factory):
    course_id = _add_course(db_session_factory)
    no_header = client.post(f"/api/courses/{course_id}/media/process")
    assert no_header.status_code == 403


def test_course_process_400_when_nothing_to_process(client, db_session_factory):
    course_id = _add_course(db_session_factory)
    resp = client.post(f"/api/courses/{course_id}/media/process", headers=CSRF_HEADERS)
    assert resp.status_code == 400


def test_course_process_skipped_and_done_rows_are_not_auto_included(client, db_session_factory):
    course_id = _add_course(db_session_factory)
    skipped_material = _add_material(db_session_factory, course_id, source_url="https://zoom.us/rec/share/skip")
    _add_media_source(
        db_session_factory, course_id, skipped_material, url="https://zoom.us/rec/share/skip", status="skipped",
    )
    done_material = _add_material(db_session_factory, course_id, source_url="https://zoom.us/rec/share/done")
    _add_media_source(
        db_session_factory, course_id, done_material, url="https://zoom.us/rec/share/done", status="done",
    )

    resp = client.post(f"/api/courses/{course_id}/media/process", headers=CSRF_HEADERS)
    assert resp.status_code == 400  # neither skipped nor done counts toward the default worklist


def test_course_process_unknown_course_404(client):
    resp = client.post("/api/courses/999999/media/process", headers=CSRF_HEADERS)
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# (8) single-source process
# --------------------------------------------------------------------------


def test_single_source_process_only_runs_that_source(client, db_session_factory):
    course_id = _add_course(db_session_factory)
    m1 = _add_material(db_session_factory, course_id, source_url="https://zoom.us/rec/share/mock-captions")
    s1 = _add_media_source(db_session_factory, course_id, m1, url="https://zoom.us/rec/share/mock-captions")
    m2 = _add_material(db_session_factory, course_id, source_url="https://zoom.us/rec/share/mock-captions-2")
    s2 = _add_media_source(db_session_factory, course_id, m2, url="https://zoom.us/rec/share/mock-captions-2")

    resp = client.post(f"/api/media/{s1}/process", headers=CSRF_HEADERS)
    assert resp.status_code == 200

    body = _wait_for_media_idle(client, course_id)
    by_id = {s["id"]: s["status"] for s in body["sources"]}
    assert by_id[s1] == "done"
    assert by_id[s2] == "detected"  # untouched


def test_single_source_process_done_row_is_400(client, db_session_factory):
    course_id = _add_course(db_session_factory)
    material_id = _add_material(db_session_factory, course_id, source_url="https://zoom.us/rec/share/x")
    source_id = _add_media_source(
        db_session_factory, course_id, material_id, url="https://zoom.us/rec/share/x", status="done",
    )

    resp = client.post(f"/api/media/{source_id}/process", headers=CSRF_HEADERS)
    assert resp.status_code == 400


def test_single_source_process_retries_a_failed_row(client, db_session_factory):
    course_id = _add_course(db_session_factory)
    material_id = _add_material(db_session_factory, course_id, source_url="https://zoom.us/rec/share/mock-captions")
    source_id = _add_media_source(
        db_session_factory, course_id, material_id, url="https://zoom.us/rec/share/mock-captions",
        status="failed", error="wrong_passcode: mock wrong_passcode",
    )

    resp = client.post(f"/api/media/{source_id}/process", headers=CSRF_HEADERS)
    assert resp.status_code == 200

    body = _wait_for_media_idle(client, course_id)
    assert body["sources"][0]["status"] == "done"


def test_single_source_process_unknown_404(client):
    resp = client.post("/api/media/999999/process", headers=CSRF_HEADERS)
    assert resp.status_code == 404


def test_single_source_process_requires_csrf(client, db_session_factory):
    course_id = _add_course(db_session_factory)
    material_id = _add_material(db_session_factory, course_id, source_url="https://zoom.us/rec/share/mock-captions")
    source_id = _add_media_source(
        db_session_factory, course_id, material_id, url="https://zoom.us/rec/share/mock-captions",
    )

    no_header = client.post(f"/api/media/{source_id}/process")
    assert no_header.status_code == 403


# --------------------------------------------------------------------------
# (9) PUT /api/media/{source_id}: passcode + skip/unskip + 409s
# --------------------------------------------------------------------------


def test_put_requires_csrf(client, db_session_factory):
    course_id = _add_course(db_session_factory)
    material_id = _add_material(db_session_factory, course_id, source_url="https://zoom.us/rec/share/x")
    source_id = _add_media_source(db_session_factory, course_id, material_id, url="https://zoom.us/rec/share/x")

    resp = client.put(f"/api/media/{source_id}", json={"passcode": "abc"})
    assert resp.status_code == 403


def test_put_set_and_clear_passcode(client, db_session_factory):
    course_id = _add_course(db_session_factory)
    material_id = _add_material(db_session_factory, course_id, source_url="https://zoom.us/rec/share/x")
    source_id = _add_media_source(db_session_factory, course_id, material_id, url="https://zoom.us/rec/share/x")

    resp = client.put(f"/api/media/{source_id}", json={"passcode": "abc"}, headers=CSRF_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["passcode"] == "abc"

    resp2 = client.put(f"/api/media/{source_id}", json={"passcode": None}, headers=CSRF_HEADERS)
    assert resp2.status_code == 200
    assert resp2.json()["passcode"] is None


def test_put_skip_and_unskip(client, db_session_factory):
    course_id = _add_course(db_session_factory)
    material_id = _add_material(db_session_factory, course_id, source_url="https://zoom.us/rec/share/x")
    source_id = _add_media_source(db_session_factory, course_id, material_id, url="https://zoom.us/rec/share/x")

    resp = client.put(f"/api/media/{source_id}", json={"status": "skipped"}, headers=CSRF_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped"

    resp2 = client.put(f"/api/media/{source_id}", json={"status": "detected"}, headers=CSRF_HEADERS)
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "detected"


def test_put_409_while_run_active(client, db_session_factory):
    course_id = _add_course(db_session_factory)
    material_id = _add_material(db_session_factory, course_id, source_url="https://zoom.us/rec/share/mock-captions")
    source_id = _add_media_source(
        db_session_factory, course_id, material_id, url="https://zoom.us/rec/share/mock-captions",
    )

    process_resp = client.post(f"/api/courses/{course_id}/media/process", headers=CSRF_HEADERS)
    assert process_resp.status_code == 200

    put_resp = client.put(f"/api/media/{source_id}", json={"status": "skipped"}, headers=CSRF_HEADERS)
    assert put_resp.status_code == 409

    _wait_for_media_idle(client, course_id)


def test_put_409_for_disallowed_transition_while_fetching(client, db_session_factory):
    course_id = _add_course(db_session_factory)
    material_id = _add_material(db_session_factory, course_id, source_url="https://zoom.us/rec/share/x")
    source_id = _add_media_source(
        db_session_factory, course_id, material_id, url="https://zoom.us/rec/share/x", status="fetching",
    )

    resp = client.put(f"/api/media/{source_id}", json={"status": "skipped"}, headers=CSRF_HEADERS)
    assert resp.status_code == 409


def test_put_unknown_source_404(client):
    resp = client.put("/api/media/999999", json={"status": "skipped"}, headers=CSRF_HEADERS)
    assert resp.status_code == 404


def test_put_explicit_null_status_is_rejected_not_500(client, db_session_factory):
    """The contract only allows `status: 'skipped'|'detected'` -- an
    explicit `"status": null` must be rejected cleanly, never reach the
    NOT NULL `media_sources.status` column (which would 500 on commit)."""
    course_id = _add_course(db_session_factory)
    material_id = _add_material(db_session_factory, course_id, source_url="https://zoom.us/rec/share/x")
    source_id = _add_media_source(db_session_factory, course_id, material_id, url="https://zoom.us/rec/share/x")

    resp = client.put(f"/api/media/{source_id}", json={"status": None}, headers=CSRF_HEADERS)
    assert resp.status_code in (400, 422)

    with db_session_factory() as session:
        row = session.get(MediaSource, source_id)
        assert row.status == "detected"  # unchanged


# --------------------------------------------------------------------------
# (10) SSE: the event bus sequence for a media run. Subscribes to
# `runner.event_bus` directly (rather than reading the wire-level
# `GET /api/events` stream the way test_frontend_api.py's
# test_sse_events_stream_shows_a_triggered_run does) -- same pattern
# test_enrichment_runner.py uses for the enrichment path's SSE sequence.
# The wire-level framing (SSE headers, `data:` lines, heartbeats) is generic
# infra already covered by that pipeline test; what's specific to media is
# the EVENT CONTENT this asserts: run-started, per-source fetching/done, and
# a terminal complete, all with type "media".
# --------------------------------------------------------------------------


def test_media_events_sequence_run_started_fetching_done_complete(app, db_session_factory):
    course_id = _add_course(db_session_factory)
    material_id = _add_material(db_session_factory, course_id, source_url="https://zoom.us/rec/share/mock-captions")
    _add_media_source(db_session_factory, course_id, material_id, url="https://zoom.us/rec/share/mock-captions")

    async def scenario():
        runner = app.state.runner
        queue = runner.event_bus.subscribe()

        run_token = runner.start_media(course_id)

        events = []
        while True:
            event = await asyncio.wait_for(queue.get(), timeout=5)
            events.append(event)
            if event["status"] in ("complete", "failed"):
                break

        runner.event_bus.unsubscribe(queue)
        return events, run_token

    events, run_token = asyncio.run(scenario())

    for event in events:
        assert event["type"] == "media"
        assert event["courseId"] == course_id
        assert event["runToken"] == run_token

    assert events[0]["status"] == "run-started"
    assert "sourceId" not in events[0]

    fetching_events = [e for e in events if e["status"] == "fetching"]
    assert fetching_events and "sourceId" in fetching_events[0]

    done_events = [e for e in events if e["status"] == "done"]
    assert done_events and "sourceId" in done_events[0]

    assert events[-1]["status"] == "complete"
    assert "sourceId" not in events[-1]


# --------------------------------------------------------------------------
# (11) The payoff of treating a transcript as just another material: it must
# flow through the ORDINARY pipeline with no media-specific handling
# anywhere downstream. Everything above this point stops at "a transcript
# material row exists"; this is the one test that carries a recording all
# the way to the graph the student actually reads.
# --------------------------------------------------------------------------


def _add_extracted_material(app, db_session_factory, course_id, *, title, body) -> int:
    """A material already past the extract pass (status='extracted', text
    sidecar written) -- the state `ingest_transcript` leaves a transcript
    in, so the transcript under test isn't the only thing S1/S2 have to work
    with."""
    sha256, size = app.state.blob_store.put_bytes(body.encode("utf-8"))
    app.state.blob_store.write_text(sha256, body)
    with db_session_factory() as session:
        material = Material(
            course_id=course_id, kind="document", title=title, mime="text/plain",
            sha256=sha256, size_bytes=size, status="extracted",
        )
        session.add(material)
        session.commit()
        return material.id


def test_transcript_flows_through_the_pipeline_into_the_graph(client, app, db_session_factory):
    course_id = _add_course(db_session_factory)
    _add_extracted_material(
        app, db_session_factory, course_id,
        title="Course Syllabus", body="CS100 syllabus. Week 1 intro. Week 2 sorting algorithms.",
    )
    material_id = _add_material(
        db_session_factory, course_id, title="Lecture 1 Recording",
        source_url="https://zoom.us/rec/share/mock-captions",
    )
    source_id = _add_media_source(
        db_session_factory, course_id, material_id, url="https://zoom.us/rec/share/mock-captions",
    )

    assert client.post(f"/api/courses/{course_id}/media/process", headers=CSRF_HEADERS).status_code == 200
    media_body = _wait_for_media_idle(client, course_id)
    transcript_material_id = media_body["sources"][0]["transcriptMaterialId"]
    assert media_body["sources"][0]["status"] == "done"
    assert transcript_material_id is not None

    with db_session_factory() as session:
        assert session.get(Material, transcript_material_id).status == "extracted"  # pre-pipeline

    assert client.post(f"/api/courses/{course_id}/pipeline/run", headers=CSRF_HEADERS).status_code == 200
    status = _wait_for_pipeline_idle(client, course_id)
    assert [s for s in status["stages"] if s["status"] not in ("complete", "aborted")] == []

    with db_session_factory() as session:
        transcript = session.get(Material, transcript_material_id)
        assert transcript.kind == "transcript"
        assert transcript.status == "summarized"
        assert transcript.summary

    graph = client.get(f"/api/courses/{course_id}/graph").json()
    assert transcript_material_id in {m["id"] for m in graph["materials"]}
    # Attached to a real topic or to Unsorted -- either way it is reachable
    # from the graph, which is the invariant that matters (a material with
    # zero attachments is invisible to the student).
    assert transcript_material_id in {a["materialId"] for a in graph["attachments"]}

    # And the source row still points at it, so the drawer's "Transcript
    # ready" affordance survives a pipeline run.
    after = client.get(f"/api/courses/{course_id}/media").json()
    assert after["sources"][0]["id"] == source_id
    assert after["sources"][0]["transcriptMaterialId"] == transcript_material_id
