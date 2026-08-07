"""Tests for `GET /api/courses/{id}/runs` -- the sync/pipeline run history
the frontend's Runs drawer reads. Read-only, no CSRF header needed."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from brightspace_agent.db.models import Course, PipelineRun, SyncRun


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BSA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BSA_MOCK_LLM", "1")
    return tmp_path


@pytest.fixture
def app(data_dir):
    from brightspace_agent.main import create_app

    return create_app()


@pytest.fixture
def client(app):
    with TestClient(app, base_url="http://127.0.0.1:8730") as test_client:
        yield test_client


@pytest.fixture
def db_session_factory(app):
    return app.state.session_factory


@pytest.fixture
def course_id(db_session_factory) -> int:
    with db_session_factory() as session:
        course = Course(d2l_org_unit_id=99, tenant_origin="school.d2l.com", name="Algorithms")
        session.add(course)
        session.commit()
        return course.id


def _add_sync_run(db_session_factory, course_id, *, status="complete", stats=None, started="2026-08-06T22:00:00+00:00") -> int:
    with db_session_factory() as session:
        run = SyncRun(
            course_id=course_id,
            source="extension",
            status=status,
            started_at=started,
            finished_at="2026-08-06T22:01:00+00:00",
            stats_json=json.dumps(stats) if isinstance(stats, dict) else stats,
        )
        session.add(run)
        session.commit()
        return run.id


def _add_pipeline_run(db_session_factory, course_id, *, stage="summarize", status="complete", usage=None) -> int:
    with db_session_factory() as session:
        run = PipelineRun(
            course_id=course_id,
            stage=stage,
            status=status,
            started_at="2026-08-06T23:00:00+00:00",
            finished_at="2026-08-06T23:05:00+00:00",
            usage_json=json.dumps(usage) if isinstance(usage, dict) else usage,
        )
        session.add(run)
        session.commit()
        return run.id


def test_runs_returns_sync_and_pipeline_history_newest_first(client, db_session_factory, course_id):
    first_sync = _add_sync_run(
        db_session_factory, course_id,
        stats={"files": 73, "bytes": 58487965, "notNeeded": 2, "extrasSkipped": 0, "errors": []},
    )
    second_sync = _add_sync_run(
        db_session_factory, course_id, status="failed",
        stats={"files": 0, "bytes": 0, "notNeeded": 0, "extrasSkipped": 0,
               "errors": [{"d2lTopicId": 111, "message": "Failed to fetch"}]},
    )
    pipeline = _add_pipeline_run(
        db_session_factory, course_id,
        usage={"input_tokens": 275792, "output_tokens": 9147, "est_cost_usd": 0.3215},
    )

    response = client.get(f"/api/courses/{course_id}/runs")

    assert response.status_code == 200
    body = response.json()
    assert [run["id"] for run in body["syncRuns"]] == [second_sync, first_sync]
    newest = body["syncRuns"][0]
    assert newest["status"] == "failed"
    assert newest["errorCount"] == 1
    assert newest["errors"] == [{"d2lTopicId": 111, "message": "Failed to fetch"}]
    oldest = body["syncRuns"][1]
    assert (oldest["files"], oldest["bytes"], oldest["notNeeded"]) == (73, 58487965, 2)
    assert oldest["errorCount"] == 0

    assert [run["id"] for run in body["pipelineRuns"]] == [pipeline]
    run = body["pipelineRuns"][0]
    assert run["stage"] == "summarize"
    assert (run["inputTokens"], run["outputTokens"]) == (275792, 9147)
    assert run["estCostUsd"] == pytest.approx(0.3215)
    assert run["error"] is None


def test_runs_caps_the_error_list_but_reports_the_full_count(client, db_session_factory, course_id):
    errors = [{"d2lTopicId": index, "message": "Failed to fetch"} for index in range(8)]
    _add_sync_run(db_session_factory, course_id, status="failed",
                  stats={"files": 0, "bytes": 0, "notNeeded": 0, "extrasSkipped": 0, "errors": errors})

    body = client.get(f"/api/courses/{course_id}/runs").json()

    run = body["syncRuns"][0]
    assert run["errorCount"] == 8
    assert len(run["errors"]) == 5
    assert run["errors"][0]["d2lTopicId"] == 0


def test_runs_is_fail_soft_on_malformed_json(client, db_session_factory, course_id):
    _add_sync_run(db_session_factory, course_id, stats="{not json")
    _add_sync_run(db_session_factory, course_id, stats=None)
    _add_pipeline_run(db_session_factory, course_id, usage="{not json")
    _add_pipeline_run(db_session_factory, course_id, usage=None)

    response = client.get(f"/api/courses/{course_id}/runs")

    assert response.status_code == 200
    body = response.json()
    for run in body["syncRuns"]:
        assert (run["files"], run["bytes"], run["errorCount"], run["errors"]) == (0, 0, 0, [])
    for run in body["pipelineRuns"]:
        assert (run["inputTokens"], run["outputTokens"], run["estCostUsd"]) == (0, 0, 0.0)


def test_runs_returns_only_the_last_ten_of_each(client, db_session_factory, course_id):
    for _ in range(12):
        _add_sync_run(db_session_factory, course_id, stats={"files": 1, "bytes": 1, "notNeeded": 0, "extrasSkipped": 0, "errors": []})
        _add_pipeline_run(db_session_factory, course_id, usage={"input_tokens": 1, "output_tokens": 1, "est_cost_usd": 0.0})

    body = client.get(f"/api/courses/{course_id}/runs").json()

    assert len(body["syncRuns"]) == 10
    assert len(body["pipelineRuns"]) == 10


def test_runs_scopes_to_the_requested_course(client, db_session_factory, course_id):
    with db_session_factory() as session:
        other = Course(d2l_org_unit_id=100, tenant_origin="school.d2l.com", name="Other")
        session.add(other)
        session.commit()
        other_id = other.id
    _add_sync_run(db_session_factory, other_id)
    mine = _add_sync_run(db_session_factory, course_id)

    body = client.get(f"/api/courses/{course_id}/runs").json()

    assert [run["id"] for run in body["syncRuns"]] == [mine]
    assert body["pipelineRuns"] == []


def test_runs_404_for_unknown_course(client):
    response = client.get("/api/courses/424242/runs")

    assert response.status_code == 404
