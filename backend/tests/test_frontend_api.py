"""Tests for the frontend-facing read APIs, the CSRF rule on mutating
pipeline endpoints, the dry-run cost estimate, and the SSE event stream.
Against a real (mocked-LLM) FastAPI app -- no network access, no API key.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import select

from brightspace_agent.db.models import Course, Material, MaterialTopic, Topic

CSRF_HEADERS = {"X-BSA-Request": "1"}


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BSA_DATA_DIR", str(tmp_path))
    # This file (unlike the other API test modules) actually exercises the
    # runner end to end -- force MockBackend regardless of whatever's in
    # the host environment, so these tests can't accidentally make a real
    # network call just because an ANTHROPIC_API_KEY happens to be set.
    monkeypatch.setenv("BSA_MOCK_LLM", "1")
    return tmp_path


@pytest.fixture
def app(data_dir):
    from brightspace_agent.main import create_app

    return create_app()


@pytest.fixture
def client(app):
    # `with` (not a bare TestClient(app)) matters here: without it, every
    # call gets its own throwaway portal/event loop that's torn down (and
    # any orphaned asyncio.create_task() it started along the way
    # cancelled) as soon as that one call returns -- fine for request/
    # response tests, but it silently kills PipelineRunner's background
    # run between two calls in the same test, e.g. the active-run-guard
    # test below. `with` keeps one portal alive for the fixture's scope,
    # matching how a real server actually behaves.
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def db_session_factory(app):
    return app.state.session_factory


@pytest.fixture
def blob_store(app):
    return app.state.blob_store


def _add_course(db_session_factory, *, org_unit_id=1, name="Intro to CS", code="CS100") -> int:
    with db_session_factory() as session:
        course = Course(d2l_org_unit_id=org_unit_id, tenant_origin="school.d2l.com", name=name, code=code)
        session.add(course)
        session.commit()
        return course.id


def _add_material(db_session_factory, course_id, **kwargs) -> int:
    with db_session_factory() as session:
        material = Material(course_id=course_id, **kwargs)
        session.add(material)
        session.commit()
        return material.id


# --------------------------------------------------------------------------
# (1) GET /api/courses
# --------------------------------------------------------------------------


def test_courses_list_shape_and_material_counts(client, db_session_factory):
    course_id = _add_course(db_session_factory)
    _add_material(db_session_factory, course_id, kind="document", title="A", status="summarized", sha256="a" * 64)
    _add_material(db_session_factory, course_id, kind="document", title="B", status="summarized", sha256="b" * 64)
    _add_material(db_session_factory, course_id, kind="document", title="C", status="failed", sha256="c" * 64)
    _add_material(db_session_factory, course_id, kind="link", title="D", status="fetched")

    resp = client.get("/api/courses")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    course_out = body[0]
    assert set(course_out) == {
        "id", "orgUnitId", "name", "code", "term", "taxonomyVersion", "lastSyncedAt", "materialCounts", "pipeline",
    }
    assert course_out["id"] == course_id
    assert course_out["orgUnitId"] == 1
    assert course_out["name"] == "Intro to CS"
    assert course_out["taxonomyVersion"] == 0
    assert course_out["pipeline"] is None  # no pipeline run has ever happened
    assert course_out["materialCounts"] == {"total": 4, "summarized": 2, "failed": 1}

    single = client.get(f"/api/courses/{course_id}")
    assert single.status_code == 200
    assert single.json() == course_out


def test_get_course_unknown_404(client):
    resp = client.get("/api/courses/999999")
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# (2) GET /api/courses/{id}/graph
# --------------------------------------------------------------------------


def test_graph_unknown_course_404(client):
    resp = client.get("/api/courses/999999/graph")
    assert resp.status_code == 404


def test_graph_shape_on_seeded_course(client, db_session_factory):
    course_id = _add_course(db_session_factory)
    with db_session_factory() as session:
        topic = Topic(
            course_id=course_id, taxonomy_version=1, slug="intro", name="Intro",
            description="Introductory material.", order_index=0, created_by="agent",
        )
        session.add(topic)
        session.flush()
        material = Material(course_id=course_id, kind="document", title="Lecture 1", status="summarized")
        session.add(material)
        session.flush()
        session.add(
            MaterialTopic(
                material_id=material.id, topic_id=topic.id, taxonomy_version=1,
                confidence=0.8, rationale="on topic", method="llm", review_status="auto",
            )
        )
        session.get(Course, course_id).taxonomy_version = 1
        session.commit()

    resp = client.get(f"/api/courses/{course_id}/graph")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"topics", "materials", "topicEdges", "attachments", "meta"}
    assert body["meta"]["taxonomyVersion"] == 1
    assert [t["slug"] for t in body["topics"]] == ["intro"]


# --------------------------------------------------------------------------
# (3) materials: file + text
# --------------------------------------------------------------------------


def test_material_file_streams_exact_bytes_with_mime(client, db_session_factory, blob_store):
    course_id = _add_course(db_session_factory)
    data = b"%PDF-1.4 fake pdf content for streaming test"
    sha256, size = blob_store.put_bytes(data)
    material_id = _add_material(
        db_session_factory, course_id, kind="document", title="Syllabus.pdf", mime="application/pdf",
        sha256=sha256, size_bytes=size, status="extracted",
    )

    resp = client.get(f"/api/materials/{material_id}/file")
    assert resp.status_code == 200
    assert resp.content == data
    assert resp.headers["content-type"] == "application/pdf"
    assert "inline" in resp.headers["content-disposition"]


def test_material_text_returns_sidecar(client, db_session_factory, blob_store):
    course_id = _add_course(db_session_factory)
    body = "Extracted plain text sidecar content."
    sha256, size = blob_store.put_bytes(body.encode("utf-8"))
    blob_store.write_text(sha256, body)
    material_id = _add_material(
        db_session_factory, course_id, kind="document", title="Notes", mime="text/plain",
        sha256=sha256, size_bytes=size, status="extracted",
    )

    resp = client.get(f"/api/materials/{material_id}/text")
    assert resp.status_code == 200
    assert resp.text == body
    assert resp.headers["content-type"].startswith("text/plain")


def test_material_get_shape(client, db_session_factory):
    course_id = _add_course(db_session_factory)
    material_id = _add_material(
        db_session_factory, course_id, kind="document", title="Lecture 1", mime="application/pdf",
        sha256="a" * 64, size_bytes=100, status="summarized", summary="A short summary.",
        summary_meta_json=json.dumps({"key_terms": ["alpha", "beta"]}),
    )

    resp = client.get(f"/api/materials/{material_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == material_id
    assert body["courseId"] == course_id
    assert body["title"] == "Lecture 1"
    assert body["summary"] == "A short summary."
    assert body["keyTerms"] == ["alpha", "beta"]
    assert body["topicIds"] == []


def test_material_and_file_and_text_404s(client, db_session_factory):
    assert client.get("/api/materials/999999").status_code == 404
    assert client.get("/api/materials/999999/file").status_code == 404
    assert client.get("/api/materials/999999/text").status_code == 404

    course_id = _add_course(db_session_factory)
    # A material with no sha256 (a link, or an un-uploaded stub) has neither
    # a blob nor a text sidecar.
    material_id = _add_material(db_session_factory, course_id, kind="link", title="Course site", status="fetched")
    assert client.get(f"/api/materials/{material_id}/file").status_code == 404
    assert client.get(f"/api/materials/{material_id}/text").status_code == 404


# --------------------------------------------------------------------------
# (4) pipeline/run: CSRF + active-run guard
# --------------------------------------------------------------------------


def test_pipeline_run_requires_csrf_header(client, db_session_factory):
    course_id = _add_course(db_session_factory)

    no_header = client.post(f"/api/courses/{course_id}/pipeline/run", json={})
    assert no_header.status_code == 403

    with_header = client.post(f"/api/courses/{course_id}/pipeline/run", json={}, headers=CSRF_HEADERS)
    assert with_header.status_code == 200
    body = with_header.json()
    assert isinstance(body["runToken"], int)


def test_pipeline_run_while_active_is_409(client, db_session_factory):
    course_id = _add_course(db_session_factory)

    first = client.post(f"/api/courses/{course_id}/pipeline/run", json={}, headers=CSRF_HEADERS)
    assert first.status_code == 200

    second = client.post(f"/api/courses/{course_id}/pipeline/run", json={}, headers=CSRF_HEADERS)
    assert second.status_code == 409


def test_pipeline_run_unknown_course_404(client):
    resp = client.post("/api/courses/999999/pipeline/run", json={}, headers=CSRF_HEADERS)
    assert resp.status_code == 404


def _wait_for_pipeline_idle(client, course_id, *, timeout_s=5.0):
    deadline = time.monotonic() + timeout_s
    status = None
    while time.monotonic() < deadline:
        status = client.get(f"/api/courses/{course_id}/pipeline/status").json()
        if not status["active"]:
            return status
        time.sleep(0.02)
    raise AssertionError(f"pipeline still active after {timeout_s}s: {status}")


def test_pipeline_run_with_stages_filter_only_runs_requested_stages(client, db_session_factory):
    course_id = _add_course(db_session_factory)

    resp = client.post(
        f"/api/courses/{course_id}/pipeline/run", json={"stages": ["assemble"]}, headers=CSRF_HEADERS,
    )
    assert resp.status_code == 200

    status = _wait_for_pipeline_idle(client, course_id)
    assert [s["stage"] for s in status["stages"]] == ["assemble"]
    assert status["stages"][0]["status"] == "complete"


def test_pipeline_status_shape(client, db_session_factory):
    course_id = _add_course(db_session_factory)
    resp = client.get(f"/api/courses/{course_id}/pipeline/status")
    assert resp.status_code == 200
    assert resp.json() == {"active": False, "stages": []}


def test_dry_run_requires_csrf_header(client, db_session_factory):
    course_id = _add_course(db_session_factory)
    resp = client.post(f"/api/courses/{course_id}/pipeline/dry-run")
    assert resp.status_code == 403


# --------------------------------------------------------------------------
# (5) dry-run: counts from DB state, no backend calls
# --------------------------------------------------------------------------


class _CountingBackend:
    def __init__(self, inner):
        self._inner = inner
        self.calls = 0

    def structured_call(self, schema, *, system, user, tier):
        self.calls += 1
        return self._inner.structured_call(schema, system=system, user=user, tier=tier)

    def model_for_tier(self, tier):
        return self._inner.model_for_tier(tier)


def test_dry_run_counts_match_db_state_with_no_backend_calls(client, app, db_session_factory):
    course_id = _add_course(db_session_factory)
    counting = _CountingBackend(app.state.runner.backend)
    app.state.runner.backend = counting

    # summarize: one fetched-with-sha256 (needs extract) + one already
    # extracted-with-no-summary.
    _add_material(
        db_session_factory, course_id, kind="document", title="Fetched", mime="application/pdf",
        sha256="a" * 64, size_bytes=10, status="fetched",
    )
    _add_material(
        db_session_factory, course_id, kind="document", title="Extracted", mime="text/plain",
        sha256="b" * 64, size_bytes=10, status="extracted",
    )
    # taxonomy: any summarized material triggers exactly one call.
    # classify: this summarized material has no material_topics row at the
    # course's current taxonomy version (0), so it counts too.
    _add_material(
        db_session_factory, course_id, kind="document", title="Summarized", mime="text/plain",
        sha256="c" * 64, size_bytes=10, status="summarized", summary="s",
    )

    resp = client.post(f"/api/courses/{course_id}/pipeline/dry-run", headers=CSRF_HEADERS)
    assert resp.status_code == 200
    body = resp.json()

    assert body["byStage"]["summarize"]["calls"] == 2
    assert body["byStage"]["taxonomy"]["calls"] == 1
    assert body["byStage"]["classify"]["calls"] == 1
    for stage in ("summarize", "taxonomy", "classify"):
        assert body["byStage"][stage]["estCostUsd"] > 0
    assert body["totalEstCostUsd"] == pytest.approx(
        sum(body["byStage"][s]["estCostUsd"] for s in ("summarize", "taxonomy", "classify"))
    )
    assert body["totalEstCostUsd"] > 0

    assert counting.calls == 0  # a dry run must never touch the LLM


def test_dry_run_zero_calls_for_a_fresh_course(client, db_session_factory):
    course_id = _add_course(db_session_factory)
    resp = client.post(f"/api/courses/{course_id}/pipeline/dry-run", headers=CSRF_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["byStage"]["summarize"]["calls"] == 0
    assert body["byStage"]["taxonomy"]["calls"] == 0
    assert body["byStage"]["classify"]["calls"] == 0
    assert body["totalEstCostUsd"] == 0


def test_dry_run_unknown_course_404(client):
    resp = client.post("/api/courses/999999/pipeline/dry-run", headers=CSRF_HEADERS)
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# (6) SSE: /api/events over a real socket (TestClient/ASGITransport buffer
# the whole streaming response until the ASGI call completes, which never
# happens for an endpoint that streams forever -- so this test runs a real
# uvicorn server on a loopback port instead).
# --------------------------------------------------------------------------


def test_sse_events_stream_shows_a_triggered_run(app):
    with app.state.session_factory() as session:
        course = Course(d2l_org_unit_id=1, tenant_origin="school.d2l.com", name="Intro to CS")
        session.add(course)
        session.commit()
        course_id = course.id

    async def scenario():
        server_config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        server = uvicorn.Server(server_config)
        server_task = asyncio.create_task(server.serve())
        try:
            while not server.started:
                await asyncio.sleep(0.01)
            port = server.servers[0].sockets[0].getsockname()[1]

            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http_client:
                events: list[dict] = []

                async def read_stream():
                    async with http_client.stream("GET", "/api/events") as response:
                        assert response.status_code == 200
                        assert response.headers["content-type"].startswith("text/event-stream")
                        current_data = None
                        async for line in response.aiter_lines():
                            if line.startswith("data:"):
                                current_data = line[len("data:") :].strip()
                            elif line == "" and current_data is not None:
                                event = json.loads(current_data)
                                events.append(event)
                                current_data = None
                                if event.get("status") == "run-finished":
                                    return

                read_task = asyncio.create_task(read_stream())
                await asyncio.sleep(0.3)  # let the SSE subscription land before triggering
                assert app.state.event_bus.subscriber_count() == 1

                run_resp = await http_client.post(
                    f"/api/courses/{course_id}/pipeline/run", json={}, headers=CSRF_HEADERS,
                )
                assert run_resp.status_code == 200

                await asyncio.wait_for(read_task, timeout=10)
                # `read_stream` returned, closing the `async with
                # http_client.stream(...)` block -- the client disconnect
                # this produces should reach the server-side generator's
                # `finally: event_bus.unsubscribe(queue)` (api/events.py).
                # Give the server a moment to notice before checking.
                for _ in range(50):
                    if app.state.event_bus.subscriber_count() == 0:
                        break
                    await asyncio.sleep(0.05)
                subscriber_count_after_disconnect = app.state.event_bus.subscriber_count()
                return events, subscriber_count_after_disconnect
        finally:
            server.should_exit = True
            await server_task

    events, subscriber_count_after_disconnect = asyncio.run(scenario())

    assert events, "expected at least one event on the SSE stream"
    for event in events:
        assert event["type"] == "pipeline"
        assert set(event) >= {"type", "courseId", "runToken", "stage", "status"}
        assert event["courseId"] == course_id

    assert events[0]["status"] == "run-started"
    assert events[-1]["status"] == "run-finished"
    assert subscriber_count_after_disconnect == 0  # the disconnect unsubscribed cleanly
