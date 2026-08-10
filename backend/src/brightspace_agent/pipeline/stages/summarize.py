"""S1 summarize stage: for one course, extract text for every fetched
material and then produce a cached, structured LLM summary for every
extracted material.

Three passes (`run_summarize_stage`):

1. Extract: `status='fetched'` + `sha256 NOT NULL` -> `extract_text` from the
   blob -> write a text sidecar -> `status='extracted'`. A `None` extraction
   result means `status='failed'`. Materials with no sha256 (links,
   un-uploaded stubs) are skipped entirely -- they're not this stage's job.
2. Summarize: `status='extracted'` + `summary IS NULL` -> check `llm_cache`
   by (sha256, stage, prompt_version, model); a hit whose JSON still
   validates as a `DocSummary` is reused with no LLM call, anything else
   (unparseable or off-schema) counts as a miss and gets overwritten by the
   fresh answer -- see `_read_cache`. A miss calls the backend and writes
   the cache row -> `status='summarized'`. Extras materials (announcements/
   assignments from the /toc extras path) already land at
   `status='extracted'` with a sidecar written, so they flow into this pass
   the same way as anything the extract pass just produced -- no special
   casing needed.
3. Metadata pseudo-document (M3.5a): `status='fetched'` materials pass 1
   never resolves -- no sha256 at all, most commonly a link -- get
   summarized anyway, from a deterministic `Title:`/`Kind:`/`Module:`/`URL:`
   string built from the row itself rather than extracted text. See
   `_promote_metadata_one` for why this never touches `material.sha256`,
   and `_select_metadata_material_ids` for why "has bytes but no sidecar"
   is deliberately not this pass's problem.

All three passes fan out across `concurrency` workers (`asyncio.gather` +
`Semaphore`); each worker opens its own session, does its work, and commits,
so no session is ever held open across an `await` (the backend call runs via
`asyncio.to_thread`, since `LLMBackend` is a sync interface).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from importlib import resources

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from brightspace_agent.agents.llm import LLMBackend, LLMCallError, Tier, UsageInfo
from brightspace_agent.agents.schemas import DocSummary
from brightspace_agent.db.models import LlmCache, Material, Module
from brightspace_agent.ingest.extract import extract_text
from brightspace_agent.ingest.store import BlobStore
from brightspace_agent.pipeline.stats import StageStats

logger = logging.getLogger(__name__)

PROMPT_VERSION = "s1.v2"  # M3.5a: adds the metadata pseudo-document pass (3)
_STAGE = "summarize"
_TIER: Tier = "fast"
_MAX_CHARS = 12000

# M3.5a: kinds eligible for pass 3 (metadata pseudo-document). Every kind
# that can legitimately have no extractable file text: a link has no blob at
# all, and assignment/announcement/other cover extras or file-less stubs.
# 'syllabus'/'slides'/'document'/'video'/'transcript' are deliberately
# excluded -- those are expected to have real bytes, and a material of one
# of those kinds still stuck at 'fetched' with no sha256 is a genuine gap
# worth surfacing as failed/never-fetched, not papering over with a
# title-only summary.
#
# Public (not `_`-prefixed) because api/pipeline.py's `_dry_run_counts`
# reads it to count pass 3's calls: the dry-run estimate and the stage have
# to agree on which materials pass 3 will pick up, and one shared tuple is
# the only way that stays true when this list changes.
METADATA_KINDS = ("link", "assignment", "announcement", "other")

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

    # Pass 3 (M3.5a): materials pass 1 never touches -- no sha256 at all, most
    # commonly a link -- get a fair shot at classification via a metadata-only
    # pseudo-document instead of sitting at 'fetched' forever. See
    # `_promote_metadata_one`.
    metadata_ids = _select_metadata_material_ids(session_factory, course_id)
    await _fan_out(
        metadata_ids,
        concurrency,
        lambda material_id: asyncio.to_thread(
            _promote_metadata_one,
            session_factory,
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

        cached = _read_cache(session, sha256, model)

        if cached is not None:
            usage: UsageInfo = {"model": model, "input_tokens": 0, "output_tokens": 0, "est_cost_usd": 0.0}
            _apply_summary(material, cached.model_dump(), model, usage)
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
        _upsert_cache_row(session, sha256, model, doc_summary_data)
        stats.summarized += 1
        with cost_lock:
            stats.add_usage(usage)
        session.commit()

    if progress:
        progress(f"summarize:{material_id}")


def _upsert_cache_row(session: Session, sha256: str, model: str, doc_summary_data: dict) -> None:
    """Write (or replace) `sha256`'s llm_cache row for this stage.

    Upsert, for two reasons (same pair as classify.py's `_write_cache`):

    1. Cache race: two workers summarizing content with identical bytes (or,
       for pass 3, an identical pseudo-document) both miss the cache and
       both call the LLM, so the second write here reaches a
       sha256/stage/prompt_version/model the first has already inserted.
       Without an upsert that's an IntegrityError crashing the whole stage
       over a healthy material. Both answers were computed from
       byte-identical input, so either winning is fine.
    2. A row `_read_cache` rejected as unusable has to be replaceable.
       `do_nothing` would leave the bad row there forever, and the stage
       would pay for a fresh call on every single run.

    Shared by pass 2 (`_summarize_one`) and pass 3 (`_promote_metadata_one`)
    -- both key `llm_cache` the same way, the only difference being what
    `sha256` stands for (a real file's content hash vs. a pseudo-document's).
    """
    output_json = json.dumps(doc_summary_data)
    created_at = _now_iso()
    session.execute(
        sqlite_insert(LlmCache)
        .values(
            sha256=sha256,
            stage=_STAGE,
            prompt_version=PROMPT_VERSION,
            model=model,
            output_json=output_json,
            created_at=created_at,
        )
        .on_conflict_do_update(
            index_elements=["sha256", "stage", "prompt_version", "model"],
            set_={"output_json": output_json, "created_at": created_at},
        )
    )


def _read_cache(session: Session, sha256: str, model: str) -> DocSummary | None:
    """The cached summary, or None if there isn't a usable one.

    Validation happens *here*, not at the point of use: a row that is
    truncated JSON, or valid JSON that no longer matches `DocSummary` (an
    older prompt version's shape, a hand-edited row, a half-written file),
    would otherwise raise inside the worker on every run forever -- and
    since one poisoned row fails the whole stage, one bad material would
    wedge the entire course's pipeline. Any unusable row is treated as a
    miss, and the fresh answer overwrites it (the write below is an
    upsert). Mirrors classify.py's `_read_cache`.
    """
    row = session.execute(
        select(LlmCache).where(
            LlmCache.sha256 == sha256,
            LlmCache.stage == _STAGE,
            LlmCache.prompt_version == PROMPT_VERSION,
            LlmCache.model == model,
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    try:
        return DocSummary.model_validate(json.loads(row.output_json))
    except (json.JSONDecodeError, ValidationError) as exc:
        logger.warning(
            "summarize: ignoring unusable cache row for %s (%s); re-asking the model", sha256[:12], exc
        )
        return None


def _build_user_prompt(material: Material, text: str) -> str:
    return (
        f"Title: {material.title}\n"
        f"Kind: {material.kind}\n"
        "Material text:\n"
        f"{text[:_MAX_CHARS]}"
    )


def _apply_summary(material: Material, doc_summary_data: dict, model: str, usage: UsageInfo) -> None:
    """`doc_summary_data` is always a `DocSummary.model_dump()` -- either
    from a fresh call or from a cache row `_read_cache` already validated --
    so the direct key access below cannot KeyError on a malformed row."""
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


# --------------------------------------------------------------------------
# Pass 3 (M3.5a): metadata pseudo-document for materials with no text
# --------------------------------------------------------------------------


def _select_metadata_material_ids(
    session_factory: sessionmaker[Session], course_id: int
) -> list[int]:
    """`status='fetched'`, kind in `METADATA_KINDS`, and no sha256 at all --
    a link never gets a blob, and a file-less stub hasn't been uploaded yet.

    `sha256 IS NULL` is the whole rule on purpose. A material that HAS bytes
    but no text sidecar is pass 1's territory (this stage resolves every
    fetched+sha256 material to 'extracted' or 'failed' before pass 3 runs);
    summarizing one here would write a title-only guess into `summary` as if
    it were the real document's, while `compute_needed` -- which keys re-fetch
    off `sha256 IS NULL` -- sees nothing missing and never repairs the sidecar.
    """
    with session_factory() as session:
        return list(
            session.execute(
                select(Material.id).where(
                    Material.course_id == course_id,
                    Material.status == "fetched",
                    Material.sha256.is_(None),
                    Material.kind.in_(METADATA_KINDS),
                )
            ).scalars().all()
        )


def _build_pseudo_doc(material: Material, module_title: str | None) -> str:
    """A deterministic stand-in for "the material's text", built entirely
    from metadata already on the row (title/kind/module/URL) -- no blob, no
    extraction. Deterministic in both directions: the exact same material
    always renders the exact same string (stable cache key), and changing
    any one field (a retitled link, a moved module) changes it too.
    """
    return (
        f"Title: {material.title}\n"
        f"Kind: {material.kind}\n"
        f"Module: {module_title or '(none)'}\n"
        f"URL: {material.source_url or '(none)'}"
    )


def _module_title(session: Session, module_id: int | None) -> str | None:
    if module_id is None:
        return None
    module = session.get(Module, module_id)
    return module.title if module is not None else None


def _promote_metadata_one(
    session_factory: sessionmaker[Session],
    backend: LLMBackend,
    material_id: int,
    stats: StageStats,
    progress: ProgressCallback | None,
    cost_cap_usd: float | None,
    cost_lock: threading.Lock,
) -> None:
    """Summarize one metadata-only material straight from 'fetched' to
    'summarized' -- there is no 'extracted' step here, since there's no text
    to extract. Otherwise a near-mirror of `_summarize_one`: same cache
    read/write, same cost-cap check, same failure handling. The one real
    difference is the cache key: with no file sha256 to key on, the
    pseudo-document's own sha256 (`_build_pseudo_doc` -> hashed here) stands
    in for it in the `llm_cache` row this writes/reads.

    Deliberately leaves `material.sha256` itself as `None` -- it is NOT set
    to the pseudo-doc hash. A material eligible for this pass can be kind
    'other' from a real File-type ToC entry whose upload just hasn't
    happened yet (a transient network error, a sync that's still catching
    up): `ingest/diff.py`'s `compute_needed` treats `sha256 IS NULL` as "this
    file is still needed" specifically so a failed/pending upload always
    gets retried. Writing a pseudo-doc hash into that column would silence
    that signal forever -- the real content would never be re-fetched, and
    this metadata-only guess would masquerade as the finished article. When
    real content DOES arrive later, `upsert_file_material`'s own
    sha-changed check (`None` -> a real hash is always "changed") already
    resets the material back through the normal pipeline with no help
    needed from this stage. The cost: S3's classify-stage cache is skipped
    for these materials (its own `if sha256:` guards already handle a `None`
    sha256 as a plain cache miss, unconditionally caching nothing -- no new
    code needed there either), which is the trade this stage makes for not
    quietly breaking re-fetch.
    """
    with session_factory() as session:
        material = session.get(Material, material_id)
        if (
            material is None
            or material.status != "fetched"
            or material.kind not in METADATA_KINDS
        ):
            return  # raced, or no longer eligible

        pseudo_doc = _build_pseudo_doc(material, _module_title(session, material.module_id))
        pseudo_sha = hashlib.sha256(pseudo_doc.encode("utf-8")).hexdigest()
        model = backend.model_for_tier(_TIER)

        cached = _read_cache(session, pseudo_sha, model)

        if cached is not None:
            usage: UsageInfo = {"model": model, "input_tokens": 0, "output_tokens": 0, "est_cost_usd": 0.0}
            _apply_summary(material, cached.model_dump(), model, usage)
            stats.cached_hits += 1
            stats.summarized += 1
            session.commit()
            if progress:
                progress(f"summarize:cached:{material_id}")
            return

        # Cost cap: same optimistic-under-concurrency check as pass 2 (see
        # run_summarize_stage's docstring) -- checked right before the only
        # paid call here, so a cache hit above never trips it.
        if cost_cap_usd is not None:
            with cost_lock:
                cap_reached = stats.usage_total["est_cost_usd"] >= cost_cap_usd
            if cap_reached:
                stats.aborted = True
                if progress:
                    progress(f"summarize:cost-cap:{material_id}")
                return

        try:
            parsed, usage = backend.structured_call(
                DocSummary, system=_SYSTEM_PROMPT, user=pseudo_doc, tier=_TIER
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
        _upsert_cache_row(session, pseudo_sha, model, doc_summary_data)
        stats.summarized += 1
        with cost_lock:
            stats.add_usage(usage)
        session.commit()

    if progress:
        progress(f"summarize:{material_id}")
