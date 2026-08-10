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
import shutil
from dataclasses import asdict
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from brightspace_agent.agents.llm import LLMBackend
from brightspace_agent.agents.web import MockWebBackend, WebBackend
from brightspace_agent.config import Settings
from brightspace_agent.db.models import Course, MediaSource, PipelineRun, Topic
from brightspace_agent.ingest.store import BlobStore
from brightspace_agent.media.fetch import FetchSpec, MediaFetchError, MediaFetcher, MockMediaFetcher
from brightspace_agent.media.ingest_transcript import ingest_transcript
from brightspace_agent.media.transcribe import MediaTranscribeError, MockTranscriber, Transcriber
from brightspace_agent.pipeline.graph import PipelineDeps, PipelineState, build_pipeline_graph
from brightspace_agent.pipeline.stages.enrich import run_enrich_stage, run_topic_enrichment

logger = logging.getLogger(__name__)

# What a `media_sources` row left mid-job by a dead process gets as its
# error (see `_reconcile_orphaned_rows_from_previous_process`). Written to be
# read by the course owner in the Recordings drawer, so it names the action
# rather than the internal cause.
_INTERRUPTED_BY_RESTART = "interrupted by a server restart; press Process to retry"

# Local ASR runs on the one GPU this machine has. `_execute_media` is already
# sequential *within* a run, but nothing stopped two courses' media jobs
# (separate asyncio tasks, separate `_active` entries) from reaching the
# transcriber at the same time and thrashing that GPU. Module-level so the
# serialization is process-wide -- one semaphore for every PipelineRunner
# instance, matching the one piece of hardware it's standing in for.
#
# Worth knowing if you write a test that makes two runs contend on this: an
# asyncio.Semaphore binds itself to the running loop the first time an
# acquire actually has to WAIT, and raises if a later contended acquire
# happens on a different loop. The app has exactly one loop for its whole
# life, so this only ever matters across tests that each build their own.
_ASR_SEMAPHORE = asyncio.Semaphore(1)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunActiveError(Exception):
    """Raised by `PipelineRunner.start()`/`start_enrichment()`/
    `start_media()` when a run (pipeline OR enrichment OR media -- all three
    share one guard, see `start_enrichment`'s and `start_media`'s
    docstrings) is already active for that course. The API layer maps this
    to a 409."""

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


class MediaSourceNotFoundError(Exception):
    """Raised by `PipelineRunner.start_media(source_ids=...)` when a given
    `source_id` doesn't exist, or exists but doesn't belong to `course_id`.
    Checked for every id in `source_ids` before any processing starts (see
    `start_media`'s docstring). The API layer maps this to a 404."""

    def __init__(self, source_id: int) -> None:
        super().__init__(f"no media source {source_id} for this course")
        self.source_id = source_id


class NoMediaToProcessError(Exception):
    """Raised by `PipelineRunner.start_media()` when there is nothing to
    run: either the default worklist (every `media_sources` row with status
    in `('detected', 'failed')`) is empty, or an explicitly given
    `source_id` is already `'done'` (retrying a done source isn't a thing --
    see `start_media`'s docstring). The API layer maps this to a 400."""


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


def _attempt_token() -> str:
    """A fresh, sortable, on-disk-safe attempt-dir name: microsecond-
    precision UTC timestamp, no colons. `start_media` builds a NEW attempt
    dir (`settings.media_dir / str(source_id) / _attempt_token()`) every
    time it processes a source -- never reusing a previous attempt's dir --
    so stale files from an earlier failed try are unreachable to a retry.
    Flagged in M2.2's review."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")


# --------------------------------------------------------------------------
# Per-run hooks for the media path (M2.4). Like enrichment, deliberately NOT
# a LangGraph node -- `start_media` drives the fetch/transcribe/ingest
# sequence directly from one background task, one source at a time
# (sequential -- single GPU for transcription). One `pipeline_runs` row per
# run (stage='media'); on top of the run-level started/complete/failed
# events this also emits one SSE event per source-level status change
# (fetching/transcribing/done/failed), each carrying a `sourceId`.
# --------------------------------------------------------------------------


class _MediaRunHooks:
    def __init__(
        self, session_factory: sessionmaker[Session], event_bus: EventBus, course_id: int, run_token: int
    ) -> None:
        self._session_factory = session_factory
        self._event_bus = event_bus
        self._course_id = course_id
        self._run_token = run_token
        self._row_id: int | None = None  # set by on_start; reconcile_orphaned_rows reads it

    def on_start(self) -> None:
        with self._session_factory() as session:
            row = PipelineRun(course_id=self._course_id, stage="media", status="running", started_at=_now_iso())
            session.add(row)
            session.commit()
            self._row_id = row.id
        self._publish("run-started")

    def on_finish(self, counts: dict, status: str, error: str | None) -> None:
        # The leading three keys keep the Runs drawer's existing usage_json
        # parser happy (api/courses.py's `_pipeline_run_out` reads
        # input_tokens/output_tokens/est_cost_usd) -- media spends nothing
        # on an LLM, so they're always zero; the rest are this run's own
        # bookkeeping, read by nothing but this module's own tests today.
        usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "est_cost_usd": 0.0,
            "sources": counts["sources"],
            "done": counts["done"],
            "failed": counts["failed"],
            "captions": counts["captions"],
            "transcribed": counts["transcribed"],
        }
        if self._row_id is not None:
            with self._session_factory() as session:
                row = session.get(PipelineRun, self._row_id)
                if row is not None:
                    row.status = status
                    row.finished_at = _now_iso()
                    row.usage_json = json.dumps(usage)
                    row.error = error
                    session.commit()
        self._publish(status)

    def emit_source(self, source_id: int, status: str) -> None:
        self._publish(status, source_id=source_id)

    def reconcile_orphaned_rows(self) -> None:
        """Mirrors `_EnrichmentRunHooks.reconcile_orphaned_rows` -- see its
        docstring for the full reasoning; identical here for this run's
        single stage='media' row."""
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
                "failed to reconcile orphaned media pipeline_runs row for course %s (run %s)",
                self._course_id, self._run_token,
            )
            return

        if orphaned:
            logger.warning(
                "pipeline_runs row for course %s stage 'media' was left 'running' (run %s); marked orphaned",
                self._course_id, self._run_token,
            )
            self._publish("failed")

    def _publish(self, status: str, *, source_id: int | None = None) -> None:
        event: dict = {
            "type": "media",
            "courseId": self._course_id,
            "runToken": self._run_token,
            "status": status,
        }
        if source_id is not None:
            event["sourceId"] = source_id
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
        media_fetcher: MediaFetcher | None = None,
        transcriber: Transcriber | None = None,
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
        self.blob_store = blob_store  # M2.4: start_media's ingest_transcript() call needs it
        # M2.4: same optional/defaults-to-mock rule as web_backend above --
        # every existing caller/test that constructs a PipelineRunner
        # without these keeps working unchanged (no subprocess/heavy import
        # anywhere in that path); main.py's real app always passes the real
        # ones via make_media_fetcher/make_transcriber.
        self.media_fetcher = media_fetcher if media_fetcher is not None else MockMediaFetcher()
        self.transcriber = transcriber if transcriber is not None else MockTranscriber()

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
        self._media_tasks: dict[int, asyncio.Task] = {}  # course_id -> most recent media run's task
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

        `media_sources` rows carry the identical property one level down: a
        row is only `'fetching'`/`'transcribing'` while `_process_media_
        source` is actively driving it, so at construction time those can
        only be leftovers too. Left alone they're worse than a stale
        `pipeline_runs` row -- the Recordings drawer offers no action at all
        on a row in a mid-job status, so the source would be permanently
        unprocessable short of hand-editing the database. Failed here with a
        user-facing hint instead, which puts Process back on the row.
        """
        try:
            with self._session_factory() as session:
                stale_rows = list(
                    session.execute(select(PipelineRun).where(PipelineRun.status == "running")).scalars().all()
                )
                stuck_sources = list(
                    session.execute(
                        select(MediaSource).where(MediaSource.status.in_(("fetching", "transcribing")))
                    ).scalars().all()
                )
                if not stale_rows and not stuck_sources:
                    return
                now = _now_iso()
                for row in stale_rows:
                    row.status = "failed"
                    row.finished_at = now
                    row.error = "orphaned-by-restart"
                    self._last_run_rows.setdefault(row.course_id, []).append(row.id)
                for source in stuck_sources:
                    source.status = "failed"
                    source.error = _INTERRUPTED_BY_RESTART
                    source.updated_at = now
                session.commit()
        except Exception:  # noqa: BLE001 -- a DB hiccup here must degrade, not crash boot (matches
            # reconcile_orphaned_rows's own try/except, its per-run sibling)
            logger.exception("failed to reconcile orphaned rows from a previous process at startup")
            return
        if stale_rows:
            logger.warning(
                "reconciled %d orphaned 'running' pipeline_runs row(s) left over from a previous process",
                len(stale_rows),
            )
        if stuck_sources:
            logger.warning(
                "reconciled %d media_sources row(s) left mid-job by a previous process", len(stuck_sources)
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

    # ----------------------------------------------------------------------
    # M2.4: on-demand media (fetch -> transcribe -> ingest). Deliberately NOT
    # a LangGraph node -- same reasoning as start_enrichment above -- driving
    # the fetch/transcribe/ingest sequence directly from one background
    # task, sharing the `_active` guard, the `pipeline_runs` table
    # (stage='media'), and the event bus with the pipeline/enrichment paths.
    # Sequential across sources (one at a time): local ASR runs on a single
    # GPU, so there is no fan-out to parallelize here the way summarize/
    # classify do.
    # ----------------------------------------------------------------------

    def start_media(self, course_id: int, source_ids: list[int] | None = None) -> int:
        """Launch a background media job for `course_id`: for each source in
        the worklist, fetch (captions or audio), transcribe if audio, and
        ingest the resulting VTT as a transcript material -- one source at a
        time. Returns a run token.

        Worklist: `source_ids` if given, else every `media_sources` row for
        this course with status in `('detected', 'failed')` (rows already
        `'skipped'`/`'done'` are never auto-included). When `source_ids` IS
        given, every id is validated BEFORE any processing starts: unknown /
        not-this-course raises `MediaSourceNotFoundError` (API layer: 404);
        an id already `'done'` raises `NoMediaToProcessError` (API layer:
        400) -- any other status is allowed (retry semantics, including
        `'skipped'`). The no-args form raises `NoMediaToProcessError` if the
        default worklist is empty.

        Reuses the exact `_active` guard `start()`/`start_enrichment()` do:
        `RunActiveError` (API layer: 409) if a run of ANY kind is already
        active for this course.
        """
        if course_id in self._active:
            raise RunActiveError(course_id)

        worklist_ids = self._build_media_worklist(course_id, source_ids)

        run_token = next(self._run_tokens)
        self._active[course_id] = run_token

        hooks = _MediaRunHooks(self._session_factory, self.event_bus, course_id, run_token)
        task = asyncio.create_task(self._execute_media(course_id, run_token, worklist_ids, hooks))
        self._media_tasks[course_id] = task
        return run_token

    def _build_media_worklist(self, course_id: int, source_ids: list[int] | None) -> list[int]:
        with self._session_factory() as session:
            if source_ids is not None:
                worklist: list[int] = []
                for source_id in source_ids:
                    row = session.get(MediaSource, source_id)
                    if row is None or row.course_id != course_id:
                        raise MediaSourceNotFoundError(source_id)
                    if row.status == "done":
                        raise NoMediaToProcessError(f"media source {source_id} is already done")
                    worklist.append(row.id)
                return worklist

            worklist = list(
                session.execute(
                    select(MediaSource.id)
                    .where(MediaSource.course_id == course_id, MediaSource.status.in_(("detected", "failed")))
                    .order_by(MediaSource.id)
                )
                .scalars()
                .all()
            )
            if not worklist:
                raise NoMediaToProcessError(f"no media sources to process for course {course_id}")
            return worklist

    async def _execute_media(
        self, course_id: int, run_token: int, worklist_ids: list[int], hooks: "_MediaRunHooks"
    ) -> None:
        try:
            # `on_start` sits outside the inner try/except below, same
            # reasoning as `_execute_enrichment`'s: if it itself raises,
            # that's a bug in the hook, not "the job failed" -- there's no
            # counts to report via on_finish, and reconcile_orphaned_rows()
            # below is what catches the row it already created.
            hooks.on_start()
            counts = {"sources": len(worklist_ids), "done": 0, "failed": 0, "captions": 0, "transcribed": 0}
            try:
                for source_id in worklist_ids:
                    await self._process_media_source(course_id, source_id, hooks, counts)
            except Exception as exc:  # noqa: BLE001 -- the job itself crashed; a per-source
                # failure never reaches here (see _process_media_source's own broad
                # except) -- only a bug outside that (e.g. a DB hiccup between
                # sources) lands the batch row as 'failed' rather than 'complete'.
                logger.exception("media run crashed for course %s (run %s)", course_id, run_token)
                hooks.on_finish(counts, "failed", f"internal: {exc}")
            else:
                # Complete even if some sources failed -- failure isolation
                # per source is the whole point; only a crash of the batch
                # itself (handled above) marks the run 'failed'.
                hooks.on_finish(counts, "complete", None)
        except Exception:
            logger.exception("media on_start failed for course %s (run %s)", course_id, run_token)
        finally:
            hooks.reconcile_orphaned_rows()
            self._active.pop(course_id, None)

    async def _process_media_source(
        self, course_id: int, source_id: int, hooks: "_MediaRunHooks", counts: dict
    ) -> None:
        """Fetch (+ transcribe if needed) + ingest ONE source. Never lets an
        exception escape -- `MediaFetchError`/`MediaTranscribeError` and any
        other exception are all handled the same way (status='failed', the
        error recorded, an SSE event emitted) so the batch loop in
        `_execute_media` always continues to the next source. That includes
        the initial status write and the attempt-dir `mkdir` itself (an
        OS-level failure there -- ENOSPC, permission denied on
        `media_dir` -- must strand this one source as 'failed', not crash
        the whole batch and leave the row stuck at 'fetching' forever);
        `attempt_dir`'s Path object is the only thing built outside the
        try, since constructing it is pure string joining with no I/O.
        """
        attempt_dir = self._settings.media_dir / str(source_id) / _attempt_token()
        try:
            self._set_media_source_status(source_id, status="fetching", error=None)
            hooks.emit_source(source_id, "fetching")

            attempt_dir.mkdir(parents=True, exist_ok=True)

            platform, url, passcode = self._read_media_source_spec(source_id)
            spec = FetchSpec(platform=platform, url=url, passcode=passcode, dest_dir=attempt_dir)
            fetch_result = await asyncio.to_thread(self.media_fetcher.fetch, spec)

            if fetch_result.kind == "captions":
                vtt_path = fetch_result.path
            else:
                self._set_media_source_status(source_id, status="transcribing", error=None)
                hooks.emit_source(source_id, "transcribing")
                # Single GPU: `_execute_media` already runs this course's
                # sources one at a time, but two COURSES' media jobs are two
                # independent asyncio tasks with independent `_active`
                # entries, so only this process-wide semaphore keeps their
                # ASR calls from overlapping. Held across the whole
                # to_thread, i.e. for the real duration of the transcription.
                async with _ASR_SEMAPHORE:
                    vtt_path = await asyncio.to_thread(
                        self.transcriber.transcribe, fetch_result.path, attempt_dir
                    )

            # ingest_transcript sets status='done' (and clears error) itself
            # on success -- nothing left for this method to write to the row.
            # counts["captions"]/["transcribed"] are only credited here,
            # alongside counts["done"] -- not right after fetch -- so a
            # source that fetched fine but failed to ingest is counted once,
            # as failed, never double-counted into a captions/transcribed
            # bucket too (keeps done == captions + transcribed always true).
            await asyncio.to_thread(ingest_transcript, self._session_factory, self.blob_store, source_id, vtt_path)
            counts["done"] += 1
            if fetch_result.kind == "captions":
                counts["captions"] += 1
            else:
                counts["transcribed"] += 1
            hooks.emit_source(source_id, "done")
        except (MediaFetchError, MediaTranscribeError) as exc:
            self._set_media_source_status(source_id, status="failed", error=f"{exc.kind}: {exc.user_message}")
            counts["failed"] += 1
            hooks.emit_source(source_id, "failed")
        except Exception as exc:  # noqa: BLE001 -- turned into a failed source, not raised
            logger.exception("media job: source %s crashed unexpectedly (course %s)", source_id, course_id)
            self._set_media_source_status(source_id, status="failed", error=f"internal: {exc}")
            counts["failed"] += 1
            hooks.emit_source(source_id, "failed")
        finally:
            # The transcript material's blob/text are already safely in the
            # blob store by this point (ingest_transcript ran before this
            # finally on the success path) -- the attempt dir only ever held
            # the raw fetched/transcribed working files, safe to discard on
            # success AND on failure, unless the operator opted to keep them
            # (settings.keep_media, e.g. for debugging a failed fetch).
            if not self._settings.keep_media:
                shutil.rmtree(attempt_dir, ignore_errors=True)

    def _read_media_source_spec(self, source_id: int) -> tuple[str, str, str | None]:
        with self._session_factory() as session:
            row = session.get(MediaSource, source_id)
            return row.platform, row.url, row.passcode

    def _set_media_source_status(self, source_id: int, *, status: str, error: str | None) -> None:
        with self._session_factory() as session:
            row = session.get(MediaSource, source_id)
            if row is not None:
                row.status = status
                row.error = error
                row.updated_at = _now_iso()
                session.commit()

    async def wait_media_idle(self, course_id: int) -> None:
        """Await the most recently started media run for `course_id`, if any
        is tracked. Mirrors `wait_idle`/`wait_enrichment_idle`."""
        task = self._media_tasks.get(course_id)
        if task is not None:
            await task
