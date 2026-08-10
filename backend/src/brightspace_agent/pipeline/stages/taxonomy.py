"""S2 taxonomy stage: one smart-model call proposes the course's whole topic
map from the syllabus, the module outline, and every material summary.

The stage is a single LLM call, so unlike S1/S3 there is no fan-out -- but
there is a lot of care around what goes *into* that call and what is allowed
to come out of it:

1. Gather (one session): syllabus text (sidecar preferred, summary as a
   fallback), the module tree rendered as an indented outline, and one
   compact line per summarized material that has real content, ordered so
   the model sees the course roughly in teaching order (metadata-only
   summaries are excluded -- see `_material_summary_lines`). The prompt is
   capped at `_PROMPT_MAX_CHARS`; summaries past the cap are dropped and
   logged.
2. Cache: key on a hash of the assembled prompt itself (system + user +
   model), so an unchanged course never pays twice -- and two courses can
   never collide, however similar their module titles look.
3. Validate in code, never by hoping the prompt held: slugs are normalized,
   duplicates dropped, edges pointing at unknown slugs or at themselves
   removed, and a proposal with fewer than `_MIN_TOPICS` topics fails the
   stage rather than writing a junk taxonomy over a course.
4. Write (one session, one commit): topics + edges at
   `courses.taxonomy_version + 1`, then bump the course. Older versions are
   never deleted -- they are the history the taxonomy editor (Task 12) and
   any later remap depend on.

Step 4 is skipped entirely when the validated proposal digests to the same
taxonomy the course is already on: re-running the pipeline on an unchanged
course must be a true no-op, because every new version would otherwise force
S3 to re-classify (and re-bill) every material against an identical map.

Steps 1-4 are skipped entirely -- before the LLM call -- when the course's
current taxonomy contains any user-authored topic (`created_by='user'`, as
pipeline/taxonomy_apply.py leaves them). The student's map wins by default;
a caller that genuinely wants the agent's opinion again has to ask for it
(`force=True`, surfaced as `forceTaxonomy` on POST /pipeline/run).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import resources

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from brightspace_agent.agents.llm import LLMBackend, Tier
from brightspace_agent.agents.promptfmt import (
    SECTION_COURSE,
    SECTION_MATERIAL_SUMMARIES,
    SECTION_MODULE_OUTLINE,
    SECTION_SYLLABUS,
    render_topic_block,
    slugify,
    taxonomy_digest,
)
from brightspace_agent.agents.schemas import TaxonomyOut, TopicDef, TopicEdgeDef
from brightspace_agent.db.models import Course, LlmCache, Material, Module, Topic, TopicEdge
from brightspace_agent.ingest.store import BlobStore
from brightspace_agent.pipeline.stats import StageStats

logger = logging.getLogger(__name__)

PROMPT_VERSION = "s2.v1"
_STAGE = "taxonomy"
_TIER: Tier = "smart"

_SYLLABUS_MAX_CHARS = 15_000
_PROMPT_MAX_CHARS = 60_000
_SUMMARY_LINES = 2
_MIN_TOPICS = 3
_MAX_TOPICS = 30
_MAX_SLUG_CHARS = 80  # slugs end up in URLs and the taxonomy editor

_SYSTEM_PROMPT = (
    resources.files("brightspace_agent.agents.prompts").joinpath("taxonomy.md").read_text(encoding="utf-8")
)


class TaxonomyStageError(RuntimeError):
    """The stage could not produce a usable taxonomy. Nothing was written:
    the course keeps whatever taxonomy version it already had."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


async def run_taxonomy_stage(
    session_factory: sessionmaker[Session],
    backend: LLMBackend,
    course_id: int,
    *,
    blob_store: BlobStore | None = None,
    force: bool = False,
) -> StageStats:
    """Propose and persist a new taxonomy version for `course_id`.

    `blob_store` is optional: with it, the syllabus goes into the prompt as
    its full extracted text (far richer -- a syllabus usually lists the
    course's topic schedule outright); without it, the stage falls back to
    the syllabus material's summary.

    `force` overrides the user-taxonomy guard below. Default `False`: a
    course whose current taxonomy contains any user-authored topic is left
    alone (no LLM call, no write, `stats.skipped_user_taxonomy`). `True`
    re-proposes anyway, which will write the agent's taxonomy at a new
    version over the student's -- only pass it when a caller has explicitly
    asked for that (see the `forceTaxonomy` flag on POST /pipeline/run).

    Raises `TaxonomyStageError` if the model's proposal is unusable.
    """
    return await asyncio.to_thread(_run_sync, session_factory, backend, course_id, blob_store, force)


def _has_user_topics(session: Session, course_id: int, version: int) -> bool:
    """True if any topic at `version` was authored (or renamed, or merged)
    by the student rather than the agent -- see pipeline/taxonomy_apply.py's
    `_assign_slug_and_owner` for exactly when `created_by` becomes 'user'."""
    return (
        session.execute(
            select(Topic.id)
            .where(
                Topic.course_id == course_id,
                Topic.taxonomy_version == version,
                Topic.created_by == "user",
            )
            .limit(1)
        ).first()
        is not None
    )


def _run_sync(
    session_factory: sessionmaker[Session],
    backend: LLMBackend,
    course_id: int,
    blob_store: BlobStore | None,
    force: bool = False,
) -> StageStats:
    stats = StageStats()
    model = backend.model_for_tier(_TIER)

    with session_factory() as session:
        course = session.get(Course, course_id)
        if course is None:
            raise TaxonomyStageError(f"no course with id {course_id}")

        # A student's own taxonomy outranks the agent's. Once an edit has
        # minted a version carrying user-authored topics, the next full run
        # would otherwise propose from the same summaries, digest
        # differently from the edited map, and write the AGENT's taxonomy at
        # version+1 -- silently reverting the edit, with re-classification
        # billed on top. Checked before the LLM call, so declining costs
        # nothing.
        if not force and _has_user_topics(session, course_id, course.taxonomy_version):
            stats.unchanged = True
            stats.skipped_user_taxonomy = True
            stats.taxonomy_version = course.taxonomy_version
            logger.info(
                "taxonomy: course %s is on a user-edited taxonomy (version %d); "
                "leaving it alone (pass force=True to re-propose over it)",
                course_id, course.taxonomy_version,
            )
            return stats

        inputs = _gather_inputs(session, course, blob_store)
        user_prompt = _build_user_prompt(inputs)
        cache_key = _cache_key(user_prompt, model)
        cached_json = _read_cache(session, cache_key, model)

    proposal: TaxonomyOut | None = None
    if cached_json is not None:
        proposal = _parse_cached(cached_json)
        if proposal is not None:
            stats.cached_hits += 1

    if proposal is None:
        logger.info(
            "taxonomy: calling %s for course %s (%d chars, %d summaries)",
            model, course_id, len(user_prompt), len(inputs.material_lines),
        )
        parsed, usage = backend.structured_call(
            TaxonomyOut, system=_SYSTEM_PROMPT, user=user_prompt, tier=_TIER
        )
        proposal = parsed  # type: ignore[assignment]
        stats.add_usage(usage)

    topics, edges = _validate(proposal, course_id)
    proposed_digest = _content_digest(
        render_topic_block((topic.slug, topic.name, topic.description) for topic in topics),
        ((edge.from_slug, edge.to_slug, edge.relation) for edge in edges),
    )

    with session_factory() as session:
        course = session.get(Course, course_id)
        if course is None:  # deleted mid-run
            raise TaxonomyStageError(f"no course with id {course_id}")

        if stats.cached_hits == 0:
            _upsert_cache(session, cache_key, model, proposal)

        # Same taxonomy as the course already has -> nothing to write. Without
        # this, every sync would mint an identical version, and S3 would
        # re-classify (and re-bill) the entire course against it.
        if _current_digest(session, course_id, course.taxonomy_version) == proposed_digest:
            session.commit()  # keep the cache row; touch nothing else
            stats.unchanged = True
            stats.taxonomy_version = course.taxonomy_version
            logger.info(
                "taxonomy: course %s proposal matches version %d; keeping it (no new version)",
                course_id, course.taxonomy_version,
            )
            return stats

        version = course.taxonomy_version + 1
        _write_taxonomy(session, course_id, version, topics, edges)
        course.taxonomy_version = version
        session.commit()

    stats.topics = len(topics)
    stats.edges = len(edges)
    stats.taxonomy_version = version
    logger.info(
        "taxonomy: course %s now at version %d (%d topics, %d edges%s)",
        course_id, version, stats.topics, stats.edges, ", from cache" if stats.cached_hits else "",
    )
    return stats


# --------------------------------------------------------------------------
# Input gathering
# --------------------------------------------------------------------------


@dataclass
class _TaxonomyInputs:
    course_name: str
    course_code: str | None
    syllabus_text: str
    module_lines: list[str] = field(default_factory=list)
    material_lines: list[str] = field(default_factory=list)


def _gather_inputs(session: Session, course: Course, blob_store: BlobStore | None) -> _TaxonomyInputs:
    module_lines, module_rank = _render_module_outline(session, course.id)
    return _TaxonomyInputs(
        course_name=course.name,
        course_code=course.code,
        syllabus_text=_syllabus_text(session, course.id, blob_store),
        module_lines=module_lines,
        material_lines=_material_summary_lines(session, course.id, module_rank),
    )


def _render_module_outline(session: Session, course_id: int) -> tuple[list[str], dict[int, int]]:
    """The module tree as an indented outline, depth-first in sort order.

    Also returns each module's position in that walk, so materials can be
    listed in roughly the order the course teaches them.
    """
    modules = list(
        session.execute(
            select(Module).where(Module.course_id == course_id).order_by(Module.sort_order, Module.id)
        ).scalars().all()
    )
    children: dict[int | None, list[Module]] = {}
    for module in modules:
        children.setdefault(module.parent_id, []).append(module)

    lines: list[str] = []
    rank: dict[int, int] = {}

    def walk(parent_id: int | None, depth: int) -> None:
        for module in children.get(parent_id, []):
            rank[module.id] = len(rank)
            lines.append(f"{'  ' * depth}- {module.title}")
            walk(module.id, depth + 1)

    walk(None, 0)
    # Orphaned subtrees (parent row missing) would otherwise vanish silently.
    for module in modules:
        if module.id not in rank:
            rank[module.id] = len(rank)
            lines.append(f"- {module.title}")

    return lines, rank


def _syllabus_text(session: Session, course_id: int, blob_store: BlobStore | None) -> str:
    """The syllabus material's extracted text, or its summary as a fallback.

    Which of the two was used matters to the cache: the key hashes the
    assembled prompt, so a run that could only reach the summary and a later
    run that reads the full sidecar are correctly treated as different
    questions rather than sharing an answer.
    """
    syllabi = list(
        session.execute(
            select(Material)
            .where(Material.course_id == course_id, Material.kind == "syllabus")
            .order_by(Material.id)
        ).scalars().all()
    )
    if not syllabi:
        return ""

    if blob_store is not None:
        for material in syllabi:
            if not material.sha256:
                continue
            text = blob_store.read_text(material.sha256)
            if text and text.strip():
                return text[:_SYLLABUS_MAX_CHARS]

    for material in syllabi:
        if material.summary:
            logger.info(
                "taxonomy: no syllabus sidecar text for course %s; using its summary instead", course_id
            )
            return material.summary[:_SYLLABUS_MAX_CHARS]

    return ""


def _material_summary_lines(
    session: Session, course_id: int, module_rank: dict[int, int]
) -> list[str]:
    """One compact line per summarized material that has real content.

    `sha256 IS NULL` materials are deliberately excluded (M3.5a): those are
    exactly the ones S1's pass 3 summarized from a metadata pseudo-document
    (`_promote_metadata_one` leaves `sha256` None on purpose), and such a
    summary is a restatement of the title the outline already carries -- no
    taxonomy signal, only noise.

    The cost of including them is not just a slightly worse prompt. Adding
    them changes the proposal, which digests differently from the taxonomy
    the course is already on, which mints a new version -- so the first run
    after this feature shipped would silently re-version and re-classify
    (full-course re-bill) every course that has ever had a link in it. The
    materials themselves are still summarized and still classified against
    the existing taxonomy; they just don't get a vote on what that taxonomy
    is.
    """
    materials = list(
        session.execute(
            select(Material).where(
                Material.course_id == course_id,
                Material.status == "summarized",
                Material.summary.is_not(None),
                Material.sha256.is_not(None),
            )
        ).scalars().all()
    )
    module_titles = {
        module.id: module.title
        for module in session.execute(select(Module).where(Module.course_id == course_id)).scalars().all()
    }
    # Teaching order: by module position in the outline, unfiled materials last.
    materials.sort(key=lambda m: (module_rank.get(m.module_id or -1, len(module_rank)), m.id))

    lines: list[str] = []
    for material in materials:
        summary_lines = [line.strip() for line in (material.summary or "").splitlines() if line.strip()]
        summary_head = " ".join(summary_lines[:_SUMMARY_LINES])
        key_terms = ", ".join(_key_terms(material))
        module_note = ""
        if material.module_id in module_titles:
            module_note = f" (module: {module_titles[material.module_id]})"
        lines.append(f'- [{material.kind}] "{material.title}"{module_note} :: {summary_head} :: {key_terms}')
    return lines


def _key_terms(material: Material) -> list[str]:
    try:
        meta = json.loads(material.summary_meta_json or "{}")
    except json.JSONDecodeError:
        return []
    terms = meta.get("key_terms") or []
    return [str(term) for term in terms if str(term).strip()]


def _build_user_prompt(inputs: _TaxonomyInputs) -> str:
    course_line = inputs.course_name
    if inputs.course_code:
        course_line = f"{inputs.course_name} ({inputs.course_code})"

    header_parts = [
        SECTION_COURSE,
        course_line,
        "",
        SECTION_SYLLABUS,
        inputs.syllabus_text.strip() or "(no syllabus material was found for this course)",
        "",
        SECTION_MODULE_OUTLINE,
        "\n".join(inputs.module_lines) or "(no module structure was found for this course)",
        "",
        f"{SECTION_MATERIAL_SUMMARIES}",
    ]
    header = "\n".join(header_parts)

    if not inputs.material_lines:
        return f"{header}\n(no summarized materials yet)\n"

    budget = _PROMPT_MAX_CHARS - len(header)
    kept: list[str] = []
    used = 0
    for line in inputs.material_lines:
        if used + len(line) + 1 > budget and kept:
            break
        kept.append(line)
        used += len(line) + 1

    dropped = len(inputs.material_lines) - len(kept)
    if dropped:
        logger.warning(
            "taxonomy: prompt cap of %d chars reached; dropped %d of %d material summaries",
            _PROMPT_MAX_CHARS, dropped, len(inputs.material_lines),
        )
        kept.append(f"({dropped} further materials omitted to fit the context budget)")

    return f"{header}\n" + "\n".join(kept) + "\n"


def _cache_key(user_prompt: str, model: str) -> str:
    """sha256 over exactly what the model is asked, and which model is asked.

    Hashing a summary of the inputs (course-independent things like module
    titles and material shas) let two different courses collide: identical
    module titles in "Data Structures" and "Organic Chemistry" produced the
    same key, and the second course silently inherited the first one's
    taxonomy forever. Hashing the assembled prompt makes that impossible by
    construction -- the prompt already contains the course name and code, the
    syllabus text *as actually resolved* (sidecar or summary fallback), the
    outline, and the summaries -- and it picks up prompt-template edits for
    free alongside PROMPT_VERSION.
    """
    canonical = json.dumps(
        {"system": _SYSTEM_PROMPT, "user": user_prompt, "model": model},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _read_cache(session: Session, cache_key: str, model: str) -> str | None:
    row = session.execute(
        select(LlmCache).where(
            LlmCache.sha256 == cache_key,
            LlmCache.stage == _STAGE,
            LlmCache.prompt_version == PROMPT_VERSION,
            LlmCache.model == model,
        )
    ).scalar_one_or_none()
    return row.output_json if row is not None else None


def _upsert_cache(session: Session, cache_key: str, model: str, proposal: TaxonomyOut) -> None:
    # `on_conflict_do_update`, not a plain insert: reaching here with a row
    # already present means the stored proposal was unparseable (see
    # `_parse_cached`), so the fresh one should replace it.
    output_json = json.dumps(proposal.model_dump())
    created_at = _now_iso()
    session.execute(
        sqlite_insert(LlmCache)
        .values(
            sha256=cache_key,
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


def _content_digest(topic_block: str, edges: Iterable[tuple[str, str, str]]) -> str:
    """A content fingerprint covering BOTH topics and edges.

    Topics alone used to be the whole story here, which meant an edge-only
    change (same topics, a different prerequisite/related link) digested
    identically to whatever the course already had -- the guarded write
    below treats "unchanged" as "write nothing", so a legitimate edge
    change was silently discarded and could never be repaired by
    re-running S2. Edges are sorted by (from_slug, to_slug, relation) --
    not proposal order -- so the model listing the same edges in a
    different order never causes a spurious version bump.

    Deliberately NOT reused for S3's cache key: classify.py's prompt only
    ever shows the topic list (`render_topic_block`), never the edges, so
    its cache key stays topic-only -- an edge-only taxonomy change must not
    force every material to be re-classified (and re-billed).
    """
    edge_block = "\n".join(f"{from_slug} -> {to_slug} :: {relation}" for from_slug, to_slug, relation in sorted(edges))
    return taxonomy_digest(f"{topic_block}\n{edge_block}")


def _current_digest(session: Session, course_id: int, version: int) -> str | None:
    """The content digest (topics AND edges) of the taxonomy the course is on
    right now, or None if it has no topics yet."""
    topics = list(
        session.execute(
            select(Topic)
            .where(Topic.course_id == course_id, Topic.taxonomy_version == version)
            .order_by(Topic.order_index, Topic.id)
        ).scalars().all()
    )
    if not topics:
        return None
    topic_block = render_topic_block((topic.slug, topic.name, topic.description) for topic in topics)

    # Edges aren't versioned themselves (see topic_edges' schema): an edge
    # belongs to this version iff both endpoints are topics at this version
    # -- the same rule graph/build.py uses to scope edges into the graph.
    slug_by_topic_id = {topic.id: topic.slug for topic in topics}
    edge_rows = list(
        session.execute(select(TopicEdge).where(TopicEdge.course_id == course_id)).scalars().all()
    )
    edges = [
        (slug_by_topic_id[edge.from_topic_id], slug_by_topic_id[edge.to_topic_id], edge.relation)
        for edge in edge_rows
        if edge.from_topic_id in slug_by_topic_id and edge.to_topic_id in slug_by_topic_id
    ]
    return _content_digest(topic_block, edges)


def _parse_cached(output_json: str) -> TaxonomyOut | None:
    """A cache row that no longer parses is treated as a miss rather than
    wedging the stage forever on one bad row."""
    try:
        return TaxonomyOut.model_validate(json.loads(output_json))
    except (json.JSONDecodeError, ValidationError) as exc:
        logger.warning("taxonomy: ignoring unparseable cache row (%s); calling the model instead", exc)
        return None


# --------------------------------------------------------------------------
# Post-validation -- the model's proposal is a suggestion, not a schema
# --------------------------------------------------------------------------


def _normalize_slug(value: str) -> str:
    """Kebab-case and length-capped. Edges go through this too, so a slug the
    model wrote out in full still resolves to its truncated topic."""
    return slugify(value)[:_MAX_SLUG_CHARS].strip("-")


def _validate(proposal: TaxonomyOut, course_id: int) -> tuple[list[TopicDef], list[TopicEdgeDef]]:
    topics: list[TopicDef] = []
    seen_slugs: set[str] = set()

    for topic in proposal.topics:
        slug = _normalize_slug(topic.slug) or _normalize_slug(topic.name)
        name = topic.name.strip()
        if not slug or not name:
            logger.warning("taxonomy: dropping topic with empty slug/name (%r)", topic)
            continue
        if slug in seen_slugs:
            logger.warning("taxonomy: dropping duplicate topic slug %r (kept the first)", slug)
            continue
        seen_slugs.add(slug)
        topics.append(
            TopicDef(
                slug=slug,
                name=name,
                description=topic.description.strip(),
                module_hints=[hint.strip() for hint in topic.module_hints if hint.strip()],
            )
        )

    if len(topics) > _MAX_TOPICS:
        logger.warning(
            "taxonomy: model proposed %d topics for course %s; keeping the first %d",
            len(topics), course_id, _MAX_TOPICS,
        )
        topics = topics[:_MAX_TOPICS]
        seen_slugs = {topic.slug for topic in topics}

    if len(topics) < _MIN_TOPICS:
        raise TaxonomyStageError(
            f"taxonomy proposal for course {course_id} had only {len(topics)} usable topics "
            f"(minimum {_MIN_TOPICS}); nothing was written"
        )

    edges: list[TopicEdgeDef] = []
    seen_edges: set[tuple[str, str, str]] = set()
    for edge in proposal.edges:
        from_slug = _normalize_slug(edge.from_slug)
        to_slug = _normalize_slug(edge.to_slug)
        if from_slug not in seen_slugs or to_slug not in seen_slugs:
            logger.warning(
                "taxonomy: dropping edge %r -> %r (%s): unknown topic slug",
                edge.from_slug, edge.to_slug, edge.relation,
            )
            continue
        if from_slug == to_slug:
            logger.warning("taxonomy: dropping self-edge on %r", from_slug)
            continue
        key = (from_slug, to_slug, edge.relation)
        if key in seen_edges:
            continue
        seen_edges.add(key)
        edges.append(TopicEdgeDef(from_slug=from_slug, to_slug=to_slug, relation=edge.relation))

    return topics, edges


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def _write_taxonomy(
    session: Session,
    course_id: int,
    version: int,
    topics: list[TopicDef],
    edges: list[TopicEdgeDef],
) -> None:
    topic_ids: dict[str, int] = {}
    for order_index, topic in enumerate(topics):
        row = Topic(
            course_id=course_id,
            taxonomy_version=version,
            slug=topic.slug,
            name=topic.name,
            description=topic.description,
            order_index=order_index,
            created_by="agent",
        )
        session.add(row)
        session.flush()
        topic_ids[topic.slug] = row.id

    for edge in edges:
        session.add(
            TopicEdge(
                course_id=course_id,
                from_topic_id=topic_ids[edge.from_slug],
                to_topic_id=topic_ids[edge.to_slug],
                relation=edge.relation,
                created_by="agent",
            )
        )
