"""Tests for the taxonomy editor's apply logic (Task 12): the patch-vs-
structural decision, carry-over of still-valid assignments, slug handling,
and validation -- all against a real (in-memory-ish sqlite) DB, with a fake
runner standing in for `PipelineRunner` so these stay unit tests (no LLM,
no asyncio event loop).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from brightspace_agent.db.models import Course, Material, MaterialTopic, Topic, TopicEdge
from brightspace_agent.db.session import init_db
from brightspace_agent.pipeline.taxonomy_apply import (
    EdgeEditIn,
    TaxonomyValidationError,
    TopicEditIn,
    apply_taxonomy_edit,
)


@pytest.fixture
def session_factory(tmp_path):
    return init_db(tmp_path / "brightspace.db")[1]


@pytest.fixture
def course_id(session_factory):
    with session_factory() as session:
        course = Course(d2l_org_unit_id=1, tenant_origin="school.d2l.com", name="Intro to CS", code="CS100")
        session.add(course)
        session.commit()
        return course.id


class _FakeRunner:
    """Stands in for `PipelineRunner`: records `start()` calls instead of
    actually running anything."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, list[str] | None]] = []
        self._next_token = 100

    def start(self, course_id: int, stages: list[str] | None = None) -> int:
        self.calls.append((course_id, list(stages) if stages is not None else None))
        token = self._next_token
        self._next_token += 1
        return token


@pytest.fixture
def runner():
    return _FakeRunner()


# --------------------------------------------------------------------------
# Seeding helpers
# --------------------------------------------------------------------------


def _seed_v1(session_factory, course_id, topics, edges=()):
    """`topics`: [(slug, name, description)]. `edges`: [(from_slug, to_slug,
    relation)]. Returns {slug: topic_id}, and bumps the course to version 1."""
    with session_factory() as session:
        ids: dict[str, int] = {}
        for index, (slug, name, description) in enumerate(topics):
            row = Topic(
                course_id=course_id, taxonomy_version=1, slug=slug, name=name,
                description=description, order_index=index, created_by="agent",
            )
            session.add(row)
            session.flush()
            ids[slug] = row.id
        for from_slug, to_slug, relation in edges:
            session.add(
                TopicEdge(
                    course_id=course_id, from_topic_id=ids[from_slug], to_topic_id=ids[to_slug],
                    relation=relation, created_by="agent",
                )
            )
        session.get(Course, course_id).taxonomy_version = 1
        session.commit()
        return ids


def _add_material(session_factory, course_id, *, title="Material", status="summarized") -> int:
    with session_factory() as session:
        material = Material(course_id=course_id, kind="document", title=title, status=status)
        session.add(material)
        session.commit()
        return material.id


def _add_assignment(
    session_factory, material_id, topic_id, version, *,
    confidence=0.8, rationale="on topic", method="llm", review_status="auto",
) -> None:
    with session_factory() as session:
        session.add(
            MaterialTopic(
                material_id=material_id, topic_id=topic_id, taxonomy_version=version,
                confidence=confidence, rationale=rationale, method=method, review_status=review_status,
            )
        )
        session.commit()


def _course(session_factory, course_id) -> Course:
    with session_factory() as session:
        course = session.get(Course, course_id)
        session.expunge(course)
        return course


def _topics_at(session_factory, course_id, version) -> list[Topic]:
    with session_factory() as session:
        rows = list(
            session.execute(
                select(Topic)
                .where(Topic.course_id == course_id, Topic.taxonomy_version == version)
                .order_by(Topic.order_index, Topic.id)
            ).scalars().all()
        )
        for row in rows:
            session.expunge(row)
        return rows


def _edges(session_factory, course_id) -> list[TopicEdge]:
    with session_factory() as session:
        rows = list(session.execute(select(TopicEdge).where(TopicEdge.course_id == course_id)).scalars().all())
        for row in rows:
            session.expunge(row)
        return rows


def _material_topics_at(session_factory, version) -> list[MaterialTopic]:
    with session_factory() as session:
        rows = list(
            session.execute(select(MaterialTopic).where(MaterialTopic.taxonomy_version == version)).scalars().all()
        )
        for row in rows:
            session.expunge(row)
        return rows


def _apply(session_factory, runner, course_id, topics, edges=()):
    with session_factory() as session:
        course = session.get(Course, course_id)
        result = apply_taxonomy_edit(session, runner, course, topics, list(edges))
        return result


# --------------------------------------------------------------------------
# (1) Rename + re-describe only -> patch
# --------------------------------------------------------------------------


def test_rename_and_redescribe_only_is_a_patch(session_factory, runner, course_id):
    ids = _seed_v1(
        session_factory, course_id,
        [("intro", "Intro", "d1"), ("advanced", "Advanced", "d2"), ("extra", "Extra", "d3")],
        edges=[("intro", "advanced", "prerequisite")],
    )
    material_id = _add_material(session_factory, course_id)
    _add_assignment(session_factory, material_id, ids["intro"], 1)

    topics = [
        TopicEditIn(id=ids["intro"], name="Introduction", description="Updated description."),
        TopicEditIn(id=ids["advanced"], name="Advanced", description="d2"),
        TopicEditIn(id=ids["extra"], name="Extra", description="d3"),
    ]
    edges = [EdgeEditIn(from_index=0, to_index=1, relation="prerequisite")]

    result = _apply(session_factory, runner, course_id, topics, edges)

    assert result.taxonomy_version == 1
    assert result.reclassify is False
    assert result.run_token is None
    assert runner.calls == []  # no run started

    assert _course(session_factory, course_id).taxonomy_version == 1  # no version bump
    v1 = {row.id: row for row in _topics_at(session_factory, course_id, 1)}
    assert v1[ids["intro"]].name == "Introduction"
    assert v1[ids["intro"]].description == "Updated description."
    assert v1[ids["intro"]].created_by == "user"  # changed
    assert v1[ids["intro"]].slug == "intro"  # slug untouched on a patch
    assert v1[ids["advanced"]].created_by == "agent"  # untouched row stays agent
    assert v1[ids["extra"]].created_by == "agent"

    assert _topics_at(session_factory, course_id, 2) == []  # no new version minted

    # assignments untouched: still exactly the one v1 row
    v1_assignments = _material_topics_at(session_factory, 1)
    assert len(v1_assignments) == 1
    assert v1_assignments[0].topic_id == ids["intro"]


# --------------------------------------------------------------------------
# (2) Add a topic -> structural; old assignments carried; assignment-less
#     materials remain assignment-less (would be queued by a real classify)
# --------------------------------------------------------------------------


def test_add_topic_is_structural_and_carries_old_assignments(session_factory, runner, course_id):
    ids = _seed_v1(session_factory, course_id, [("a", "A", "da"), ("b", "B", "db")])
    mat_a = _add_material(session_factory, course_id, title="On A")
    _add_assignment(session_factory, mat_a, ids["a"], 1, confidence=0.7)
    mat_unassigned = _add_material(session_factory, course_id, title="Never classified")

    topics = [
        TopicEditIn(id=ids["a"], name="A", description="da"),
        TopicEditIn(id=ids["b"], name="B", description="db"),
        TopicEditIn(id=None, name="C", description="dc"),
    ]

    result = _apply(session_factory, runner, course_id, topics)

    assert result.taxonomy_version == 2
    assert result.reclassify is True
    assert result.run_token is not None
    assert runner.calls == [(course_id, ["classify", "assemble"])]

    assert _course(session_factory, course_id).taxonomy_version == 2
    v2 = _topics_at(session_factory, course_id, 2)
    assert [t.name for t in v2] == ["A", "B", "C"]
    a2 = next(t for t in v2 if t.name == "A")
    c2 = next(t for t in v2 if t.name == "C")
    assert a2.created_by == "agent"  # unmodified, carried
    assert c2.created_by == "user"  # newly added

    v2_assignments = _material_topics_at(session_factory, 2)
    by_material = {row.material_id: row for row in v2_assignments}
    assert by_material[mat_a].topic_id == a2.id
    assert by_material[mat_a].confidence == 0.7
    assert mat_unassigned not in by_material  # stayed unassigned -> reclassify candidate


# --------------------------------------------------------------------------
# (3) Delete a topic -> its materials lack rows at N+1; others carried
# --------------------------------------------------------------------------


def test_delete_topic_leaves_its_materials_without_a_new_row(session_factory, runner, course_id):
    ids = _seed_v1(session_factory, course_id, [("a", "A", "da"), ("b", "B", "db"), ("c", "C", "dc")])
    mat_a = _add_material(session_factory, course_id, title="On A")
    mat_b = _add_material(session_factory, course_id, title="On B")
    mat_c = _add_material(session_factory, course_id, title="On C")
    _add_assignment(session_factory, mat_a, ids["a"], 1)
    _add_assignment(session_factory, mat_b, ids["b"], 1)
    _add_assignment(session_factory, mat_c, ids["c"], 1)

    # Drop "c" entirely.
    topics = [
        TopicEditIn(id=ids["a"], name="A", description="da"),
        TopicEditIn(id=ids["b"], name="B", description="db"),
    ]

    result = _apply(session_factory, runner, course_id, topics)

    assert result.taxonomy_version == 2
    assert result.reclassify is True
    assert runner.calls == [(course_id, ["classify", "assemble"])]

    v2 = _topics_at(session_factory, course_id, 2)
    assert {t.name for t in v2} == {"A", "B"}

    v2_assignments = _material_topics_at(session_factory, 2)
    by_material = {row.material_id for row in v2_assignments}
    assert mat_a in by_material
    assert mat_b in by_material
    assert mat_c not in by_material  # its topic was deleted -> will reclassify


# --------------------------------------------------------------------------
# (4) Merge A+B into C -> materials of A and B point at C; duplicate-material
#     case keeps the highest-confidence row; review_status preserved
# --------------------------------------------------------------------------


def test_merge_two_topics_carries_and_dedupes_by_confidence(session_factory, runner, course_id):
    ids = _seed_v1(session_factory, course_id, [("a", "A", "da"), ("b", "B", "db"), ("d", "D", "dd")])
    mat_a = _add_material(session_factory, course_id, title="On A only")
    mat_b = _add_material(session_factory, course_id, title="On B only")
    mat_ab = _add_material(session_factory, course_id, title="On both A and B")

    _add_assignment(session_factory, mat_a, ids["a"], 1, confidence=0.6, review_status="auto")
    _add_assignment(session_factory, mat_b, ids["b"], 1, confidence=0.9, review_status="auto")
    _add_assignment(
        session_factory, mat_ab, ids["a"], 1, confidence=0.7, rationale="via A", review_status="auto",
    )
    _add_assignment(
        session_factory, mat_ab, ids["b"], 1, confidence=0.95, rationale="via B", review_status="confirmed",
    )

    topics = [
        TopicEditIn(id=None, name="C", description="merged", merged_from_topic_ids=[ids["a"], ids["b"]]),
        TopicEditIn(id=ids["d"], name="D", description="dd"),
    ]

    result = _apply(session_factory, runner, course_id, topics)

    assert result.taxonomy_version == 2
    assert result.reclassify is True

    v2 = _topics_at(session_factory, course_id, 2)
    c2 = next(t for t in v2 if t.name == "C")
    assert c2.created_by == "user"  # merge target

    v2_assignments = {row.material_id: row for row in _material_topics_at(session_factory, 2)}
    assert v2_assignments[mat_a].topic_id == c2.id
    assert v2_assignments[mat_a].confidence == 0.6
    assert v2_assignments[mat_b].topic_id == c2.id
    assert v2_assignments[mat_b].confidence == 0.9

    # duplicate-material case: mat_ab had two v1 rows (A: 0.7, B: 0.95) both
    # mapping onto C -- exactly one v2 row survives, the higher-confidence one.
    ab_rows = [row for row in _material_topics_at(session_factory, 2) if row.material_id == mat_ab]
    assert len(ab_rows) == 1
    assert ab_rows[0].topic_id == c2.id
    assert ab_rows[0].confidence == 0.95
    assert ab_rows[0].rationale == "via B"
    assert ab_rows[0].review_status == "confirmed"  # preserved from the winning row


# --------------------------------------------------------------------------
# (5) Edge-only change -> structural path; all assignments carried; a real
#     classify run's worklist would be empty (every material still covered)
# --------------------------------------------------------------------------


def test_edge_only_change_is_structural_with_full_carryover(session_factory, runner, course_id):
    ids = _seed_v1(
        session_factory, course_id, [("a", "A", "da"), ("b", "B", "db")],
        edges=[("a", "b", "prerequisite")],
    )
    mat_a = _add_material(session_factory, course_id, title="On A")
    mat_b = _add_material(session_factory, course_id, title="On B")
    _add_assignment(session_factory, mat_a, ids["a"], 1)
    _add_assignment(session_factory, mat_b, ids["b"], 1)

    # Same topics (unchanged names/descriptions), but a different edge set.
    topics = [
        TopicEditIn(id=ids["a"], name="A", description="da"),
        TopicEditIn(id=ids["b"], name="B", description="db"),
    ]
    edges = [EdgeEditIn(from_index=0, to_index=1, relation="related")]  # was 'prerequisite'

    result = _apply(session_factory, runner, course_id, topics, edges)

    assert result.taxonomy_version == 2  # structural despite unchanged topic wording
    assert result.reclassify is True
    assert runner.calls == [(course_id, ["classify", "assemble"])]

    v2 = _topics_at(session_factory, course_id, 2)
    v2_edges = _edges(session_factory, course_id)
    id_by_name = {t.name: t.id for t in v2}
    new_edges = [e for e in v2_edges if e.from_topic_id in id_by_name.values() and e.to_topic_id in id_by_name.values()]
    assert len(new_edges) == 1
    assert new_edges[0].relation == "related"

    v2_assignments = _material_topics_at(session_factory, 2)
    covered_materials = {row.material_id for row in v2_assignments}
    # Every summarized material still has a row -- a real classify worklist
    # (materials lacking any row at the new version) would be empty.
    assert covered_materials == {mat_a, mat_b}


# --------------------------------------------------------------------------
# (6) Validation 422s (raised as TaxonomyValidationError; the API layer maps
#     these to 422): unknown id, repeated id, self-loop edge
# --------------------------------------------------------------------------


def test_unknown_topic_id_is_rejected(session_factory, runner, course_id):
    _seed_v1(session_factory, course_id, [("a", "A", "da")])
    topics = [TopicEditIn(id=999999, name="A", description="da")]

    with pytest.raises(TaxonomyValidationError):
        _apply(session_factory, runner, course_id, topics)

    assert _course(session_factory, course_id).taxonomy_version == 1
    assert runner.calls == []


def test_unknown_merged_from_id_is_rejected(session_factory, runner, course_id):
    ids = _seed_v1(session_factory, course_id, [("a", "A", "da")])
    topics = [TopicEditIn(id=ids["a"], name="A", description="da", merged_from_topic_ids=[999999])]

    with pytest.raises(TaxonomyValidationError):
        _apply(session_factory, runner, course_id, topics)


def test_repeated_id_is_rejected(session_factory, runner, course_id):
    ids = _seed_v1(session_factory, course_id, [("a", "A", "da"), ("b", "B", "db")])
    topics = [
        TopicEditIn(id=ids["a"], name="A", description="da"),
        TopicEditIn(id=ids["a"], name="A dup", description="da"),
    ]

    with pytest.raises(TaxonomyValidationError):
        _apply(session_factory, runner, course_id, topics)


def test_id_repeated_as_both_own_and_merged_is_rejected(session_factory, runner, course_id):
    ids = _seed_v1(session_factory, course_id, [("a", "A", "da"), ("b", "B", "db")])
    topics = [
        TopicEditIn(id=ids["a"], name="A", description="da"),
        TopicEditIn(id=ids["b"], name="B", description="db", merged_from_topic_ids=[ids["a"]]),
    ]

    with pytest.raises(TaxonomyValidationError):
        _apply(session_factory, runner, course_id, topics)


def test_empty_name_is_rejected(session_factory, runner, course_id):
    ids = _seed_v1(session_factory, course_id, [("a", "A", "da")])
    topics = [TopicEditIn(id=ids["a"], name="   ", description="da")]

    with pytest.raises(TaxonomyValidationError):
        _apply(session_factory, runner, course_id, topics)


def test_self_loop_edge_is_rejected(session_factory, runner, course_id):
    ids = _seed_v1(session_factory, course_id, [("a", "A", "da"), ("b", "B", "db")])
    topics = [
        TopicEditIn(id=ids["a"], name="A", description="da"),
        TopicEditIn(id=ids["b"], name="B", description="db"),
    ]
    edges = [EdgeEditIn(from_index=0, to_index=0, relation="related")]

    with pytest.raises(TaxonomyValidationError):
        _apply(session_factory, runner, course_id, topics, edges)

    assert _course(session_factory, course_id).taxonomy_version == 1
    assert runner.calls == []


# --------------------------------------------------------------------------
# (7) Slug behavior: renamed topic gets a new slug (structural only -- a
#     pure rename patches in place and never touches the slug); collisions
#     dedupe with a -2 suffix.
# --------------------------------------------------------------------------


def test_renamed_topic_gets_a_new_slug_when_structural(session_factory, runner, course_id):
    ids = _seed_v1(session_factory, course_id, [("intro", "Intro", "d1"), ("advanced", "Advanced", "d2")])

    # Renaming "intro" alongside adding a new topic forces the structural
    # path (a pure rename alone would patch, leaving the slug untouched).
    topics = [
        TopicEditIn(id=ids["intro"], name="Introduction Basics", description="d1"),
        TopicEditIn(id=ids["advanced"], name="Advanced", description="d2"),
        TopicEditIn(id=None, name="New Topic", description="d3"),
    ]

    result = _apply(session_factory, runner, course_id, topics)
    assert result.taxonomy_version == 2

    v2 = {t.name: t for t in _topics_at(session_factory, course_id, 2)}
    assert v2["Introduction Basics"].slug == "introduction-basics"
    assert v2["Introduction Basics"].slug != "intro"
    assert v2["Advanced"].slug == "advanced"  # untouched topic keeps its old slug
    assert v2["New Topic"].slug == "new-topic"


def test_slug_collision_dedupes_with_numeric_suffix(session_factory, runner, course_id):
    ids = _seed_v1(session_factory, course_id, [("intro", "Intro", "d1"), ("advanced", "Advanced", "d2")])

    # Rename "intro" to the same name as "advanced" (forced structural by
    # also adding a third topic) -- its new slug must not collide with
    # "advanced"'s kept slug.
    topics = [
        TopicEditIn(id=ids["intro"], name="Advanced", description="d1"),
        TopicEditIn(id=ids["advanced"], name="Advanced", description="d2"),
        TopicEditIn(id=None, name="Third", description="d3"),
    ]

    result = _apply(session_factory, runner, course_id, topics)
    assert result.taxonomy_version == 2

    v2 = _topics_at(session_factory, course_id, 2)
    slugs = [t.slug for t in v2]
    assert len(slugs) == len(set(slugs))  # all unique
    assert "advanced" in slugs
    assert "advanced-2" in slugs
