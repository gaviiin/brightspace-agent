"""`PipelineRunner`: runs the compiled pipeline graph (`start()`) as a
managed background job per course, and (M3.2) the on-demand enrichment path
(`start_enrichment()`) alongside it -- both record a `pipeline_runs` row
(the enrichment path: one row, `stage='enrich'`, since it's a single async
call rather than a graph of stage nodes) and publish live progress on an
in-process event bus that `GET /api/events` (SSE) subscribes to.

Constructed once at app startup (see `main.create_app`) and stashed on
`app.state.runner`. One `PipelineRunner` instance can drive concurrent runs
for *different* courses (each gets its own `asyncio.Task`); only one run per
course may be active at a time (`RunActiveError` otherwise) -- pipeline and
enrichment share that guard, so the two kinds can never collide either.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from brightspace_agent.agents.llm import LLMBackend
from brightspace_agent.agents.web import MockWebBackend, WebBackend
from brightspace_agent.config import Settings
from brightspace_agent.db.models import Course, PipelineRun, Topic
from brightspace_agent.ingest.store import BlobStore
from brightspace_agent.pipeline.graph import PipelineDeps, PipelineState, build_pipeline_graph
from brightspace_agent.pipeline.stages.enrich import run_enrich_stage, run_topic_enrichment

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunActiveError(Exception):
    """Raised by `PipelineRunner.start()`/`start_enrichment()` when a run
    (pipeline OR enrichment -- they share one guard, see `start_enrichment`'s
    docstring) is already active for that course. The API layer maps this to
    a 409."""

    def __init__(self, course_id: int) -> None:
        super().__init__(f"a pipeline run is already active for course {course_id}")
        self.course_id = course_id


class TopicNotFoundError(Exception):
    """Raised by `PipelineRunner.start_enrichment(topic_id=...)` when
    `topic_id` doesn't exist, doesn't belong to the given course, or isn't
    part of that course's CURRENT taxonomy version (e.g. an id from a
    version a structural taxonomy edit has since superseded). The API layer
    maps this to a 404."""

    def __init__(self, topic_id: int) -> None:
        super().__init__(f"no topic {topic_id} at the course's current taxonomy version")
        self.topic_id = topic_id


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
# Per-run hooks for the enrichment path (M3.2). Enrichment isn't a LangGraph
# node -- `start_enrichment` drives `run_topic_enrichment`/`run_enrich_stage`
# directly from one async task -- so there's exactly one `pipeline_runs` row
# per run (stage='enrich'), not one per graph node. Mirrors `_RunHooks`'s
# row + SSE bookkeeping and orphan-reconciliation shape, collapsed
# accordingly, and publishes `type: "enrichment"` events instead of
# `type: "pipeline"`.
# --------------------------------------------------------------------------


class _EnrichmentRunHooks:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        event_bus: EventBus,
        course_id: int,
        run_token: int,
        topic_id: int | None,
    ) -> None:
        self._session_factory = session_factory
        self._event_bus = event_bus
        self._course_id = course_id
        self._run_token = run_token
        self._topic_id = topic_id
        self._row_id: int | None = None  # set by on_start; reconcile_orphaned_rows reads it

    def on_start(self) -> None:
        with self._session_factory() as session:
            row = PipelineRun(course_id=self._course_id, stage="enrich", status="running", started_at=_now_iso())
            session.add(row)
            session.commit()
            self._row_id = row.id
        self._publish("run-started")

    def on_finish(self, stats: dict, status: str, error: str | None) -> None:
        if self._row_id is not None:
            with self._session_factory() as session:
                row = session.get(PipelineRun, self._row_id)
                if row is not None:
                    row.status = status
                    row.finished_at = _now_iso()
                    row.usage_json = json.dumps(stats.get("usage_total") or {})
                    row.error = error
                    session.commit()
        self._publish(status, stats=stats)

    def reconcile_orphaned_rows(self) -> None:
        """Called from `PipelineRunner._execute_enrichment`'s `finally`
        block on every exit -- see `_RunHooks.reconcile_orphaned_rows`'s
        docstring for the full reasoning; identical here, just for this
        run's single row instead of a per-stage dict of them."""
        if self._row_id is None:
            return
        orphaned = False
        try:
            with self._session_factory() as session:
                row = session.get(PipelineRun, self._row_id)
                if row is not None and row.status == "running":
                    row.status = "failed"
                    row.finished_at = _now_iso()
                    row.error = "orphaned"
                    session.commit()
                    orphaned = True
        except Exception:
            logger.exception(
                "failed to reconcile orphaned enrichment pipeline_runs row for course %s (run %s)",
                self._course_id, self._run_token,
            )
            return

        if orphaned:
            logger.warning(
                "pipeline_runs row for course %s stage 'enrich' was left 'running' (run %s); marked orphaned",
                self._course_id, self._run_token,
            )
            self._publish("failed", stats={})

    def _publish(self, status: str, *, stats: dict | None = None) -> None:
        event: dict = {
            "type": "enrichment",
            "courseId": self._course_id,
            "runToken": self._run_token,
            "status": status,
        }
        if self._topic_id is not None:
            event["topicId"] = self._topic_id
        if stats is not None:
            event["stats"] = stats
        self._event_bus.publish(event)


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
        web_backend: WebBackend | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self.backend = backend  # read by tests/callers that want to observe LLM traffic
        # M3.2: enrichment's finder/verifier need the web-tool backend, not
        # the plain LLMBackend above. Optional (defaults to the offline mock)
        # so every existing caller/test that constructs a PipelineRunner
        # without one -- there's no other kind of run that needs it -- keeps
        # working unchanged; main.py's real app always passes the real one.
        self.web_backend = web_backend if web_backend is not None else MockWebBackend()
        self.settings = settings  # read by the dry-run endpoint (configured model names)
        self.event_bus = event_bus if event_bus is not None else EventBus()

        deps = PipelineDeps(
            session_factory=session_factory, blob_store=blob_store, backend=backend, settings=settings
        )
        # Built once: the compiled graph is stateless between runs (course_id
        # and per-run hooks travel through astream()'s state/config args).
        self._graph = build_pipeline_graph(deps)

        # `_active` is shared between the pipeline path (start()) and the
        # enrichment path (start_enrichment()) -- one course_id -> run_token
        # map, so a pipeline run and an enrichment run can never both be
        # active for the same course (see start_enrichment's docstring).
        self._active: dict[int, int] = {}  # course_id -> run_token, present only while running
        self._tasks: dict[int, asyncio.Task] = {}  # course_id -> most recent pipeline run's task
        self._last_run_rows: dict[int, list[int]] = {}  # course_id -> pipeline_runs.id for the latest run
        self._enrichment_tasks: dict[int, asyncio.Task] = {}  # course_id -> most recent enrichment run's task
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
        try:
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
        except Exception:  # noqa: BLE001 -- a DB hiccup here must degrade, not crash boot (matches
            # reconcile_orphaned_rows's own try/except, its per-run sibling)
            logger.exception("failed to reconcile orphaned pipeline_runs rows from a previous process at startup")
            return
        logger.warning(
            "reconciled %d orphaned 'running' pipeline_runs row(s) left over from a previous process",
            len(stale_rows),
        )

    def is_active(self, course_id: int) -> bool:
        """True if a run is currently active for `course_id`. Lets a caller
        that's about to do something expensive/durable on the strength of
        "then I'll start a run" (see pipeline/taxonomy_apply.py's structural
        path) check first, instead of discovering the conflict only when
        `start()` itself raises `RunActiveError` -- by then, whatever the
        caller already committed stays committed regardless."""
        return course_id in self._active

    def start(
        self, course_id: int, stages: list[str] | None = None, *, force_taxonomy: bool = False
    ) -> int:
        """Launch a background run for `course_id`. Returns a run token.
        Raises `RunActiveError` if a run is already active for this course.

        `force_taxonomy` lets S2 re-propose over a taxonomy the student has
        edited (see pipeline/stages/taxonomy.py's `force`). Default False:
        an ordinary run leaves a user-edited taxonomy exactly as it is.
        """
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
        task = asyncio.create_task(
            self._execute(course_id, run_token, requested_stages, hooks, force_taxonomy)
        )
        self._tasks[course_id] = task
        return run_token

    async def _execute(
        self,
        course_id: int,
        run_token: int,
        requested_stages: set[str] | None,
        hooks: _RunHooks,
        force_taxonomy: bool = False,
    ) -> None:
        config = {
            "configurable": {
                "hooks": hooks,
                "requested_stages": requested_stages,
                "force_taxonomy": force_taxonomy,
            }
        }
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

    # ----------------------------------------------------------------------
    # M3.2: on-demand enrichment. Deliberately NOT a LangGraph node -- see
    # the module docstring's framing and pipeline/stages/enrich.py's own
    # docstring. `start()`/`_execute()` above drive the compiled S1-S4 graph;
    # this drives run_topic_enrichment/run_enrich_stage directly from one
    # background task, sharing only the `_active` guard, the `pipeline_runs`
    # table (stage='enrich'), and the event bus with the pipeline path.
    # ----------------------------------------------------------------------

    def start_enrichment(
        self, course_id: int, *, topic_id: int | None = None, cost_cap_usd: float | None = None
    ) -> int:
        """Launch a background enrichment run for `course_id`. `topic_id=None`
        enriches every topic at the course's current taxonomy version
        (`run_enrich_stage`); a given `topic_id` enriches just that one topic
        (`run_topic_enrichment`) -- after validating it belongs to
        `course_id` at the course's CURRENT taxonomy version, raising
        `TopicNotFoundError` (API layer: 404) otherwise.

        Reuses the exact `_active` guard `start()` does: `RunActiveError`
        (API layer: 409) if a run of EITHER kind is already active for this
        course, so a pipeline run and an enrichment run can never collide.
        `cost_cap_usd` defaults to `Settings.max_cost_usd_per_run`, same as
        the pipeline path; for the course-batch path it is passed straight
        through to `run_enrich_stage` unchanged -- i.e. applied PER TOPIC,
        not shared across the whole batch (an accepted M3.1 limitation; see
        the M3.2 brief). Returns a run token.
        """
        if course_id in self._active:
            raise RunActiveError(course_id)
        if topic_id is not None:
            self._validate_topic_for_enrichment(course_id, topic_id)

        cap = cost_cap_usd if cost_cap_usd is not None else self._settings.max_cost_usd_per_run
        run_token = next(self._run_tokens)
        self._active[course_id] = run_token

        hooks = _EnrichmentRunHooks(self._session_factory, self.event_bus, course_id, run_token, topic_id)
        task = asyncio.create_task(self._execute_enrichment(course_id, run_token, topic_id, cap, hooks))
        self._enrichment_tasks[course_id] = task
        return run_token

    def _validate_topic_for_enrichment(self, course_id: int, topic_id: int) -> None:
        with self._session_factory() as session:
            course = session.get(Course, course_id)
            topic = session.get(Topic, topic_id)
            if (
                course is None
                or topic is None
                or topic.course_id != course_id
                or topic.taxonomy_version != course.taxonomy_version
            ):
                raise TopicNotFoundError(topic_id)

    async def _execute_enrichment(
        self,
        course_id: int,
        run_token: int,
        topic_id: int | None,
        cost_cap_usd: float,
        hooks: _EnrichmentRunHooks,
    ) -> None:
        try:
            # `on_start` sits outside the inner try/except below, deliberately
            # mirroring graph.py's per-node shape (hooks.on_start(stage) then
            # a try/except around just the stage call): if on_start itself
            # raises (e.g. its event-bus publish blows up right after the
            # row was already committed), that's a bug in the hook, not a
            # "the enrichment run failed" outcome -- there's no stats/error
            # to report via on_finish, and reconcile_orphaned_rows() below is
            # what catches the row it already created.
            hooks.on_start()
            try:
                if topic_id is not None:
                    stats = await run_topic_enrichment(
                        self._session_factory, self.backend, self.web_backend, topic_id, cost_cap_usd=cost_cap_usd,
                    )
                else:
                    stats = await run_enrich_stage(
                        self._session_factory, self.backend, self.web_backend, course_id, cost_cap_usd=cost_cap_usd,
                    )
            except Exception as exc:  # noqa: BLE001 -- turned into a failed run row, not raised
                logger.exception("enrichment run crashed for course %s (run %s)", course_id, run_token)
                hooks.on_finish({}, "failed", f"{type(exc).__name__}: {exc}")
            else:
                stats_dict = asdict(stats)
                status = "aborted" if stats_dict.get("aborted") else "complete"
                hooks.on_finish(stats_dict, status, "cost-cap" if status == "aborted" else None)
        except Exception:
            logger.exception("enrichment on_start failed for course %s (run %s)", course_id, run_token)
        finally:
            # Same reasoning as _execute's finally: the one place guaranteed
            # to run regardless of how the run ended (including this task
            # being cancelled), so it's where a row left 'running' by a
            # hook failure or cancellation gets reconciled.
            hooks.reconcile_orphaned_rows()
            self._active.pop(course_id, None)

    async def wait_enrichment_idle(self, course_id: int) -> None:
        """Await the most recently started enrichment run for `course_id`,
        if any is tracked. Mirrors `wait_idle` for the pipeline path."""
        task = self._enrichment_tasks.get(course_id)
        if task is not None:
            await task

    def enrichment_status(self, course_id: int) -> dict:
        """`{active, lastRun}` for `course_id`'s enrichment. `active` is
        `is_active()` -- the guard is shared with the pipeline path, so this
        answers "is anything blocking a new enrichment run right now",
        accurately regardless of which kind is currently active. `lastRun`
        is read straight from the DB (the latest stage='enrich'
        `pipeline_runs` row for this course, across ALL processes/runs, not
        just this one's in-memory state) -- `None` if enrichment has never
        run for this course."""
        active = self.is_active(course_id)
        with self._session_factory() as session:
            row = (
                session.execute(
                    select(PipelineRun)
                    .where(PipelineRun.course_id == course_id, PipelineRun.stage == "enrich")
                    .order_by(PipelineRun.id.desc())
                    .limit(1)
                )
                .scalars()
                .first()
            )
            last_run = None
            if row is not None:
                last_run = {
                    "status": row.status,
                    "startedAt": row.started_at,
                    "finishedAt": row.finished_at,
                    "usage": json.loads(row.usage_json) if row.usage_json else None,
                }
        return {"active": active, "lastRun": last_run}
