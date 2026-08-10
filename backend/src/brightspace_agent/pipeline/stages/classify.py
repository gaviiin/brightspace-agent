"""S3 classify stage: for every summarized material with no assignment at the
course's current taxonomy version, ask a cheap model which topics it belongs
to, and write the resulting `material_topics` rows.

Shape (same fan-out pattern as S1): the taxonomy is loaded once and rendered
into a numbered block that every prompt shares; the worklist is a DB query
for materials lacking rows at the current version, so a re-run after a crash,
a new sync, or a taxonomy bump naturally picks up exactly what is missing.
Each worker opens its own session inside `asyncio.to_thread`, does one
material, and commits.

Two things worth knowing:

- **Failures are per-item.** A model error on one material logs, counts, and
  moves on -- the material keeps `status='summarized'` so the next run
  retries it, and nothing is cached. One bad document must not abort a
  course.
- **The cache key carries the taxonomy's content**, not its version number:
  `(material.sha256, 'classify', "s3.vN:<taxonomy digest>", model)`. Changing
  the taxonomy re-classifies everything, as it must; renumbering it does not.
  Since `material.sha256` is content-addressed and therefore shared across
  courses, the digest is also what stops two courses that happen to hold the
  same file from reading each other's answers.

M3.5a: the model also returns `is_administrative` per material (grades,
scheduling, office hours, logistics -- never course content). A material the
model marks administrative gets the flag written and NO `material_topics`
rows, regardless of what `assignments` it also returned -- S4 (graph/build.py)
files it under its own "Logistics & admin" bucket instead of a real topic or
Unsorted. The flag isn't versioned by taxonomy (materials.is_administrative is
a single column), but it's re-derived every time this stage actually
processes the material -- which, since an administrative material never gets
material_topics rows, is every run until reclassified into real content (see
`_select_worklist`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import resources

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from brightspace_agent.agents.llm import LLMBackend, Tier
from brightspace_agent.agents.promptfmt import (
    SECTION_COURSE_TOPICS,
    SECTION_MATERIAL,
    render_topic_block,
    slugify,
    taxonomy_digest,
)
from brightspace_agent.agents.schemas import ClassificationOut
from brightspace_agent.db.models import Course, LlmCache, Material, MaterialTopic, Topic
from brightspace_agent.pipeline.stats import StageStats

logger = logging.getLogger(__name__)

PROMPT_VERSION = "s3.v2"  # M3.5a: adds is_administrative to the output schema
_STAGE = "classify"
_TIER: Tier = "fast"
_MAX_ASSIGNMENTS = 3  # classify.md asks for 1-3; this is what enforces it

_SYSTEM_PROMPT = (
    resources.files("brightspace_agent.agents.prompts").joinpath("classify.md").read_text(encoding="utf-8")
)

ProgressCallback = Callable[[str], None]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class _TaxonomyContext:
    version: int
    block: str  # the numbered topic list shown in every prompt
    topic_ids: dict[str, int]  # slug -> topics.id at `version`
    cache_prompt_version: str


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


async def run_classify_stage(
    session_factory: sessionmaker[Session],
    backend: LLMBackend,
    course_id: int,
    *,
    concurrency: int = 4,
    progress: ProgressCallback | None = None,
    cost_cap_usd: float | None = None,
) -> StageStats:
    """`cost_cap_usd` (Task 9's runner-level spend guard): if given, checked
    before every LLM call against `stats.usage_total["est_cost_usd"]` -- once
    that reaches the cap, the remaining worklist is left untouched (still
    `status='summarized'`, so a later run retries it) and `stats.aborted` is
    set. Cache hits never count against it. `None` (the default) means
    uncapped, matching every existing caller.

    The check is optimistic under `concurrency > 1`, not exact (Task 13):
    `cost_lock` (one `threading.Lock` per call to this function, shared by
    every worker -- workers run via `asyncio.to_thread`, i.e. real OS
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

    context = _load_taxonomy_context(session_factory, course_id)
    if context is None:
        logger.warning(
            "classify: course %s has no topics at its current taxonomy version; nothing to classify "
            "against (run the taxonomy stage first)",
            course_id,
        )
        return stats
    stats.taxonomy_version = context.version

    material_ids = _select_worklist(session_factory, course_id, context.version)
    if not material_ids:
        logger.info("classify: course %s has nothing to classify at version %d", course_id, context.version)
        return stats

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _run_one(material_id: int) -> None:
        async with semaphore:
            await asyncio.to_thread(
                _classify_one,
                session_factory,
                backend,
                context,
                material_id,
                stats,
                progress,
                cost_cap_usd,
                cost_lock,
            )

    await asyncio.gather(*(_run_one(material_id) for material_id in material_ids))
    return stats


def _load_taxonomy_context(
    session_factory: sessionmaker[Session], course_id: int
) -> _TaxonomyContext | None:
    with session_factory() as session:
        course = session.get(Course, course_id)
        if course is None:
            raise ValueError(f"no course with id {course_id}")
        version = course.taxonomy_version
        topics = list(
            session.execute(
                select(Topic)
                .where(Topic.course_id == course_id, Topic.taxonomy_version == version)
                .order_by(Topic.order_index, Topic.id)
            ).scalars().all()
        )
        if not topics:
            return None

        block = render_topic_block(
            (topic.slug, topic.name, topic.description) for topic in topics
        )
        return _TaxonomyContext(
            version=version,
            block=block,
            topic_ids={topic.slug: topic.id for topic in topics},
            # Content, not version number: a re-proposed but identical
            # taxonomy must not re-bill a 200-material course.
            cache_prompt_version=f"{PROMPT_VERSION}:{taxonomy_digest(block)}",
        )


def _select_worklist(
    session_factory: sessionmaker[Session], course_id: int, version: int
) -> list[int]:
    """Summarized materials with no `material_topics` row at `version`."""
    already_assigned = (
        select(MaterialTopic.id)
        .where(
            MaterialTopic.material_id == Material.id,
            MaterialTopic.taxonomy_version == version,
        )
        .exists()
    )
    with session_factory() as session:
        return list(
            session.execute(
                select(Material.id)
                .where(
                    Material.course_id == course_id,
                    Material.status == "summarized",
                    ~already_assigned,
                )
                .order_by(Material.id)
            ).scalars().all()
        )


# --------------------------------------------------------------------------
# One material
# --------------------------------------------------------------------------


def _classify_one(
    session_factory: sessionmaker[Session],
    backend: LLMBackend,
    context: _TaxonomyContext,
    material_id: int,
    stats: StageStats,
    progress: ProgressCallback | None,
    cost_cap_usd: float | None,
    cost_lock: threading.Lock,
) -> None:
    try:
        with session_factory() as session:
            material = session.get(Material, material_id)
            if material is None or material.status != "summarized":
                return  # raced, or no longer eligible
            if _has_assignments(session, material_id, context.version):
                return  # another worker/run got here first

            model = backend.model_for_tier(_TIER)
            sha256 = material.sha256
            classification = _read_cache(session, sha256, context, model) if sha256 else None
            from_cache = classification is not None

            if classification is None:
                # Cost cap (Task 9; optimistic under concurrency > 1 since
                # Task 13 -- see run_classify_stage's docstring): checked
                # right before the only paid call in this function, so a
                # cache hit above never trips it. Once the running total
                # reaches the cap, this material (and the rest of the
                # worklist, as later workers make the same check) is left
                # exactly as it is -- still `status='summarized'` -- so a
                # later run retries it. `cost_lock` only guards the read
                # here, not the LLM call below -- see run_classify_stage's
                # docstring above for why that's optimistic rather than
                # exact.
                if cost_cap_usd is not None:
                    with cost_lock:
                        cap_reached = stats.usage_total["est_cost_usd"] >= cost_cap_usd
                    if cap_reached:
                        stats.aborted = True
                        if progress:
                            progress(f"classify:cost-cap:{material_id}")
                        return
                user_prompt = _build_user_prompt(material, context)
                parsed, usage = backend.structured_call(
                    ClassificationOut, system=_SYSTEM_PROMPT, user=user_prompt, tier=_TIER
                )
                classification = parsed
                with cost_lock:
                    stats.add_usage(usage)
                if sha256:
                    _write_cache(session, sha256, context, model, classification)
            else:
                stats.cached_hits += 1

            # M3.5a: administrative materials (grades, scheduling, office
            # hours, logistics) get the flag and NO material_topics rows --
            # S4 files them under their own bucket instead of a real topic
            # or Unsorted. `material.is_administrative` is always written
            # explicitly (both branches), not just when true, so a material
            # that used to be administrative and is re-classified as real
            # content has its stale flag cleared right here, the same run
            # that gives it real assignments.
            if classification.is_administrative:
                material.is_administrative = 1
                assignments: list[tuple[int, float, str]] = []
            else:
                material.is_administrative = 0
                assignments = _validate(classification, context, material_id)
                for topic_id, confidence, rationale in assignments:
                    session.add(
                        MaterialTopic(
                            material_id=material_id,
                            topic_id=topic_id,
                            taxonomy_version=context.version,
                            confidence=confidence,
                            rationale=rationale,
                            method="llm",
                            review_status="auto",
                        )
                    )
            session.commit()

        if assignments:
            stats.classified += 1
            stats.assignments += len(assignments)
        elif classification.is_administrative:
            stats.unassigned += 1
            logger.info(
                "classify: material %s is administrative; filed in the logistics/admin bucket",
                material_id,
            )
        else:
            stats.unassigned += 1
            logger.info(
                "classify: material %s matched no topic at version %d; it will show as unsorted",
                material_id, context.version,
            )
    except Exception as exc:  # noqa: BLE001 -- one bad material must not abort the course
        stats.failed += 1
        logger.warning(
            "classify: material %s failed (%s: %s); leaving it summarized for a later retry",
            material_id, type(exc).__name__, exc,
        )
        if progress:
            progress(f"classify:failed:{material_id}")
        return

    if progress:
        progress(f"classify:{'cached' if from_cache else 'llm'}:{material_id}")


def _has_assignments(session: Session, material_id: int, version: int) -> bool:
    return (
        session.execute(
            select(MaterialTopic.id).where(
                MaterialTopic.material_id == material_id,
                MaterialTopic.taxonomy_version == version,
            ).limit(1)
        ).first()
        is not None
    )


def _build_user_prompt(material: Material, context: _TaxonomyContext) -> str:
    key_terms = ", ".join(_key_terms(material))
    return (
        f"{SECTION_COURSE_TOPICS}\n"
        f"{context.block}\n"
        "\n"
        f"{SECTION_MATERIAL}\n"
        f"Title: {material.title}\n"
        f"Kind: {material.kind}\n"
        f"Key terms: {key_terms or '(none)'}\n"
        "Summary:\n"
        f"{material.summary or '(no summary available)'}\n"
    )


def _key_terms(material: Material) -> list[str]:
    try:
        meta = json.loads(material.summary_meta_json or "{}")
    except json.JSONDecodeError:
        return []
    terms = meta.get("key_terms") or []
    return [str(term) for term in terms if str(term).strip()]


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------


def _read_cache(
    session: Session, sha256: str, context: _TaxonomyContext, model: str
) -> ClassificationOut | None:
    """The cached classification, or None if there isn't a usable one.

    Validation happens *here*, not at the point of use: a row that is valid
    JSON but no longer matches the schema would otherwise raise on every run
    forever, and (because the write is an upsert) never get replaced. Any
    unusable row is a miss, and the fresh answer overwrites it.
    """
    row = session.execute(
        select(LlmCache).where(
            LlmCache.sha256 == sha256,
            LlmCache.stage == _STAGE,
            LlmCache.prompt_version == context.cache_prompt_version,
            LlmCache.model == model,
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    try:
        return ClassificationOut.model_validate(json.loads(row.output_json))
    except (json.JSONDecodeError, ValidationError) as exc:
        logger.warning(
            "classify: ignoring unusable cache row for %s (%s); re-asking the model",
            sha256[:12], exc,
        )
        return None


def _write_cache(
    session: Session, sha256: str, context: _TaxonomyContext, model: str, payload: ClassificationOut
) -> None:
    # Upsert, for two reasons: two workers can hit the same sha in one run
    # (the same file attached under two topics), and a row rejected by
    # `_read_cache` has to be replaceable rather than permanent.
    output_json = json.dumps(payload.model_dump())
    created_at = _now_iso()
    session.execute(
        sqlite_insert(LlmCache)
        .values(
            sha256=sha256,
            stage=_STAGE,
            prompt_version=context.cache_prompt_version,
            model=model,
            output_json=output_json,
            created_at=created_at,
        )
        .on_conflict_do_update(
            index_elements=["sha256", "stage", "prompt_version", "model"],
            set_={"output_json": output_json, "created_at": created_at},
        )
    )


# --------------------------------------------------------------------------
# Post-validation
# --------------------------------------------------------------------------


def _validate(
    classification: ClassificationOut, context: _TaxonomyContext, material_id: int
) -> list[tuple[int, float, str]]:
    """(topic_id, confidence, rationale) for the assignments worth keeping.

    Unknown slugs are dropped (a hallucinated topic is worse than none),
    confidences are clamped into [0, 1], a topic named twice keeps its
    highest-confidence row (`material_topics` is unique per material/topic/
    version), and at most `_MAX_ASSIGNMENTS` survive -- the prompt asks for
    1-3, and a material filed under eight topics is filed under none.
    """
    best: dict[int, tuple[float, str]] = {}
    for assignment in classification.assignments:
        slug = slugify(assignment.topic_slug)
        topic_id = context.topic_ids.get(slug)
        if topic_id is None:
            logger.warning(
                "classify: material %s was assigned unknown topic %r; dropped",
                material_id, assignment.topic_slug,
            )
            continue
        confidence = min(1.0, max(0.0, float(assignment.confidence)))
        rationale = assignment.rationale.strip()
        existing = best.get(topic_id)
        if existing is None or confidence > existing[0]:
            best[topic_id] = (confidence, rationale)

    kept = sorted(
        ((topic_id, confidence, rationale) for topic_id, (confidence, rationale) in best.items()),
        key=lambda row: (-row[1], row[0]),  # confidence desc, then id for determinism
    )
    if len(kept) > _MAX_ASSIGNMENTS:
        logger.warning(
            "classify: material %s was assigned %d topics; keeping the %d most confident",
            material_id, len(kept), _MAX_ASSIGNMENTS,
        )
        kept = kept[:_MAX_ASSIGNMENTS]
    return kept
