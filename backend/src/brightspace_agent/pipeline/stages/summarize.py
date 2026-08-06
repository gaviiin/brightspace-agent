"""S1 summarize stage: for one course, extract text for every fetched
material and then produce a cached, structured LLM summary for every
extracted material.

Two passes (`run_summarize_stage`):

1. Extract: `status='fetched'` + `sha256 NOT NULL` -> `extract_text` from the
   blob -> write a text sidecar -> `status='extracted'`. A `None` extraction
   result means `status='failed'`. Materials with no sha256 (links,
   un-uploaded stubs) are skipped entirely -- they're not this stage's job.
2. Summarize: `status='extracted'` + `summary IS NULL` -> check `llm_cache`
   by (sha256, stage, prompt_version, model); a hit reuses the cached
   `DocSummary` JSON with no LLM call, a miss calls the backend and writes
   the cache row -> `status='summarized'`. Extras materials (announcements/
   assignments from the /toc extras path) already land at
   `status='extracted'` with a sidecar written, so they flow into this pass
   the same way as anything the extract pass just produced -- no special
   casing needed.

Both passes fan out across `concurrency` workers (`asyncio.gather` +
`Semaphore`); each worker opens its own session, does its work, and commits,
so no session is ever held open across an `await` (the backend call runs via
`asyncio.to_thread`, since `LLMBackend` is a sync interface).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from importlib import resources

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from brightspace_agent.agents.llm import LLMBackend, LLMCallError, Tier, UsageInfo
from brightspace_agent.agents.schemas import DocSummary
from brightspace_agent.db.models import LlmCache, Material
from brightspace_agent.ingest.extract import extract_text
from brightspace_agent.ingest.store import BlobStore
from brightspace_agent.pipeline.stats import StageStats

logger = logging.getLogger(__name__)

PROMPT_VERSION = "s1.v1"
_STAGE = "summarize"
_TIER: Tier = "fast"
_MAX_CHARS = 12000

_SYSTEM_PROMPT = (
    resources.files("brightspace_agent.agents.prompts").joinpath("summarize.md").read_text(encoding="utf-8")
)

ProgressCallback = Callable[[str], None]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def run_summarize_stage(
    session_factory: sessionmaker[Session],
    blob_store: BlobStore,
    backend: LLMBackend,
    course_id: int,
    *,
    concurrency: int = 4,
    progress: ProgressCallback | None = None,
    cost_cap_usd: float | None = None,
) -> StageStats:
    """`cost_cap_usd` (Task 9's runner-level spend guard): if given, checked
    before every LLM call in pass 2 against `stats.usage_total["est_cost_usd"]`
    -- once that reaches the cap, the remaining worklist is left untouched
    (not marked failed, so a later run retries it) and `stats.aborted` is
    set. Cache hits never count against it (nothing was spent). `None`
    (the default) means uncapped, matching every existing caller.

    The check is optimistic under `concurrency > 1`, not exact (Task 13):
    `cost_lock` (one `threading.Lock` per call to this function, shared by
    every pass-2 worker -- workers run via `asyncio.to_thread`, i.e. real OS
    threads, so an `asyncio.Lock` wouldn't do) keeps the read-then-add on
    `stats.usage_total` internally consistent, but a worker's "check, call
    the LLM, record spend" isn't one atomic unit -- up to `concurrency`
    workers can all see "still under the cap" and start a paid call before
    any of them has recorded its spend. See
    `Settings.max_cost_usd_per_run`'s docstring for the accepted overshoot
    bound this trades for real fan-out throughput.
    """
    stats = StageStats()
    cost_lock = threading.Lock()

    fetched_ids = _select_material_ids(
        session_factory, course_id, status="fetched", require_sha256=True
    )
    await _fan_out(
        fetched_ids,
        concurrency,
        lambda material_id: asyncio.to_thread(
            _extract_one, session_factory, blob_store, material_id, stats, progress
        ),
    )

    extractable_ids = _select_material_ids(
        session_factory, course_id, status="extracted", require_summary_null=True
    )
    await _fan_out(
        extractable_ids,
        concurrency,
        lambda material_id: asyncio.to_thread(
            _summarize_one,
            session_factory,
            blob_store,
            backend,
            material_id,
            stats,
            progress,
            cost_cap_usd,
            cost_lock,
        ),
    )

    return stats


async def _fan_out(material_ids: list[int], concurrency: int, make_awaitable) -> None:
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _run(material_id: int) -> None:
        async with semaphore:
            await make_awaitable(material_id)

    await asyncio.gather(*(_run(material_id) for material_id in material_ids))


def _select_material_ids(
    session_factory: sessionmaker[Session],
    course_id: int,
    *,
    status: str,
    require_sha256: bool = False,
    require_summary_null: bool = False,
) -> list[int]:
    with session_factory() as session:
        stmt = select(Material.id).where(Material.course_id == course_id, Material.status == status)
        if require_sha256:
            stmt = stmt.where(Material.sha256.is_not(None))
        if require_summary_null:
            stmt = stmt.where(Material.summary.is_(None))
        return list(session.execute(stmt).scalars().all())


# --------------------------------------------------------------------------
# Pass 1: extract
# --------------------------------------------------------------------------


def _extract_one(
    session_factory: sessionmaker[Session],
    blob_store: BlobStore,
    material_id: int,
    stats: StageStats,
    progress: ProgressCallback | None,
) -> None:
    with session_factory() as session:
        material = session.get(Material, material_id)
        if material is None or material.status != "fetched" or not material.sha256:
            return  # nothing to do (raced, or no longer eligible)

        blob_path = blob_store.path_for(material.sha256)
        text = extract_text(blob_path, material.mime, material.kind)

        if text is None:
            material.status = "failed"
            material.error = "unsupported-or-unparseable"
            stats.failed += 1
        else:
            blob_store.write_text(material.sha256, text)
            material.status = "extracted"
            material.error = None
            stats.extracted += 1

        session.commit()

    if progress:
        progress(f"extract:{material_id}")


# --------------------------------------------------------------------------
# Pass 2: summarize
# --------------------------------------------------------------------------


def _summarize_one(
    session_factory: sessionmaker[Session],
    blob_store: BlobStore,
    backend: LLMBackend,
    material_id: int,
    stats: StageStats,
    progress: ProgressCallback | None,
    cost_cap_usd: float | None,
    cost_lock: threading.Lock,
) -> None:
    with session_factory() as session:
        material = session.get(Material, material_id)
        if material is None or material.status != "extracted" or material.summary is not None:
            return  # nothing to do (raced, or no longer eligible)

        sha256 = material.sha256
        if not sha256:
            material.status = "failed"
            material.error = "missing-sha256"
            stats.failed += 1
            session.commit()
            if progress:
                progress(f"summarize:failed:{material_id}")
            return

        model = backend.model_for_tier(_TIER)

        cached = session.execute(
            select(LlmCache).where(
                LlmCache.sha256 == sha256,
                LlmCache.stage == _STAGE,
                LlmCache.prompt_version == PROMPT_VERSION,
                LlmCache.model == model,
            )
        ).scalar_one_or_none()

        if cached is not None:
            doc_summary_data = json.loads(cached.output_json)
            usage: UsageInfo = {"model": model, "input_tokens": 0, "output_tokens": 0, "est_cost_usd": 0.0}
            _apply_summary(material, doc_summary_data, model, usage)
            stats.cached_hits += 1
            stats.summarized += 1
            session.commit()
            if progress:
                progress(f"summarize:cached:{material_id}")
            return

        # Cost cap (Task 9; optimistic under concurrency > 1 since Task 13 --
        # see run_summarize_stage's docstring): checked here, right before
        # the only paid call in this function -- a cache hit above never
        # reaches this point, so it never counts against the cap. Once the
        # running total reaches the cap, this material (and the rest of the
        # worklist, as later workers make the same check) is left exactly
        # as it is -- still `status='extracted'` -- so a later run retries
        # it. `cost_lock` only guards the read here; it is not held across
        # the LLM call below, which is the whole reason this is optimistic
        # rather than exact.
        if cost_cap_usd is not None:
            with cost_lock:
                cap_reached = stats.usage_total["est_cost_usd"] >= cost_cap_usd
            if cap_reached:
                stats.aborted = True
                if progress:
                    progress(f"summarize:cost-cap:{material_id}")
                return

        text = blob_store.read_text(sha256) or ""
        user_prompt = _build_user_prompt(material, text)

        try:
            parsed, usage = backend.structured_call(
                DocSummary, system=_SYSTEM_PROMPT, user=user_prompt, tier=_TIER
            )
        except LLMCallError as exc:
            material.status = "failed"
            material.error = f"llm-error: {exc}"
            stats.failed += 1
            session.commit()
            if progress:
                progress(f"summarize:failed:{material_id}")
            return

        doc_summary_data = parsed.model_dump()
        _apply_summary(material, doc_summary_data, model, usage)

        # Cache-race guard: two workers summarizing materials with identical
        # bytes both miss the cache above and both call the LLM: the second
        # write here reaches a sha256/stage/prompt_version/model that the
        # first has already inserted. An upsert (rather than a plain insert)
        # treats that as a harmless duplicate instead of raising
        # IntegrityError and crashing the whole stage over a healthy
        # material -- the first writer's cached row wins, which is fine,
        # since both were computed from byte-identical input.
        session.execute(
            sqlite_insert(LlmCache)
            .values(
                sha256=sha256,
                stage=_STAGE,
                prompt_version=PROMPT_VERSION,
                model=model,
                output_json=json.dumps(doc_summary_data),
                created_at=_now_iso(),
            )
            .on_conflict_do_nothing(index_elements=["sha256", "stage", "prompt_version", "model"])
        )
        stats.summarized += 1
        with cost_lock:
            stats.add_usage(usage)
        session.commit()

    if progress:
        progress(f"summarize:{material_id}")


def _build_user_prompt(material: Material, text: str) -> str:
    return (
        f"Title: {material.title}\n"
        f"Kind: {material.kind}\n"
        "Material text:\n"
        f"{text[:_MAX_CHARS]}"
    )


def _apply_summary(material: Material, doc_summary_data: dict, model: str, usage: UsageInfo) -> None:
    material.summary = doc_summary_data["summary"]
    material.summary_meta_json = json.dumps(
        {
            "model": model,
            "prompt_version": PROMPT_VERSION,
            "key_terms": doc_summary_data["key_terms"],
            "doc_kind_guess": doc_summary_data["doc_kind_guess"],
            "usage": usage,
        }
    )
    material.status = "summarized"
    material.error = None
