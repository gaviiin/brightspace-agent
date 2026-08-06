"""Tests for `PipelineRunner` (pipeline/runner.py): background execution,
pipeline_runs bookkeeping, the active-run guard, the event bus, and the
cost cap end to end. Against MockBackend/stub backends directly -- no
HTTP layer, no network access, no API key.
"""

from __future__ import annotations

import asyncio
import json

import fitz  # PyMuPDF
import pytest
from sqlalchemy import select

from brightspace_agent.agents.llm import MockBackend
from brightspace_agent.config import Settings
from brightspace_agent.db.models import Course, Material, PipelineRun
from brightspace_agent.db.session import init_db
from brightspace_agent.ingest.store import BlobStore
from brightspace_agent.pipeline.runner import PipelineRunner, RunActiveError


def _make_pdf_bytes(text: str) -> bytes:
    doc = fitz.open()
    try:
        page = doc.new_page()
        page.insert_text((72, 72), text)
        return doc.tobytes()
    finally:
        doc.close()


@pytest.fixture
def session_factory(tmp_path):
    return init_db(tmp_path / "brightspace.db")[1]


@pytest.fixture
def blob_store(tmp_path):
    return BlobStore(blobs_dir=tmp_path / "blobs", text_dir=tmp_path / "text")


@pytest.fixture
def course_id(session_factory):
    with session_factory() as session:
        course = Course(d2l_org_unit_id=1, tenant_origin="school.d2l.com", name="Intro to CS", code="CS100")
        session.add(course)
        session.commit()
        return course.id


def _add_fetched_pdf(session_factory, blob_store, course_id, *, title, text, kind="document") -> int:
    sha256, size = blob_store.put_bytes(_make_pdf_bytes(text))
    with session_factory() as session:
        material = Material(
            course_id=course_id, kind=kind, title=title, mime="application/pdf",
            sha256=sha256, size_bytes=size, status="fetched",
        )
        session.add(material)
        session.commit()
        return material.id


def _seed_summarizable_course(session_factory, blob_store, course_id, count: int = 3) -> list[int]:
    ids = [
        _add_fetched_pdf(
            session_factory, blob_store, course_id,
            title="Course Syllabus", text="CS100 syllabus. Week 1 intro. Week 2 loops.", kind="syllabus",
        )
    ]
    for i in range(count - 1):
        ids.append(
            _add_fetched_pdf(
                session_factory, blob_store, course_id,
                title=f"Lecture {i}", text=f"Distinct lecture body {i} about topic {i}.",
            )
        )
    return ids


def _make_runner(session_factory, blob_store, backend, *, max_cost_usd_per_run: float = 5.0) -> PipelineRunner:
    settings = Settings(max_cost_usd_per_run=max_cost_usd_per_run)
    return PipelineRunner(session_factory, blob_store, backend, settings)


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


class _FixedCostBackend:
    """Wraps a backend, reporting a fixed `est_cost_usd` per call."""

    def __init__(self, inner, est_cost_usd: float) -> None:
        self._inner = inner
        self._est_cost_usd = est_cost_usd
        self.calls = 0

    def structured_call(self, schema, *, system, user, tier):
        self.calls += 1
        parsed, usage = self._inner.structured_call(schema, system=system, user=user, tier=tier)
        usage = {**usage, "est_cost_usd": self._est_cost_usd}
        return parsed, usage

    def model_for_tier(self, tier):
        return self._inner.model_for_tier(tier)


# --------------------------------------------------------------------------
# (1) start -> await completion -> pipeline_runs rows
# --------------------------------------------------------------------------


def test_start_then_wait_produces_complete_pipeline_runs_rows_with_usage(session_factory, blob_store, course_id):
    _seed_summarizable_course(session_factory, blob_store, course_id)

    async def scenario():
        runner = _make_runner(session_factory, blob_store, MockBackend())
        run_token = runner.start(course_id)
        await runner.wait_idle(course_id)
        return run_token

    run_token = asyncio.run(scenario())
    assert isinstance(run_token, int)

    rows = _pipeline_runs(session_factory, course_id)
    assert [row.stage for row in rows] == ["summarize", "taxonomy", "classify", "assemble"]
    assert all(row.status == "complete" for row in rows)
    assert all(row.finished_at for row in rows)
    for row in rows:
        usage = json.loads(row.usage_json)
        assert set(usage) == {"input_tokens", "output_tokens", "est_cost_usd"}

    with session_factory() as session:
        course = session.get(Course, course_id)
        assert course.taxonomy_version == 1


def test_status_reflects_the_latest_run(session_factory, blob_store, course_id):
    _seed_summarizable_course(session_factory, blob_store, course_id)

    async def scenario():
        runner = _make_runner(session_factory, blob_store, MockBackend())
        runner.start(course_id)
        await runner.wait_idle(course_id)
        return runner

    runner = asyncio.run(scenario())
    status = runner.status(course_id)
    assert status["active"] is False
    assert [s["stage"] for s in status["stages"]] == ["summarize", "taxonomy", "classify", "assemble"]
    assert all(s["status"] == "complete" for s in status["stages"])
    assert all(s["usage"] is not None for s in status["stages"])


# --------------------------------------------------------------------------
# (2) active-run guard
# --------------------------------------------------------------------------


def test_second_start_while_active_raises_then_allows_a_new_run_after_completion(
    session_factory, blob_store, course_id
):
    _seed_summarizable_course(session_factory, blob_store, course_id)

    async def scenario():
        runner = _make_runner(session_factory, blob_store, MockBackend())

        first_token = runner.start(course_id)
        # No `await` happened yet -- the background task has not had a
        # chance to run, so the run is still guaranteed active here.
        with pytest.raises(RunActiveError):
            runner.start(course_id)

        await runner.wait_idle(course_id)
        assert runner.status(course_id)["active"] is False

        second_token = runner.start(course_id)
        await runner.wait_idle(course_id)
        return first_token, second_token

    first_token, second_token = asyncio.run(scenario())
    assert first_token != second_token


# --------------------------------------------------------------------------
# (3) event bus sequence
# --------------------------------------------------------------------------


def test_events_sequence_covers_run_and_every_stage_in_order(session_factory, blob_store, course_id):
    _seed_summarizable_course(session_factory, blob_store, course_id)

    async def scenario():
        runner = _make_runner(session_factory, blob_store, MockBackend())
        queue = runner.event_bus.subscribe()

        runner.start(course_id)

        events = []
        while True:
            event = await asyncio.wait_for(queue.get(), timeout=5)
            events.append(event)
            if event["status"] == "run-finished":
                break

        runner.event_bus.unsubscribe(queue)
        return events

    events = asyncio.run(scenario())

    assert events[0]["status"] == "run-started"
    assert events[0]["stage"] is None
    assert events[-1]["status"] == "run-finished"
    assert events[-1]["stage"] is None

    stage_events = events[1:-1]
    assert [(e["stage"], e["status"]) for e in stage_events] == [
        ("summarize", "started"),
        ("summarize", "complete"),
        ("taxonomy", "started"),
        ("taxonomy", "complete"),
        ("classify", "started"),
        ("classify", "complete"),
        ("assemble", "started"),
        ("assemble", "complete"),
    ]
    for event in events:
        assert event["type"] == "pipeline"
        assert event["courseId"] == course_id
        assert event["runToken"] == events[0]["runToken"]


# --------------------------------------------------------------------------
# (4) cost cap
# --------------------------------------------------------------------------


def test_cost_cap_aborts_summarize_and_skips_classify_but_still_assembles(session_factory, blob_store, course_id):
    _seed_summarizable_course(session_factory, blob_store, course_id, count=3)

    async def scenario():
        costly_backend = _FixedCostBackend(MockBackend(), est_cost_usd=10.0)
        runner = _make_runner(session_factory, blob_store, costly_backend, max_cost_usd_per_run=5.0)
        runner.start(course_id)
        await runner.wait_idle(course_id)
        return costly_backend

    costly_backend = asyncio.run(scenario())

    assert costly_backend.calls == 1  # cap (5) < per-call cost (10): only the first call happens

    rows = _pipeline_runs(session_factory, course_id)
    by_stage = {row.stage: row for row in rows}
    assert set(by_stage) == {"summarize", "assemble"}  # taxonomy and classify never started

    assert by_stage["summarize"].status == "aborted"
    assert by_stage["summarize"].error == "cost-cap"
    assert by_stage["assemble"].status == "complete"

    with session_factory() as session:
        course = session.get(Course, course_id)
        assert course.taxonomy_version == 0  # taxonomy never ran
