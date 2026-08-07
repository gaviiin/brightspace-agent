"""Tests for `PipelineRunner.start_enrichment`/`enrichment_status`
(pipeline/runner.py): the on-demand enrichment background path -- the
`pipeline_runs` (stage='enrich') lifecycle, the shared active-run guard, SSE
events (`type: "enrichment"`), per-topic isolation in the batch path, and
orphan reconciliation. Against MockBackend/MockWebBackend or small stub web
backends directly -- no HTTP layer, no network access, no API key.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import select

from brightspace_agent.agents.llm import MockBackend
from brightspace_agent.agents.web import MockWebBackend
from brightspace_agent.config import Settings
from brightspace_agent.db.models import Course, EnrichmentResource, Material, MaterialTopic, PipelineRun, Topic
from brightspace_agent.db.session import init_db
from brightspace_agent.ingest.store import BlobStore
from brightspace_agent.pipeline import runner as runner_module
from brightspace_agent.pipeline.runner import PipelineRunner, RunActiveError, TopicNotFoundError


# --------------------------------------------------------------------------
# Fixtures + seeding
# --------------------------------------------------------------------------


@pytest.fixture
def session_factory(tmp_path):
    return init_db(tmp_path / "brightspace.db")[1]


@pytest.fixture
def blob_store(tmp_path):
    return BlobStore(blobs_dir=tmp_path / "blobs", text_dir=tmp_path / "text")


@pytest.fixture
def course_id(session_factory):
    with session_factory() as session:
        course = Course(
            d2l_org_unit_id=1, tenant_origin="school.d2l.com", name="Data Structures", code="CS 2110",
            taxonomy_version=1,
        )
        session.add(course)
        session.commit()
        return course.id


def _seed_topic(session_factory, course_id, *, version=1, slug, name, order_index=0) -> int:
    with session_factory() as session:
        topic = Topic(
            course_id=course_id, taxonomy_version=version, slug=slug, name=name,
            description=f"{name} description.", order_index=order_index,
        )
        session.add(topic)
        session.commit()
        return topic.id


def _attach_material(session_factory, course_id, topic_id, *, version=1, title, summary):
    with session_factory() as session:
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
        return material.id


@pytest.fixture
def topic_id(session_factory, course_id):
    tid = _seed_topic(session_factory, course_id, slug="breadth-first-search", name="Breadth-First Search")
    _attach_material(
        session_factory, course_id, tid,
        title="Lecture 7 BFS", summary="Covers BFS on unweighted graphs, queue frontier, shortest paths.",
    )
    return tid


def _make_runner(session_factory, blob_store, *, backend=None, web_backend=None) -> PipelineRunner:
    settings = Settings(max_cost_usd_per_run=5.0)
    return PipelineRunner(
        session_factory, blob_store, backend or MockBackend(), settings, web_backend=web_backend or MockWebBackend()
    )


def _pipeline_runs(session_factory, course_id: int) -> list[PipelineRun]:
    with session_factory() as session:
        rows = list(
            session.execute(
                select(PipelineRun).where(PipelineRun.course_id == course_id).order_by(PipelineRun.id)
            ).scalars().all()
        )
        for row in rows:
            session.expunge(row)
        return rows


def _enrichment_rows(session_factory, topic_id: int) -> list[EnrichmentResource]:
    with session_factory() as session:
        rows = list(
            session.execute(
                select(EnrichmentResource).where(EnrichmentResource.topic_id == topic_id)
            ).scalars().all()
        )
        for row in rows:
            session.expunge(row)
        return rows


# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------


class _AlwaysRaisingWeb:
    """Every call raises -- for exercising the "the run itself fails" path
    of a single-topic enrichment (contrast with `_RaisingForTopicWeb`, which
    isolates one topic within a course batch)."""

    def find(self, *, system, user, tier):
        raise RuntimeError("web search exploded")

    def verify(self, *, system, user, tier):
        raise RuntimeError("should not be reached")

    def model_for_tier(self, tier):
        return f"mock-{tier}"


class _RaisingForTopicWeb:
    """Delegates to `inner`, but raises whenever the (finder) prompt mentions
    `boom_name` -- so run_enrich_stage's per-topic isolation can be exercised
    through the runner's batch path."""

    def __init__(self, inner, boom_name) -> None:
        self._inner = inner
        self._boom = boom_name

    def find(self, *, system, user, tier):
        if self._boom in user:
            raise RuntimeError("web search exploded")
        return self._inner.find(system=system, user=user, tier=tier)

    def verify(self, *, system, user, tier):
        return self._inner.verify(system=system, user=user, tier=tier)

    def model_for_tier(self, tier):
        return self._inner.model_for_tier(tier)


# --------------------------------------------------------------------------
# (1) start_enrichment(topic_id) -> rows + pipeline_runs row complete
# --------------------------------------------------------------------------


def test_start_enrichment_topic_writes_resources_and_a_complete_run_row(session_factory, blob_store, course_id, topic_id):
    async def scenario():
        runner = _make_runner(session_factory, blob_store)
        run_token = runner.start_enrichment(course_id, topic_id=topic_id)
        await runner.wait_enrichment_idle(course_id)
        return run_token

    run_token = asyncio.run(scenario())
    assert isinstance(run_token, int)

    rows = _enrichment_rows(session_factory, topic_id)
    assert rows  # something was written

    runs = _pipeline_runs(session_factory, course_id)
    assert [r.stage for r in runs] == ["enrich"]
    assert runs[0].status == "complete"
    assert runs[0].finished_at is not None
    usage = json.loads(runs[0].usage_json)
    assert set(usage) == {"input_tokens", "output_tokens", "est_cost_usd"}


# --------------------------------------------------------------------------
# (2) start_enrichment(course) batch -> every current-version topic enriched
# --------------------------------------------------------------------------


def test_start_enrichment_course_batch_enriches_every_current_version_topic(session_factory, blob_store, course_id):
    first = _seed_topic(session_factory, course_id, slug="breadth-first-search", name="Breadth-First Search")
    second = _seed_topic(session_factory, course_id, slug="depth-first-search", name="Depth-First Search", order_index=1)

    async def scenario():
        runner = _make_runner(session_factory, blob_store)
        runner.start_enrichment(course_id)
        await runner.wait_enrichment_idle(course_id)

    asyncio.run(scenario())

    assert _enrichment_rows(session_factory, first)
    assert _enrichment_rows(session_factory, second)

    runs = _pipeline_runs(session_factory, course_id)
    assert [r.stage for r in runs] == ["enrich"]
    assert runs[0].status == "complete"


# --------------------------------------------------------------------------
# (3) active-run guard: shared between pipeline and enrichment
# --------------------------------------------------------------------------


def test_second_enrichment_start_while_active_raises(session_factory, blob_store, course_id, topic_id):
    async def scenario():
        runner = _make_runner(session_factory, blob_store)
        runner.start_enrichment(course_id, topic_id=topic_id)
        with pytest.raises(RunActiveError):
            runner.start_enrichment(course_id, topic_id=topic_id)
        await runner.wait_enrichment_idle(course_id)
        assert runner.is_active(course_id) is False

        second_token = runner.start_enrichment(course_id, topic_id=topic_id)
        await runner.wait_enrichment_idle(course_id)
        return second_token

    token = asyncio.run(scenario())
    assert isinstance(token, int)


def test_pipeline_and_enrichment_runs_share_the_active_guard(session_factory, blob_store, course_id, topic_id):
    """A pipeline run and an enrichment run must not run concurrently for
    the same course -- both branches of the shared `_active` guard."""

    async def scenario():
        runner = _make_runner(session_factory, blob_store)
        runner.start(course_id, stages=["assemble"])
        with pytest.raises(RunActiveError):
            runner.start_enrichment(course_id, topic_id=topic_id)
        await runner.wait_idle(course_id)

        runner.start_enrichment(course_id, topic_id=topic_id)
        with pytest.raises(RunActiveError):
            runner.start(course_id, stages=["assemble"])
        await runner.wait_enrichment_idle(course_id)

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# (4) SSE: run-started then complete, type == "enrichment"
# --------------------------------------------------------------------------


def test_enrichment_events_sequence_is_run_started_then_complete(session_factory, blob_store, course_id, topic_id):
    async def scenario():
        runner = _make_runner(session_factory, blob_store)
        queue = runner.event_bus.subscribe()

        run_token = runner.start_enrichment(course_id, topic_id=topic_id)

        events = []
        while True:
            event = await asyncio.wait_for(queue.get(), timeout=5)
            events.append(event)
            if event["status"] in ("complete", "failed", "aborted"):
                break

        runner.event_bus.unsubscribe(queue)
        return events, run_token

    events, run_token = asyncio.run(scenario())

    assert [e["status"] for e in events] == ["run-started", "complete"]
    for event in events:
        assert event["type"] == "enrichment"
        assert event["courseId"] == course_id
        assert event["runToken"] == run_token
        assert event["topicId"] == topic_id
    assert "stats" in events[-1]


def test_enrichment_events_for_a_course_batch_have_no_topic_id(session_factory, blob_store, course_id, topic_id):
    async def scenario():
        runner = _make_runner(session_factory, blob_store)
        queue = runner.event_bus.subscribe()

        runner.start_enrichment(course_id)

        events = []
        while True:
            event = await asyncio.wait_for(queue.get(), timeout=5)
            events.append(event)
            if event["status"] in ("complete", "failed", "aborted"):
                break

        runner.event_bus.unsubscribe(queue)
        return events

    events = asyncio.run(scenario())
    assert [e["status"] for e in events] == ["run-started", "complete"]
    for event in events:
        assert "topicId" not in event


# --------------------------------------------------------------------------
# (5) a backend raising: single-topic run fails cleanly; a batch isolates it
# --------------------------------------------------------------------------


def test_topic_backend_raising_marks_the_run_failed_not_stuck_running(session_factory, blob_store, course_id, topic_id):
    async def scenario():
        runner = _make_runner(session_factory, blob_store, web_backend=_AlwaysRaisingWeb())
        runner.start_enrichment(course_id, topic_id=topic_id)
        await runner.wait_enrichment_idle(course_id)

    asyncio.run(scenario())

    runs = _pipeline_runs(session_factory, course_id)
    assert len(runs) == 1
    assert runs[0].status == "failed"
    assert runs[0].status != "running"
    assert runs[0].finished_at is not None
    assert runs[0].error


def test_course_batch_isolates_one_failing_topic_and_still_completes(session_factory, blob_store, course_id):
    good = _seed_topic(session_factory, course_id, slug="breadth-first-search", name="Breadth-First Search")
    _seed_topic(session_factory, course_id, slug="boom-topic", name="Boom Topic", order_index=1)

    async def scenario():
        runner = _make_runner(
            session_factory, blob_store, web_backend=_RaisingForTopicWeb(MockWebBackend(), boom_name="Boom Topic")
        )
        runner.start_enrichment(course_id)
        await runner.wait_enrichment_idle(course_id)

    asyncio.run(scenario())

    runs = _pipeline_runs(session_factory, course_id)
    assert len(runs) == 1
    assert runs[0].status == "complete"  # isolated: the batch itself did not fail
    assert _enrichment_rows(session_factory, good)  # the healthy topic still enriched


# --------------------------------------------------------------------------
# (6) orphan reconciliation covers stage='enrich'
# --------------------------------------------------------------------------


def test_on_start_failure_does_not_leave_an_enrich_row_stuck_running(session_factory, blob_store, course_id, topic_id, monkeypatch):
    real_on_start = runner_module._EnrichmentRunHooks.on_start

    def flaky_on_start(self):
        real_on_start(self)  # the row is genuinely created + committed
        raise RuntimeError("simulated failure right after on_start committed its row")

    monkeypatch.setattr(runner_module._EnrichmentRunHooks, "on_start", flaky_on_start)

    async def scenario():
        runner = _make_runner(session_factory, blob_store)
        runner.start_enrichment(course_id, topic_id=topic_id)
        await runner.wait_enrichment_idle(course_id)

    asyncio.run(scenario())

    runs = _pipeline_runs(session_factory, course_id)
    assert len(runs) == 1
    assert runs[0].status == "failed"
    assert runs[0].error == "orphaned"
    assert runs[0].finished_at is not None


def test_startup_sweep_covers_stale_enrich_rows_too(session_factory, blob_store, course_id):
    with session_factory() as session:
        stale = PipelineRun(
            course_id=course_id, stage="enrich", status="running", started_at="2020-01-01T00:00:00+00:00",
        )
        session.add(stale)
        session.commit()
        stale_id = stale.id

    _make_runner(session_factory, blob_store)  # constructor runs the startup sweep

    with session_factory() as session:
        row = session.get(PipelineRun, stale_id)
        assert row.status == "failed"
        assert row.error == "orphaned-by-restart"


# --------------------------------------------------------------------------
# Topic validation: 404-able TopicNotFoundError
# --------------------------------------------------------------------------


def test_start_enrichment_unknown_topic_raises_topic_not_found(session_factory, blob_store, course_id):
    runner = _make_runner(session_factory, blob_store)
    with pytest.raises(TopicNotFoundError):
        runner.start_enrichment(course_id, topic_id=999999)
    assert runner.is_active(course_id) is False  # nothing left active behind the failed validation


def test_start_enrichment_topic_from_a_stale_taxonomy_version_raises_topic_not_found(
    session_factory, blob_store, course_id
):
    # An old-version topic id (course bumped to version 2, this topic is
    # still at version 1) must not be enrichable through this path.
    old_topic_id = _seed_topic(session_factory, course_id, version=1, slug="old-topic", name="Old Topic")
    with session_factory() as session:
        session.get(Course, course_id).taxonomy_version = 2
        session.commit()

    runner = _make_runner(session_factory, blob_store)
    with pytest.raises(TopicNotFoundError):
        runner.start_enrichment(course_id, topic_id=old_topic_id)


# --------------------------------------------------------------------------
# enrichment_status()
# --------------------------------------------------------------------------


def test_enrichment_status_reflects_active_state_and_last_run(session_factory, blob_store, course_id, topic_id):
    async def scenario():
        runner = _make_runner(session_factory, blob_store)
        before = runner.enrichment_status(course_id)

        runner.start_enrichment(course_id, topic_id=topic_id)
        during = runner.enrichment_status(course_id)  # no await yet -- still active

        await runner.wait_enrichment_idle(course_id)
        after = runner.enrichment_status(course_id)
        return before, during, after

    before, during, after = asyncio.run(scenario())

    assert before == {"active": False, "lastRun": None}
    assert during["active"] is True

    assert after["active"] is False
    assert after["lastRun"]["status"] == "complete"
    assert after["lastRun"]["finishedAt"] is not None
    assert after["lastRun"]["usage"] is not None
