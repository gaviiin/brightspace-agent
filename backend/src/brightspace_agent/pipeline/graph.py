"""The LangGraph `StateGraph` that orchestrates the four pipeline stages.

Design note -- why a graph at stage granularity, not the Send API:
`StateGraph` here orchestrates at *stage* granularity (summarize -> taxonomy
-> classify -> assemble) with conditional routing on failure/cost-cap. Each
stage already fans out internally across its own materials via
`asyncio.gather` + a `Semaphore` (see pipeline/stages/*.py) -- that per-item
concurrency is a stage concern, not a graph concern. LangGraph's Send API
(https://langchain-ai.github.io/langgraph/concepts/low_level/#send) exists
for exactly the alternative shape -- a map-reduce graph where the GRAPH
itself fans out one node invocation per item (e.g. `Send("summarize_one",
{"material_id": mid})` for every material) and a reducer gathers results
back into shared state. That shape would let the graph observe and control
per-*material* progress (retries, partial cancellation, per-item
checkpointing) rather than only per-stage progress, at the cost of pulling
each stage's internal fan-out logic up into the graph. Not needed today
(stage-level progress is what the runner's SSE events and pipeline_runs
rows report), but worth remembering if a future task wants finer-grained
progress or per-material resumability.

This module is the only thing in the pipeline package that imports
`langgraph` (see the Task 9 brief) -- pipeline/runner.py drives the compiled
graph but knows nothing about LangGraph's API surface itself.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from typing import Any, Protocol, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from sqlalchemy.orm import Session, sessionmaker

from brightspace_agent.agents.llm import LLMBackend
from brightspace_agent.config import Settings
from brightspace_agent.graph.build import build_graph
from brightspace_agent.ingest.store import BlobStore
from brightspace_agent.pipeline.stages.classify import run_classify_stage
from brightspace_agent.pipeline.stages.summarize import run_summarize_stage
from brightspace_agent.pipeline.stages.taxonomy import run_taxonomy_stage

logger = logging.getLogger(__name__)

# summarize/classify fan out across `_CAPPED_STAGE_CONCURRENCY` materials at
# once. Task 9 originally pinned this to 1 for an exact, race-free cost cap
# ("check the running total, then call the LLM" has to happen strictly one
# material at a time, or several concurrent workers can all pass the check
# before any of them has recorded its spend) -- at the cost of ~4x slower
# summarize/classify stages on a large course, since every LLM call became
# fully sequential regardless of the cap actually binding on a given run.
#
# Task 13 restores real fan-out and switches the cap to optimistic: each
# stage's per-item worker still checks the running total before its one paid
# call (now behind a `threading.Lock` shared across that stage run's workers
# -- see pipeline/stages/summarize.py's/classify.py's `cost_lock` param --
# so the check-then-record pair is internally consistent, no lost updates),
# but the check and the record are no longer one atomic unit spanning the
# LLM call itself. Up to `_CAPPED_STAGE_CONCURRENCY` workers can therefore
# all see "still under the cap" and start a paid call before any of them has
# recorded its spend -- overshoot is bounded by
# `_CAPPED_STAGE_CONCURRENCY x one call's cost`. See
# Settings.max_cost_usd_per_run's docstring for why that bound is an
# acceptable trade-off for a background job with a single local user.
# Direct callers of run_summarize_stage/run_classify_stage are unaffected --
# this concurrency choice is local to the graph nodes below.
_CAPPED_STAGE_CONCURRENCY = 4


class PipelineState(TypedDict):
    course_id: int
    stage_stats: dict[str, dict]  # stage name -> StageStats-as-dict
    error: str | None


@dataclass
class PipelineDeps:
    """Static, app-lifetime dependencies the compiled graph closes over.

    Per-run/per-course values (course_id, which stages were requested, the
    cost-cap hooks) flow through LangGraph's `config["configurable"]`
    instead, via `StageHooks` below -- that's what lets one compiled graph
    (built once, see PipelineRunner.__init__) serve every course's runs.
    """

    session_factory: sessionmaker[Session]
    blob_store: BlobStore
    backend: LLMBackend
    settings: Settings


class StageHooks(Protocol):
    """What a node needs from the runner for one run: DB rows + SSE events
    per stage start/finish, and the remaining cost-cap budget. Optional --
    nodes fall back to `_NULL_HOOKS` (no-op, no cap) when a caller (e.g. a
    graph-level test) invokes the compiled graph directly, without a
    PipelineRunner in the loop.
    """

    def on_start(self, stage: str) -> None: ...

    def on_finish(self, stage: str, stats: dict, status: str, error: str | None) -> None: ...

    def remaining_budget(self) -> float | None:
        """Cost-cap budget remaining for the *next* capped stage call, or
        `None` for uncapped. Prior stages' spend (already reported via
        `on_finish`) is expected to have reduced this."""
        ...


class _NullHooks:
    def on_start(self, stage: str) -> None:
        return None

    def on_finish(self, stage: str, stats: dict, status: str, error: str | None) -> None:
        return None

    def remaining_budget(self) -> float | None:
        return None


_NULL_HOOKS = _NullHooks()


def _configurable(config: RunnableConfig | None) -> dict[str, Any]:
    return (config or {}).get("configurable", {}) or {}


def _hooks(config: RunnableConfig | None) -> StageHooks:
    return _configurable(config).get("hooks") or _NULL_HOOKS


def _requested_stages(config: RunnableConfig | None) -> set[str] | None:
    return _configurable(config).get("requested_stages")


def _force_taxonomy(config: RunnableConfig | None) -> bool:
    """Per-run opt-in to re-proposing over a user-edited taxonomy (see
    run_taxonomy_stage's `force`). Absent -> False, so every existing
    caller -- including a graph-level test invoking the compiled graph
    directly -- keeps the safe default."""
    return bool(_configurable(config).get("force_taxonomy"))


def build_pipeline_graph(deps: PipelineDeps):
    """Compile the four-stage graph once; every run reuses this same
    compiled object (course_id and per-run hooks travel through the
    `initial_state` / `config` arguments to `.astream()`/`.ainvoke()`)."""

    # Node `config` params are deliberately left unannotated: LangGraph
    # inspects each node's signature at add_node() time and (with
    # `from __future__ import annotations` active, as here) sees a *string*
    # annotation rather than the `RunnableConfig` type object it compares
    # against, and warns on every string form -- including the two it just
    # suggested. An explicit `RunnableConfig | None` annotation is what
    # callers should read; runtime typing is `RunnableConfig | None`.
    async def summarize_node(state: PipelineState, config=None) -> dict:
        stage = "summarize"
        hooks = _hooks(config)
        requested = _requested_stages(config)
        if requested is not None and stage not in requested:
            return {}
        hooks.on_start(stage)
        try:
            stats = await run_summarize_stage(
                deps.session_factory,
                deps.blob_store,
                deps.backend,
                state["course_id"],
                concurrency=_CAPPED_STAGE_CONCURRENCY,
                cost_cap_usd=hooks.remaining_budget(),
            )
        except Exception as exc:  # noqa: BLE001 -- turned into graph state, not raised
            logger.exception("summarize stage crashed for course %s", state["course_id"])
            hooks.on_finish(stage, {}, "failed", str(exc))
            return {"error": f"{stage}: {exc}"}
        stats_dict = asdict(stats)
        status = "aborted" if stats_dict.get("aborted") else "complete"
        hooks.on_finish(stage, stats_dict, status, "cost-cap" if status == "aborted" else None)
        return {"stage_stats": {**state["stage_stats"], stage: stats_dict}}

    async def taxonomy_node(state: PipelineState, config=None) -> dict:
        stage = "taxonomy"
        hooks = _hooks(config)
        requested = _requested_stages(config)
        if requested is not None and stage not in requested:
            return {}
        hooks.on_start(stage)
        try:
            stats = await run_taxonomy_stage(
                deps.session_factory,
                deps.backend,
                state["course_id"],
                blob_store=deps.blob_store,
                force=_force_taxonomy(config),
            )
        except Exception as exc:  # noqa: BLE001 -- includes TaxonomyStageError (too-small proposal)
            logger.warning("taxonomy stage failed for course %s: %s", state["course_id"], exc)
            hooks.on_finish(stage, {}, "failed", str(exc))
            return {"error": f"{stage}: {exc}"}
        stats_dict = asdict(stats)
        hooks.on_finish(stage, stats_dict, "complete", None)
        return {"stage_stats": {**state["stage_stats"], stage: stats_dict}}

    async def classify_node(state: PipelineState, config=None) -> dict:
        stage = "classify"
        hooks = _hooks(config)
        requested = _requested_stages(config)
        if requested is not None and stage not in requested:
            return {}
        hooks.on_start(stage)
        try:
            stats = await run_classify_stage(
                deps.session_factory,
                deps.backend,
                state["course_id"],
                concurrency=_CAPPED_STAGE_CONCURRENCY,
                cost_cap_usd=hooks.remaining_budget(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("classify stage crashed for course %s", state["course_id"])
            hooks.on_finish(stage, {}, "failed", str(exc))
            return {"error": f"{stage}: {exc}"}
        stats_dict = asdict(stats)
        status = "aborted" if stats_dict.get("aborted") else "complete"
        hooks.on_finish(stage, stats_dict, status, "cost-cap" if status == "aborted" else None)
        return {"stage_stats": {**state["stage_stats"], stage: stats_dict}}

    async def assemble_node(state: PipelineState, config=None) -> dict:
        # Persists nothing new -- just validates build_graph runs cleanly
        # (its own internal invariant checks would raise otherwise) and
        # records a few counts. Always attempted regardless of `stages`
        # filtering: it's free (no LLM call) and is the run's final
        # consistency check.
        stage = "assemble"
        hooks = _hooks(config)
        hooks.on_start(stage)
        try:
            course_graph = await asyncio.to_thread(_build_graph_sync, deps.session_factory, state["course_id"])
        except Exception as exc:  # noqa: BLE001
            logger.exception("assemble step crashed for course %s", state["course_id"])
            hooks.on_finish(stage, {}, "failed", str(exc))
            return {"error": f"{stage}: {exc}"}
        stats_dict = {
            "topics": len(course_graph["topics"]),
            "materials": len(course_graph["materials"]),
            "orphan_count": course_graph["meta"]["orphanCount"],
            "usage_total": {"input_tokens": 0, "output_tokens": 0, "est_cost_usd": 0.0},
        }
        hooks.on_finish(stage, stats_dict, "complete", None)
        return {"stage_stats": {**state["stage_stats"], stage: stats_dict}}

    def after_summarize(state: PipelineState) -> str:
        if state.get("error"):
            return END
        if state["stage_stats"].get("summarize", {}).get("aborted"):
            # Cost cap hit: never proceed to a later LLM stage (taxonomy,
            # classify), but assemble is free (no LLM call) and still worth
            # running -- it's the run's final consistency check.
            return "assemble"
        return "taxonomy"

    def after_taxonomy(state: PipelineState) -> str:
        if state.get("error"):
            # Never classify against a junk/absent taxonomy: an absent
            # topic map means classify's own no-op guard would fire, but a
            # thrown TaxonomyStageError never got that far.
            return END
        return "classify"

    def after_classify(state: PipelineState) -> str:
        if state.get("error"):
            return END
        return "assemble"

    graph = StateGraph(PipelineState)
    graph.add_node("summarize", summarize_node)
    graph.add_node("taxonomy", taxonomy_node)
    graph.add_node("classify", classify_node)
    graph.add_node("assemble", assemble_node)
    graph.set_entry_point("summarize")
    graph.add_conditional_edges(
        "summarize", after_summarize, {"taxonomy": "taxonomy", "assemble": "assemble", END: END}
    )
    graph.add_conditional_edges("taxonomy", after_taxonomy, {"classify": "classify", END: END})
    graph.add_conditional_edges("classify", after_classify, {"assemble": "assemble", END: END})
    graph.add_edge("assemble", END)
    return graph.compile()


def _build_graph_sync(session_factory: sessionmaker[Session], course_id: int) -> dict:
    with session_factory() as session:
        return build_graph(session, course_id)
