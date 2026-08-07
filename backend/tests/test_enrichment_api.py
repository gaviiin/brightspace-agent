"""Tests for the M3.2 enrichment API (api/enrichment.py): the topic
enrichment read model, the topic/course enrich-run endpoints (CSRF + active
-run guard), the keep/dismiss feedback endpoint (domain_reputation), and the
no-backend-calls dry-run estimate. Against a real (mocked-LLM/mocked-web)
FastAPI app -- no network access, no API key.
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from brightspace_agent.db.models import Course, DomainReputation, EnrichmentResource, Material, MaterialTopic, Topic

CSRF_HEADERS = {"X-BSA-Request": "1"}


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BSA_DATA_DIR", str(tmp_path))
    # Forces MockBackend/MockWebBackend regardless of the host environment --
    # this module exercises the runner end to end via HTTP.
    monkeypatch.setenv("BSA_MOCK_LLM", "1")
    return tmp_path


@pytest.fixture
def app(data_dir):
    from brightspace_agent.main import create_app

    return create_app()


@pytest.fixture
def client(app):
    # `with` (not a bare TestClient(app)) so the same event-loop portal
    # stays alive across calls within one test -- required for the
    # active-run-guard test, which relies on the background enrichment task
    # surviving between two sequential requests (see test_frontend_api.py's
    # client fixture docstring for the full explanation).
    with TestClient(app, base_url="http://127.0.0.1:8731") as test_client:
        yield test_client


@pytest.fixture
def db_session_factory(app):
    return app.state.session_factory


def _add_course(db_session_factory, *, org_unit_id=1, name="Data Structures", code="CS 2110") -> int:
    with db_session_factory() as session:
        course = Course(
            d2l_org_unit_id=org_unit_id, tenant_origin="school.d2l.com", name=name, code=code, taxonomy_version=1,
        )
        session.add(course)
        session.commit()
        return course.id


def _add_topic(db_session_factory, course_id, *, version=1, slug="bfs", name="Breadth-First Search", order_index=0) -> int:
    with db_session_factory() as session:
        topic = Topic(
            course_id=course_id, taxonomy_version=version, slug=slug, name=name,
            description=f"{name} description.", order_index=order_index,
        )
        session.add(topic)
        session.commit()
        return topic.id


def _attach_material(db_session_factory, course_id, topic_id, *, version=1, title, summary):
    with db_session_factory() as session:
        material = Material(
            course_id=course_id, kind="slides", title=title, mime="text/plain",
            sha256=f"sha-{title.lower().replace(' ', '-')}", summary=summary, status="summarized",
        )
        session.add(material)
        session.flush()
        session.add(
            MaterialTopic(
                material_id=material.id, topic_id=topic_id, taxonomy_version=version,
                confidence=0.9, rationale="core material", method="llm",
            )
        )
        session.commit()


def _add_enrichment_resource(db_session_factory, topic_id, *, url, status="suggested", rank=1) -> int:
    with db_session_factory() as session:
        row = EnrichmentResource(
            topic_id=topic_id, url=url, title="A resource", resource_type="article", intent="alternative_explanation",
            rationale="because", scores_json='{"relevance": 0.8}', verification_json='{"ok": true}',
            rank=rank, status=status,
        )
        session.add(row)
        session.commit()
        return row.id


class _BlockingBackend:
    """Wraps an `LLMBackend`, blocking every `structured_call` until
    `release` is set. Used to make the active-run-guard tests deterministic:
    a MockBackend/MockWebBackend-driven enrichment run for a tiny,
    material-less topic can complete within the several event-loop turns a
    single TestClient request round-trip takes, racing (and sometimes
    losing) against a same-test follow-up request meant to observe it still
    active. Blocking the planner's very first call holds the run open for
    as long as the test needs."""

    def __init__(self, inner, release: threading.Event) -> None:
        self._inner = inner
        self._release = release

    def structured_call(self, schema, *, system, user, tier):
        self._release.wait(timeout=5)
        return self._inner.structured_call(schema, system=system, user=user, tier=tier)

    def model_for_tier(self, tier):
        return self._inner.model_for_tier(tier)


def _wait_for_enrichment_idle(client: TestClient, topic_id: int, *, timeout_s: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout_s
    status = None
    while time.monotonic() < deadline:
        status = client.get(f"/api/topics/{topic_id}/enrich/status").json()
        if not status["active"]:
            return status
        time.sleep(0.02)
    raise AssertionError(f"enrichment still active after {timeout_s}s: {status}")


# --------------------------------------------------------------------------
# (1) GET /api/topics/{topicId}/enrichment: shape + 404
# --------------------------------------------------------------------------


def test_get_topic_enrichment_unknown_topic_404(client):
    resp = client.get("/api/topics/999999/enrichment")
    assert resp.status_code == 404


def test_get_topic_enrichment_empty_before_any_run(client, db_session_factory):
    course_id = _add_course(db_session_factory)
    topic_id = _add_topic(db_session_factory, course_id)

    resp = client.get(f"/api/topics/{topic_id}/enrichment")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "topicId": topic_id,
        "resources": [],
        "meta": {"suggested": 0, "kept": 0, "dismissed": 0},
    }


def test_get_topic_enrichment_shape_after_a_real_run(client, db_session_factory):
    course_id = _add_course(db_session_factory)
    topic_id = _add_topic(db_session_factory, course_id)
    _attach_material(
        db_session_factory, course_id, topic_id,
        title="Lecture 7 BFS", summary="Covers BFS on unweighted graphs, queue frontier, shortest paths.",
    )

    run_resp = client.post(f"/api/topics/{topic_id}/enrich", headers=CSRF_HEADERS)
    assert run_resp.status_code == 200
    assert isinstance(run_resp.json()["runToken"], int)
    _wait_for_enrichment_idle(client, topic_id)

    resp = client.get(f"/api/topics/{topic_id}/enrichment")
    assert resp.status_code == 200
    body = resp.json()
    assert body["topicId"] == topic_id
    assert body["resources"]  # the mock backend produced at least one
    for resource in body["resources"]:
        assert set(resource) == {
            "id", "url", "title", "resourceType", "intent", "rationale", "scores", "verification", "rank",
            "shared", "status",
        }
        assert resource["status"] == "suggested"
        assert isinstance(resource["scores"], dict)
        assert isinstance(resource["verification"], dict)
        assert resource["shared"] is False
    assert body["meta"] == {"suggested": len(body["resources"]), "kept": 0, "dismissed": 0}


# --------------------------------------------------------------------------
# (2) POST /api/topics/{topicId}/enrich: CSRF, 404, active -> 409
# --------------------------------------------------------------------------


def test_post_topic_enrich_requires_csrf_header(client, db_session_factory):
    course_id = _add_course(db_session_factory)
    topic_id = _add_topic(db_session_factory, course_id)

    no_header = client.post(f"/api/topics/{topic_id}/enrich")
    assert no_header.status_code == 403

    with_header = client.post(f"/api/topics/{topic_id}/enrich", headers=CSRF_HEADERS)
    assert with_header.status_code == 200
    assert isinstance(with_header.json()["runToken"], int)


def test_post_topic_enrich_unknown_topic_404(client):
    resp = client.post("/api/topics/999999/enrich", headers=CSRF_HEADERS)
    assert resp.status_code == 404


def test_post_topic_enrich_while_active_is_409(client, app, db_session_factory):
    course_id = _add_course(db_session_factory)
    topic_id = _add_topic(db_session_factory, course_id)

    release = threading.Event()
    app.state.runner.backend = _BlockingBackend(app.state.runner.backend, release)

    first = client.post(f"/api/topics/{topic_id}/enrich", headers=CSRF_HEADERS)
    assert first.status_code == 200

    second = client.post(f"/api/topics/{topic_id}/enrich", headers=CSRF_HEADERS)
    assert second.status_code == 409

    release.set()
    _wait_for_enrichment_idle(client, topic_id)


def test_post_topic_enrich_stale_taxonomy_version_404(client, db_session_factory):
    course_id = _add_course(db_session_factory)
    old_topic_id = _add_topic(db_session_factory, course_id, version=1, slug="old", name="Old Topic")
    with db_session_factory() as session:
        session.get(Course, course_id).taxonomy_version = 2
        session.commit()

    resp = client.post(f"/api/topics/{old_topic_id}/enrich", headers=CSRF_HEADERS)
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# (3) PUT /api/enrichment/{resourceId}: keep/dismiss feedback, no double count
# --------------------------------------------------------------------------


def test_put_enrichment_status_unknown_id_404(client):
    resp = client.put("/api/enrichment/999999", json={"status": "kept"}, headers=CSRF_HEADERS)
    assert resp.status_code == 404


def test_put_enrichment_status_requires_csrf_header(client, db_session_factory):
    course_id = _add_course(db_session_factory)
    topic_id = _add_topic(db_session_factory, course_id)
    resource_id = _add_enrichment_resource(db_session_factory, topic_id, url="https://ocw.mit.edu/notes")

    resp = client.put(f"/api/enrichment/{resource_id}", json={"status": "kept"})
    assert resp.status_code == 403


def test_put_keep_updates_status_and_increments_kept_count_once(client, db_session_factory):
    course_id = _add_course(db_session_factory)
    topic_id = _add_topic(db_session_factory, course_id)
    resource_id = _add_enrichment_resource(db_session_factory, topic_id, url="https://ocw.mit.edu/notes")

    resp = client.put(f"/api/enrichment/{resource_id}", json={"status": "kept"}, headers=CSRF_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == resource_id
    assert body["status"] == "kept"

    with db_session_factory() as session:
        rep = session.get(DomainReputation, "ocw.mit.edu")
        assert rep.kept_count == 1
        assert rep.dismissed_count == 0

    # Setting the SAME status again must not double-count.
    resp2 = client.put(f"/api/enrichment/{resource_id}", json={"status": "kept"}, headers=CSRF_HEADERS)
    assert resp2.status_code == 200
    with db_session_factory() as session:
        rep = session.get(DomainReputation, "ocw.mit.edu")
        assert rep.kept_count == 1  # unchanged


def test_put_dismiss_updates_status_and_increments_dismissed_count(client, db_session_factory):
    course_id = _add_course(db_session_factory)
    topic_id = _add_topic(db_session_factory, course_id)
    resource_id = _add_enrichment_resource(db_session_factory, topic_id, url="https://notes.cmu.edu/x")

    resp = client.put(f"/api/enrichment/{resource_id}", json={"status": "dismissed"}, headers=CSRF_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["status"] == "dismissed"

    with db_session_factory() as session:
        rep = session.get(DomainReputation, "notes.cmu.edu")
        assert rep.dismissed_count == 1
        assert rep.kept_count == 0


def test_put_status_back_to_suggested_does_not_record_feedback(client, db_session_factory):
    course_id = _add_course(db_session_factory)
    topic_id = _add_topic(db_session_factory, course_id)
    resource_id = _add_enrichment_resource(db_session_factory, topic_id, url="https://ocw.mit.edu/y", status="kept")

    resp = client.put(f"/api/enrichment/{resource_id}", json={"status": "suggested"}, headers=CSRF_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["status"] == "suggested"

    with db_session_factory() as session:
        rep = session.get(DomainReputation, "ocw.mit.edu")
        assert rep is None  # never kept/dismissed via this endpoint -> no row


# --------------------------------------------------------------------------
# (4) POST /api/courses/{courseId}/enrich: batch
# --------------------------------------------------------------------------


def test_post_course_enrich_requires_csrf_and_enriches_every_topic(client, db_session_factory):
    course_id = _add_course(db_session_factory)
    first = _add_topic(db_session_factory, course_id, slug="bfs", name="Breadth-First Search")
    second = _add_topic(db_session_factory, course_id, slug="dfs", name="Depth-First Search", order_index=1)

    no_header = client.post(f"/api/courses/{course_id}/enrich")
    assert no_header.status_code == 403

    resp = client.post(f"/api/courses/{course_id}/enrich", headers=CSRF_HEADERS)
    assert resp.status_code == 200
    assert isinstance(resp.json()["runToken"], int)

    _wait_for_enrichment_idle(client, first)

    assert client.get(f"/api/topics/{first}/enrichment").json()["resources"]
    assert client.get(f"/api/topics/{second}/enrichment").json()["resources"]


def test_post_course_enrich_while_active_is_409(client, app, db_session_factory):
    course_id = _add_course(db_session_factory)
    topic_id = _add_topic(db_session_factory, course_id)

    release = threading.Event()
    app.state.runner.backend = _BlockingBackend(app.state.runner.backend, release)

    first = client.post(f"/api/courses/{course_id}/enrich", headers=CSRF_HEADERS)
    assert first.status_code == 200

    second = client.post(f"/api/courses/{course_id}/enrich", headers=CSRF_HEADERS)
    assert second.status_code == 409

    release.set()
    _wait_for_enrichment_idle(client, topic_id)


def test_post_course_enrich_unknown_course_404(client):
    resp = client.post("/api/courses/999999/enrich", headers=CSRF_HEADERS)
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# (5) GET /api/courses/{courseId}/enrich/dry-run: DB-derived counts, no calls
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


class _CountingWebBackend:
    def __init__(self, inner):
        self._inner = inner
        self.calls = 0

    def find(self, *, system, user, tier):
        self.calls += 1
        return self._inner.find(system=system, user=user, tier=tier)

    def verify(self, *, system, user, tier):
        self.calls += 1
        return self._inner.verify(system=system, user=user, tier=tier)

    def model_for_tier(self, tier):
        return self._inner.model_for_tier(tier)


def test_dry_run_counts_match_db_state_no_backend_calls(client, app, db_session_factory):
    course_id = _add_course(db_session_factory)
    needs_enrichment = _add_topic(db_session_factory, course_id, slug="bfs", name="Breadth-First Search")
    already_enriched = _add_topic(db_session_factory, course_id, slug="dfs", name="Depth-First Search", order_index=1)
    _add_enrichment_resource(db_session_factory, already_enriched, url="https://ocw.mit.edu/dfs", status="kept")

    counting_backend = _CountingBackend(app.state.runner.backend)
    app.state.runner.backend = counting_backend
    counting_web = _CountingWebBackend(app.state.runner.web_backend)
    app.state.runner.web_backend = counting_web

    resp = client.get(f"/api/courses/{course_id}/enrich/dry-run")
    assert resp.status_code == 200
    body = resp.json()

    assert body["topicsNeedingEnrichment"] == 1  # only the un-enriched one
    assert body["callsPerTopic"] > 0
    assert body["estCostPerTopicUsd"] > 0
    assert body["totalEstCostUsd"] == pytest.approx(body["estCostPerTopicUsd"] * 1)
    assert body["totalEstCostUsd"] > 0

    assert counting_backend.calls == 0
    assert counting_web.calls == 0
    assert needs_enrichment  # sanity: fixture used


def test_dry_run_includes_the_per_search_web_search_fee(client, db_session_factory):
    # web_search is billed per search (~$0.01) on top of tokens, and at up to
    # max_uses searches per finder it dominates the token cost -- an estimate
    # that hides it under-states a real run by an order of magnitude.
    from brightspace_agent.agents.llm import WEB_SEARCH_COST_PER_SEARCH_USD
    from brightspace_agent.agents.web import web_search_max_uses
    from brightspace_agent.api.enrichment import _ASSUMED_INTENTS_PER_TOPIC

    course_id = _add_course(db_session_factory)
    _add_topic(db_session_factory, course_id)

    body = client.get(f"/api/courses/{course_id}/enrich/dry-run").json()

    expected_searches = _ASSUMED_INTENTS_PER_TOPIC * web_search_max_uses("smart")
    assert body["webSearchesPerTopic"] == expected_searches
    search_cost = expected_searches * WEB_SEARCH_COST_PER_SEARCH_USD
    assert search_cost > 0
    assert body["estCostPerTopicUsd"] > search_cost  # tokens on top of the fees
    assert body["totalEstCostUsd"] == pytest.approx(body["estCostPerTopicUsd"])


def test_dry_run_zero_for_a_fully_enriched_course(client, db_session_factory):
    course_id = _add_course(db_session_factory)
    topic_id = _add_topic(db_session_factory, course_id)
    _add_enrichment_resource(db_session_factory, topic_id, url="https://ocw.mit.edu/x", status="suggested")

    resp = client.get(f"/api/courses/{course_id}/enrich/dry-run")
    assert resp.status_code == 200
    body = resp.json()
    assert body["topicsNeedingEnrichment"] == 0
    assert body["totalEstCostUsd"] == 0


def test_dry_run_unknown_course_404(client):
    resp = client.get("/api/courses/999999/enrich/dry-run")
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# GET /api/topics/{topicId}/enrich/status
# --------------------------------------------------------------------------


def test_topic_enrich_status_shape_and_404(client, db_session_factory):
    course_id = _add_course(db_session_factory)
    topic_id = _add_topic(db_session_factory, course_id)

    resp = client.get(f"/api/topics/{topic_id}/enrich/status")
    assert resp.status_code == 200
    assert resp.json() == {"active": False, "lastRun": None}

    assert client.get("/api/topics/999999/enrich/status").status_code == 404
