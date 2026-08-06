"""Task 12: applying a student's edit to a course's taxonomy.

`apply_taxonomy_edit()` is the whole decision -- patch vs. structural -- and
everything that follows from it. The API layer (`api/taxonomy.py`) is a thin
HTTP wrapper: it parses the request body into `TopicEditIn`/`EdgeEditIn`,
calls this module, and maps `TaxonomyValidationError` to a 422. No LLM call
happens here, ever -- see the module-level note on `apply_taxonomy_edit`.

Two paths, chosen by comparing the request to the course's *current*
taxonomy version:

- **Patch**: the request keeps exactly the current version's topic ids (no
  `id: null`, no merges) and its edge set, byte for byte. The only thing
  that can differ is wording -- name/description -- so the existing rows are
  updated in place (`created_by` flips to `'user'` on rows that actually
  changed) and nothing else happens: no new version, no re-classification.

- **Structural**: anything else -- an added or deleted topic, a merge, or
  even just a different edge set with identical topic wording. This mints
  taxonomy_version + 1: one new `Topic` row per request entry (see
  `_assign_slug_and_owner` for the slug/created_by rules), fresh `TopicEdge`
  rows from the request's `edges`, and a *carry-over* pass that re-inserts
  every still-valid `material_topics` assignment at the new version (see
  `_carry_over_assignments`). Whatever a material doesn't get carried keeps
  zero rows at the new version -- exactly the condition S3's classify stage
  (`pipeline/stages/classify.py::_select_worklist`) uses to decide what to
  re-classify, so no separate "what changed" list needs to be computed or
  threaded through: the DB state alone determines the reclassify worklist.
  The runner is *invoked*, not reimplemented -- this module never calls an
  LLM itself; `runner.start(course_id, ["classify", "assemble"])` does that
  work, and its cache key (taxonomy content digest) already ensures only
  genuinely-uncovered materials get re-billed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from brightspace_agent.agents.promptfmt import slugify
from brightspace_agent.db.models import Course, MaterialTopic, Topic, TopicEdge


class TaxonomyValidationError(ValueError):
    """The request is malformed relative to the current taxonomy: an unknown
    id, a repeated id, an empty name, or a self-loop edge. The API layer
    maps this to a 422; nothing is written when it's raised."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass
class TopicEditIn:
    """One entry in the request's `topics` array. `id=None` means "new
    topic". `merged_from_topic_ids` are CURRENT-version topic ids folded
    into this entry (never including the entry's own id)."""

    id: int | None
    name: str
    description: str
    merged_from_topic_ids: list[int] = field(default_factory=list)


@dataclass
class EdgeEditIn:
    """One entry in the request's `edges` array. `from_index`/`to_index`
    index into the request's `topics` array (0-based), not topic ids --
    that's what lets an edge point at a brand-new (`id=None`) topic."""

    from_index: int
    to_index: int
    relation: str


@dataclass
class TaxonomyApplyResult:
    taxonomy_version: int
    reclassify: bool
    run_token: int | None = None


class _RunnerLike(Protocol):
    """What this module needs from `PipelineRunner`: just `start()`. Kept as
    a local Protocol (rather than importing `PipelineRunner` itself) so a
    test can inject a bare fake with no LangGraph/asyncio machinery at all
    -- see pipeline/graph.py's `StageHooks` for the same pattern."""

    def start(self, course_id: int, stages: list[str] | None = None) -> int: ...


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def apply_taxonomy_edit(
    session: Session,
    runner: _RunnerLike,
    course: Course,
    topics: list[TopicEditIn],
    edges: list[EdgeEditIn],
) -> TaxonomyApplyResult:
    """Validate and apply one taxonomy edit for `course`.

    `session` is committed by this function (once, at the end of whichever
    path is taken) -- the caller doesn't need to. On the structural path,
    `runner.start()` is called *after* that commit, so a `RunActiveError` it
    raises (a run already active for this course) propagates to the caller
    with the taxonomy edit already durable; the API layer maps that to a
    409, matching `POST .../pipeline/run`'s existing contract.
    """
    current_version = course.taxonomy_version
    current_topics = _current_topics(session, course.id, current_version)
    current_topic_ids = set(current_topics)

    _validate(topics, edges, current_topic_ids)

    if _is_patch(session, course.id, topics, edges, current_topics, current_topic_ids):
        _apply_patch(topics, current_topics)
        session.commit()
        return TaxonomyApplyResult(taxonomy_version=current_version, reclassify=False)

    new_version = current_version + 1
    _apply_structural(session, course, current_version, new_version, topics, edges, current_topics)
    session.commit()

    run_token = runner.start(course.id, ["classify", "assemble"])
    return TaxonomyApplyResult(taxonomy_version=new_version, reclassify=True, run_token=run_token)


# --------------------------------------------------------------------------
# Reading the current taxonomy
# --------------------------------------------------------------------------


def _current_topics(session: Session, course_id: int, version: int) -> dict[int, Topic]:
    rows = (
        session.execute(
            select(Topic)
            .where(Topic.course_id == course_id, Topic.taxonomy_version == version)
            .order_by(Topic.order_index, Topic.id)
        )
        .scalars()
        .all()
    )
    return {row.id: row for row in rows}


def _current_edges(session: Session, course_id: int, current_topic_ids: set[int]) -> set[tuple[int, int, str]]:
    """Edges aren't versioned themselves: one belongs to `version` iff both
    endpoints are topics at that version -- the same rule graph/build.py and
    pipeline/stages/taxonomy.py's `_current_digest` use."""
    rows = session.execute(select(TopicEdge).where(TopicEdge.course_id == course_id)).scalars().all()
    return {
        (row.from_topic_id, row.to_topic_id, row.relation)
        for row in rows
        if row.from_topic_id in current_topic_ids and row.to_topic_id in current_topic_ids
    }


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _validate(topics: list[TopicEditIn], edges: list[EdgeEditIn], current_topic_ids: set[int]) -> None:
    seen_ids: set[int] = set()

    for topic in topics:
        if not topic.name.strip():
            raise TaxonomyValidationError("topic name must not be empty")

        if topic.id is not None:
            if topic.id not in current_topic_ids:
                raise TaxonomyValidationError(f"unknown topic id {topic.id}")
            if topic.id in seen_ids:
                raise TaxonomyValidationError(f"topic id {topic.id} used more than once")
            seen_ids.add(topic.id)

        for merged_id in topic.merged_from_topic_ids:
            if merged_id not in current_topic_ids:
                raise TaxonomyValidationError(f"unknown merged topic id {merged_id}")
            if merged_id in seen_ids:
                raise TaxonomyValidationError(f"topic id {merged_id} used more than once")
            seen_ids.add(merged_id)

    topic_count = len(topics)
    for edge in edges:
        if not (0 <= edge.from_index < topic_count) or not (0 <= edge.to_index < topic_count):
            raise TaxonomyValidationError(
                f"edge index out of range (fromIndex={edge.from_index}, toIndex={edge.to_index})"
            )
        if edge.from_index == edge.to_index:
            raise TaxonomyValidationError("self-loop edges are not allowed")
        if edge.relation not in ("prerequisite", "related"):
            raise TaxonomyValidationError(f"unknown edge relation {edge.relation!r}")


# --------------------------------------------------------------------------
# Patch-vs-structural decision
# --------------------------------------------------------------------------


def _is_patch(
    session: Session,
    course_id: int,
    topics: list[TopicEditIn],
    edges: list[EdgeEditIn],
    current_topics: dict[int, Topic],
    current_topic_ids: set[int],
) -> bool:
    payload_ids: list[int] = []
    for topic in topics:
        if topic.id is None or topic.merged_from_topic_ids:
            return False
        payload_ids.append(topic.id)

    if set(payload_ids) != current_topic_ids:
        return False

    current_edge_set = _current_edges(session, course_id, current_topic_ids)
    payload_edge_set = {
        (topics[edge.from_index].id, topics[edge.to_index].id, edge.relation) for edge in edges
    }
    return payload_edge_set == current_edge_set


def _apply_patch(topics: list[TopicEditIn], current_topics: dict[int, Topic]) -> None:
    for topic in topics:
        row = current_topics[topic.id]  # id is guaranteed set on the patch path
        name = topic.name.strip()
        description = topic.description.strip()
        if row.name != name or row.description != description:
            row.name = name
            row.description = description
            row.created_by = "user"


# --------------------------------------------------------------------------
# Structural path
# --------------------------------------------------------------------------


def _apply_structural(
    session: Session,
    course: Course,
    current_version: int,
    new_version: int,
    topics: list[TopicEditIn],
    edges: list[EdgeEditIn],
    current_topics: dict[int, Topic],
) -> None:
    new_rows: list[Topic] = []
    used_slugs: set[str] = set()

    for index, topic in enumerate(topics):
        current = current_topics.get(topic.id) if topic.id is not None else None
        name = topic.name.strip()
        description = topic.description.strip()
        slug, created_by = _assign_slug_and_owner(topic, current, name, index, used_slugs)
        used_slugs.add(slug)
        row = Topic(
            course_id=course.id, taxonomy_version=new_version, slug=slug, name=name,
            description=description, order_index=index, created_by=created_by,
        )
        session.add(row)
        new_rows.append(row)

    session.flush()  # assign ids to new_rows before building the carry-over map

    old_to_new: dict[int, int] = {}
    for topic, row in zip(topics, new_rows, strict=True):
        if topic.id is not None:
            old_to_new[topic.id] = row.id
        for merged_id in topic.merged_from_topic_ids:
            old_to_new[merged_id] = row.id

    for edge in edges:
        session.add(
            TopicEdge(
                course_id=course.id,
                from_topic_id=new_rows[edge.from_index].id,
                to_topic_id=new_rows[edge.to_index].id,
                relation=edge.relation,
                created_by="user",
            )
        )

    _carry_over_assignments(session, new_version, current_version, old_to_new)

    course.taxonomy_version = new_version


def _assign_slug_and_owner(
    topic: TopicEditIn, current: Topic | None, name: str, index: int, used_slugs: set[str]
) -> tuple[str, str]:
    """Slug: keep the old topic's slug when `id` is set and the name is
    unchanged; otherwise slugify the new name (deduped against slugs already
    claimed in this version). created_by: 'user' for a new topic, a rename,
    or a merge target; 'agent' for anything carried forward untouched."""
    renamed = current is not None and current.name != name
    is_merge_target = bool(topic.merged_from_topic_ids)

    if current is not None and not renamed:
        slug = _dedupe_slug(current.slug, used_slugs)
    else:
        slug = _dedupe_slug(slugify(name) or f"topic-{index + 1}", used_slugs)

    created_by = "user" if (current is None or renamed or is_merge_target) else "agent"
    return slug, created_by


def _dedupe_slug(base: str, used_slugs: set[str]) -> str:
    if base not in used_slugs:
        return base
    suffix = 2
    while f"{base}-{suffix}" in used_slugs:
        suffix += 1
    return f"{base}-{suffix}"


def _carry_over_assignments(
    session: Session, new_version: int, old_version: int, old_to_new: dict[int, int]
) -> None:
    """Re-insert every still-valid `material_topics` row at `new_version`.
    A material maps forward iff its old topic id is in `old_to_new`
    (identity for an id-kept topic, merge-map for a folded one); anything
    else is left with zero rows at `new_version`, which is exactly what
    S3's classify worklist selects on. When several old rows for the same
    material collapse onto the same new topic (merged siblings), only the
    highest-confidence one survives -- ties keep whichever is seen first,
    which the `order_by` below makes deterministic.
    """
    if not old_to_new:
        return

    old_rows = (
        session.execute(
            select(MaterialTopic)
            .where(MaterialTopic.taxonomy_version == old_version, MaterialTopic.topic_id.in_(old_to_new))
            .order_by(MaterialTopic.material_id, MaterialTopic.topic_id, MaterialTopic.id)
        )
        .scalars()
        .all()
    )

    best: dict[tuple[int, int], MaterialTopic] = {}
    for row in old_rows:
        key = (row.material_id, old_to_new[row.topic_id])
        existing = best.get(key)
        if existing is None or (row.confidence or 0.0) > (existing.confidence or 0.0):
            best[key] = row

    for (material_id, new_topic_id), row in best.items():
        session.add(
            MaterialTopic(
                material_id=material_id,
                topic_id=new_topic_id,
                taxonomy_version=new_version,
                confidence=row.confidence,
                rationale=row.rationale,
                method=row.method,
                review_status=row.review_status,
            )
        )
