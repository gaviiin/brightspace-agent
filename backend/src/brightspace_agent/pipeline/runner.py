"""`PipelineRunner`: runs the compiled pipeline graph as a managed background
job per course, recording a `pipeline_runs` row per stage and publishing
live progress on an in-process event bus that `GET /api/events` (SSE)
subscribes to.

Constructed once at app startup (see `main.create_app`) and stashed on
`app.state.runner`. One `PipelineRunner` instance can drive concurrent runs
for *different* courses (each gets its own `asyncio.Task`); only one run per
course may be active at a time (`RunActiveError` otherwise).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from brightspace_agent.agents.llm import LLMBackend
from brightspace_agent.config import Settings
from brightspace_agent.db.models import PipelineRun
from brightspace_agent.ingest.store import BlobStore
from brightspace_agent.pipeline.graph import PipelineDeps, PipelineState, build_pipeline_graph

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunActiveError(Exception):
    """Raised by `PipelineRunner.start()` when a run is already active for
    that course. The API layer maps this to a 409."""

    def __init__(self, course_id: int) -> None:
        super().__init__(f"a pipeline run is already active for course {course_id}")
        self.course_id = course_id


# --------------------------------------------------------------------------
# Event bus
# --------------------------------------------------------------------------


class EventBus:
    """In-process pub/sub for SSE. `publish()` is callable from any thread
    (the extension-facing ingest endpoints are plain `def`s, which FastAPI
    runs in a worker thread, not the event loop) -- once a subscriber
    exists, the event loop that created its queue has necessarily already
    been captured by `subscribe()`, so a threadsafe handoff is always
    possible by the time there's anyone listening.
    """

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._loop = asyncio.get_running_loop()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def subscriber_count(self) -> int:
        """Read-only visibility into how many subscribers are live -- used
        by tests to confirm a client disconnect actually unsubscribed
        (api/events.py's generator unsubscribes in a `finally`)."""
        return len(self._subscribers)

    def publish(self, event: dict) -> None:
        if not self._subscribers:
            return  # nobody listening -- and so no loop needs to be bound yet
        for queue in list(self._subscribers):
            if self._loop is not None:
                self._loop.call_soon_threadsafe(queue.put_nowait, event)
            else:  # pragma: no cover -- defensive; subscribe() always binds first
                queue.put_nowait(event)


# --------------------------------------------------------------------------
# Per-run hooks: the bridge graph.py's nodes call into for DB rows + events
# --------------------------------------------------------------------------


class _RunHooks:
    """One instance per `start()` call. Tracks this run's cumulative spend
    so the cost cap is shared across summarize and classify (taxonomy isn't
    individually capped -- see the Task 9 brief -- but its spend still
    counts against what's left for classify)."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        event_bus: EventBus,
        course_id: int,
        run_token: int,
        max_cost_usd_per_run: float,
        row_id_sink: list[int],
    ) -> None:
        self._session_factory = session_factory
        self._event_bus = event_bus
        self._course_id = course_id
        self._run_token = run_token
        self._cap = max_cost_usd_per_run
        self._row_ids = row_id_sink  # mutated in place; PipelineRunner.status() reads this
        self._row_id_by_stage: dict[str, int] = {}
        self.spent_usd = 0.0

    def remaining_budget(self) -> float:
        return max(0.0, self._cap - self.spent_usd)

    def on_start(self, stage: str) -> None:
        with self._session_factory() as session:
            row = PipelineRun(course_id=self._course_id, stage=stage, status="running", started_at=_now_iso())
            session.add(row)
            session.commit()
            row_id = row.id
        self._row_id_by_stage[stage] = row_id
        self._row_ids.append(row_id)
        self._event_bus.publish(
            {
                "type": "pipeline",
                "courseId": self._course_id,
                "runToken": self._run_token,
                "stage": stage,
                "status": "started",
            }
        )

    def on_finish(self, stage: str, stats: dict, status: str, error: str | None) -> None:
        usage = stats.get("usage_total") or {}
        self.spent_usd += usage.get("est_cost_usd", 0.0)
        row_id = self._row_id_by_stage.get(stage)
        if row_id is not None:
            with self._session_factory() as session:
                row = session.get(PipelineRun, row_id)
                if row is not None:
                    row.status = status
                    row.finished_at = _now_iso()
                    row.usage_json = json.dumps(usage)
                    row.error = error
                    session.commit()
        self._event_bus.publish(
            {
                "type": "pipeline",
                "courseId": self._course_id,
                "runToken": self._run_token,
                "stage": stage,
                "status": status,
                "stats": stats,
            }
        )

    def reconcile_orphaned_rows(self) -> None:
        """Called from `PipelineRunner._execute`'s `finally` block, on
        *every* exit from that block -- normal completion, an exception
        that escaped a node (e.g. `on_start`/`on_finish` themselves raising,
        rather than the stage call they bracket), or the task being
        cancelled. `finally` runs even then, which is the whole point: it's
        the one place guaranteed to run regardless of *how* the run ended.

        Any row this instance created (tracked in `_row_id_by_stage` as
        soon as `on_start` commits it -- before anything that could still
        fail, like the event-bus publish) that's still `status='running'`
        at this point means `on_finish` was never reached for it. Left
        alone, that row would claim to be running forever, with no
        automatic recovery short of the next-process startup sweep (see
        `PipelineRunner.__init__`) -- which wouldn't help a still-running
        server at all. Marked 'failed'/'orphaned' instead.
        """
        if not self._row_id_by_stage:
            return
        try:
            with self._session_factory() as session:
                now = _now_iso()
                orphaned_stages: list[str] = []
                for stage, row_id in self._row_id_by_stage.items():
                    row = session.get(PipelineRun, row_id)
                    if row is not None and row.status == "running":
                        row.status = "failed"
                        row.finished_at = now
                        row.error = "orphaned"
                        orphaned_stages.append(stage)
                if orphaned_stages:
                    session.commit()
        except Exception:
            logger.exception(
                "failed to reconcile orphaned pipeline_runs rows for course %s (run %s)",
                self._course_id, self._run_token,
            )
            return

        for stage in orphaned_stages:
            logger.warning(
                "pipeline_runs row for course %s stage %r was left 'running' (run %s); marked orphaned",
                self._course_id, stage, self._run_token,
            )
            self._event_bus.publish(
                {
                    "type": "pipeline",
                    "courseId": self._course_id,
                    "runToken": self._run_token,
                    "stage": stage,
                    "status": "failed",
                    "stats": {},
                }
            )


# --------------------------------------------------------------------------
# PipelineRunner
# --------------------------------------------------------------------------


class PipelineRunner:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        blob_store: BlobStore,
        backend: LLMBackend,
        settings: Settings,
        *,
        event_bus: EventBus | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self.backend = backend  # read by tests/callers that want to observe LLM traffic
        self.settings = settings  # read by the dry-run endpoint (configured model names)
        self.event_bus = event_bus if event_bus is not None else EventBus()

        deps = PipelineDeps(
            session_factory=session_factory, blob_store=blob_store, backend=backend, settings=settings
        )
        # Built once: the compiled graph is stateless between runs (course_id
        # and per-run hooks travel through astream()'s state/config args).
        self._graph = build_pipeline_graph(deps)

        self._active: dict[int, int] = {}  # course_id -> run_token, present only while running
        self._tasks: dict[int, asyncio.Task] = {}  # course_id -> most recent run's task
        self._last_run_rows: dict[int, list[int]] = {}  # course_id -> pipeline_runs.id for the latest run
        self._run_tokens = itertools.count(1)

        self._reconcile_orphaned_rows_from_previous_process()

    def _reconcile_orphaned_rows_from_previous_process(self) -> None:
        """A `pipeline_runs` row can only be `status='running'` while a live
        task is driving it. At construction time, before `start()` has ever
        been called, `self._active` is empty and no such task exists in
        this process -- so any row already `'running'` in the DB can only
        be left over from an *earlier* process that exited without
        finishing it (a crash, `kill -9`, an unclean shutdown). There is no
        task left to eventually reconcile it (the per-run reconciliation in
        `_execute`'s `finally` only covers runs *this* process started), so
        it's swept here instead, once, at startup.
        """
        with self._session_factory() as session:
            stale_rows = list(
                session.execute(select(PipelineRun).where(PipelineRun.status == "running")).scalars().all()
            )
            if not stale_rows:
                return
            now = _now_iso()
            for row in stale_rows:
                row.status = "failed"
                row.finished_at = now
                row.error = "orphaned-by-restart"
                self._last_run_rows.setdefault(row.course_id, []).append(row.id)
            session.commit()
        logger.warning(
            "reconciled %d orphaned 'running' pipeline_runs row(s) left over from a previous process",
            len(stale_rows),
        )

    def start(self, course_id: int, stages: list[str] | None = None) -> int:
        """Launch a background run for `course_id`. Returns a run token.
        Raises `RunActiveError` if a run is already active for this course."""
        if course_id in self._active:
            raise RunActiveError(course_id)

        run_token = next(self._run_tokens)
        self._active[course_id] = run_token
        row_sink: list[int] = []
        self._last_run_rows[course_id] = row_sink
        requested_stages = set(stages) if stages else None

        hooks = _RunHooks(
            self._session_factory, self.event_bus, course_id, run_token,
            self._settings.max_cost_usd_per_run, row_sink,
        )

        self.event_bus.publish(
            {"type": "pipeline", "courseId": course_id, "runToken": run_token, "stage": None, "status": "run-started"}
        )
        task = asyncio.create_task(self._execute(course_id, run_token, requested_stages, hooks))
        self._tasks[course_id] = task
        return run_token

    async def _execute(
        self, course_id: int, run_token: int, requested_stages: set[str] | None, hooks: _RunHooks
    ) -> None:
        config = {"configurable": {"hooks": hooks, "requested_stages": requested_stages}}
        initial_state: PipelineState = {"course_id": course_id, "stage_stats": {}, "error": None}
        try:
            # astream (not ainvoke) so per-node completion is observed as it
            # happens -- graph.py's nodes already fire hooks.on_start/
            # on_finish (and therefore SSE events) synchronously as each
            # node runs, so nothing here needs to inspect the yielded
            # updates; the loop's only job is to keep the graph advancing.
            async for _update in self._graph.astream(initial_state, config=config, stream_mode="updates"):
                pass
        except Exception:
            logger.exception("pipeline run crashed for course %s (run %s)", course_id, run_token)
        finally:
            # Runs on every exit from the try above -- normal completion, a
            # caught Exception, or an uncaught BaseException (e.g. this
            # task being cancelled) propagating straight through. That's
            # deliberate: it's the one place guaranteed to run regardless
            # of how the run ended, so it's where a row left 'running' by
            # a hook failure or cancellation gets reconciled.
            hooks.reconcile_orphaned_rows()
            self._active.pop(course_id, None)
            self.event_bus.publish(
                {
                    "type": "pipeline", "courseId": course_id, "runToken": run_token,
                    "stage": None, "status": "run-finished",
                }
            )

    async def wait_idle(self, course_id: int) -> None:
        """Await the most recently started run for `course_id`, if any is
        tracked. A thin convenience for tests and any caller that wants to
        block on completion instead of polling `status()`/SSE."""
        task = self._tasks.get(course_id)
        if task is not None:
            await task

    def status(self, course_id: int) -> dict:
        """`{active, stages: [{stage, status, startedAt, finishedAt, usage}]}`
        for the most recent run of `course_id` -- `stages` is empty if no
        run has happened yet (in this server process)."""
        active = course_id in self._active
        row_ids = self._last_run_rows.get(course_id, [])
        stages: list[dict] = []
        if row_ids:
            with self._session_factory() as session:
                for row_id in row_ids:
                    row = session.get(PipelineRun, row_id)
                    if row is None:
                        continue
                    stages.append(
                        {
                            "stage": row.stage,
                            "status": row.status,
                            "startedAt": row.started_at,
                            "finishedAt": row.finished_at,
                            "usage": json.loads(row.usage_json) if row.usage_json else None,
                        }
                    )
        return {"active": active, "stages": stages}
