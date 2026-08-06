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
from brightspace_agent.db.models import Course, Material, MaterialTopic, PipelineRun, Topic
from brightspace_agent.db.session import init_db
from brightspace_agent.ingest.store import BlobStore
from brightspace_agent.pipeline import runner as runner_module
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


def _write_taxonomy(session_factory, course_id, version, topics) -> dict[str, int]:
    with session_factory() as session:
        ids: dict[str, int] = {}
        for order_index, (slug, name, description) in enumerate(topics):
            topic = Topic(
                course_id=course_id, taxonomy_version=version, slug=slug, name=name,
                description=description, order_index=order_index, created_by="agent",
            )
            session.add(topic)
            session.flush()
            ids[slug] = topic.id
        course = session.get(Course, course_id)
        course.taxonomy_version = version
        session.commit()
        return ids


def _mark_summarized(session_factory, material_id, *, summary="A pre-existing summary.") -> None:
    with session_factory() as session:
        material = session.get(Material, material_id)
        material.status = "summarized"
        material.summary = summary
        session.commit()


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


def test_is_active_reflects_the_same_state_start_guards_on(session_factory, blob_store, course_id):
    """`is_active()` (Task 12: checked by taxonomy_apply.py's structural
    path before writing anything) must agree with what `start()` itself
    guards on -- same underlying `_active` state, checked without the
    side effect of raising."""
    _seed_summarizable_course(session_factory, blob_store, course_id)

    async def scenario():
        runner = _make_runner(session_factory, blob_store, MockBackend())
        assert runner.is_active(course_id) is False

        runner.start(course_id)
        assert runner.is_active(course_id) is True  # no await yet -- still active

        await runner.wait_idle(course_id)
        assert runner.is_active(course_id) is False

    asyncio.run(scenario())


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
    # Overshoot-tolerant on purpose (Task 13 restored real fan-out --
    # summarize/classify now run at concurrency=4, not 1 -- so the cost cap
    # is optimistic, not exact: see pipeline/graph.py's
    # _CAPPED_STAGE_CONCURRENCY docstring). `count` is well past that
    # concurrency so that even in the worst-case race -- all 4 concurrent
    # workers passing the "are we under the cap yet?" check before any of
    # them has recorded its $10 spend against the $5 cap -- there are still
    # materials left in the worklist that a *later* batch (after a slot
    # frees up and spend has been recorded) must correctly see as
    # over-budget and leave untouched.
    worklist_size = 10
    _seed_summarizable_course(session_factory, blob_store, course_id, count=worklist_size)

    async def scenario():
        costly_backend = _FixedCostBackend(MockBackend(), est_cost_usd=10.0)
        runner = _make_runner(session_factory, blob_store, costly_backend, max_cost_usd_per_run=5.0)
        runner.start(course_id)
        await runner.wait_idle(course_id)
        return costly_backend

    costly_backend = asyncio.run(scenario())

    assert costly_backend.calls >= 1  # the cap (5) is reached; progress was still made
    assert costly_backend.calls < worklist_size  # and at least one call was blocked, not the whole worklist

    rows = _pipeline_runs(session_factory, course_id)
    by_stage = {row.stage: row for row in rows}
    assert set(by_stage) == {"summarize", "assemble"}  # taxonomy and classify never started

    assert by_stage["summarize"].status == "aborted"
    assert by_stage["summarize"].error == "cost-cap"
    assert by_stage["assemble"].status == "complete"

    with session_factory() as session:
        course = session.get(Course, course_id)
        assert course.taxonomy_version == 0  # taxonomy never ran


# --------------------------------------------------------------------------
# (5) `stages` partial-run filtering
# --------------------------------------------------------------------------


def test_stages_filter_skips_unrequested_stages_entirely(session_factory, blob_store, course_id):
    """A course already summarized + taxonomied, run with
    stages=['classify', 'assemble']. summarize/taxonomy must not just be
    no-ops -- they must not run at all: no pipeline_runs rows for them, and
    (the real proof) a material that summarize *would* touch if it ran is
    left completely untouched."""
    already_summarized_ids = _seed_summarizable_course(session_factory, blob_store, course_id, count=2)
    for material_id in already_summarized_ids:
        _mark_summarized(session_factory, material_id)
    _write_taxonomy(
        session_factory, course_id, 1,
        [("intro", "Intro", "d"), ("loops", "Loops", "d"), ("arrays", "Arrays", "d")],
    )
    # summarize would extract + summarize this one if it ran; it must not.
    untouched_id = _add_fetched_pdf(
        session_factory, blob_store, course_id, title="New Lecture", text="Should stay completely untouched."
    )

    async def scenario():
        runner = _make_runner(session_factory, blob_store, MockBackend())
        runner.start(course_id, stages=["classify", "assemble"])
        await runner.wait_idle(course_id)

    asyncio.run(scenario())

    rows = _pipeline_runs(session_factory, course_id)
    # No rows at all for the skipped stages -- this implementation's chosen
    # semantics (a skipped node never calls hooks.on_start/on_finish).
    assert [row.stage for row in rows] == ["classify", "assemble"]
    assert all(row.status == "complete" for row in rows)

    with session_factory() as session:
        untouched = session.get(Material, untouched_id)
        assert untouched.status == "fetched"  # summarize never ran
        course = session.get(Course, course_id)
        assert course.taxonomy_version == 1  # taxonomy never bumped it

    with session_factory() as session:
        assignments = session.execute(select(MaterialTopic)).scalars().all()
    assert len(assignments) > 0  # classify genuinely ran


# --------------------------------------------------------------------------
# (6) orphaned 'running' rows are reconciled, not left stuck forever
# --------------------------------------------------------------------------


def test_on_start_failure_does_not_leave_a_row_stuck_running(session_factory, blob_store, course_id, monkeypatch):
    """If `hooks.on_start` raises *after* it has already created and
    committed the pipeline_runs row (e.g. the event-bus publish call at the
    end of `on_start` blows up), that row must not be left at 'running'
    forever -- the per-run reconciliation in `_execute`'s `finally` block
    must catch it."""
    _seed_summarizable_course(session_factory, blob_store, course_id, count=2)

    real_on_start = runner_module._RunHooks.on_start

    def flaky_on_start(self, stage):
        real_on_start(self, stage)  # the row is genuinely created + committed
        if stage == "summarize":
            raise RuntimeError("simulated failure right after on_start committed its row")

    monkeypatch.setattr(runner_module._RunHooks, "on_start", flaky_on_start)

    async def scenario():
        runner = _make_runner(session_factory, blob_store, MockBackend())
        runner.start(course_id)
        await runner.wait_idle(course_id)

    asyncio.run(scenario())

    rows = _pipeline_runs(session_factory, course_id)
    assert rows  # the summarize row was created before the simulated failure
    assert all(row.status != "running" for row in rows)  # nothing left stuck

    summarize_row = next(row for row in rows if row.stage == "summarize")
    assert summarize_row.status == "failed"
    assert summarize_row.error == "orphaned"
    assert summarize_row.finished_at is not None

    # And the run itself still ended cleanly (no other stage started after
    # the crash -- after_summarize never got a chance to route anywhere).
    assert [row.stage for row in rows] == ["summarize"]


def test_startup_sweep_marks_stale_running_rows_as_orphaned_by_restart(session_factory, blob_store, course_id):
    """A 'running' row with no live task behind it can only be left over
    from a previous process that never finished it. PipelineRunner's
    constructor must sweep these on startup rather than leaving them
    claiming to be running forever."""
    with session_factory() as session:
        stale = PipelineRun(
            course_id=course_id, stage="summarize", status="running",
            started_at="2020-01-01T00:00:00+00:00",
        )
        session.add(stale)
        session.commit()
        stale_id = stale.id

    runner = _make_runner(session_factory, blob_store, MockBackend())

    with session_factory() as session:
        row = session.get(PipelineRun, stale_id)
        assert row.status == "failed"
        assert row.error == "orphaned-by-restart"
        assert row.finished_at is not None

    # And it shows up through status() immediately, without needing a new
    # run for this course first.
    status = runner.status(course_id)
    assert status["active"] is False
    assert any(s["stage"] == "summarize" and s["status"] == "failed" for s in status["stages"])


def test_startup_sweep_db_failure_degrades_instead_of_crashing_construction(blob_store):
    """A DB hiccup during the startup sweep (e.g. a locked/corrupt file)
    must not take the whole app down before it can even start -- matches
    the per-run reconcile_orphaned_rows's own try/except (see the Task 13
    brief's ledgered minor)."""

    def broken_session_factory():
        raise RuntimeError("simulated DB failure")

    settings = Settings(max_cost_usd_per_run=5.0)
    runner = PipelineRunner(broken_session_factory, blob_store, MockBackend(), settings)

    assert runner.is_active(1) is False
