"""Tests for S4 graph assembly (`graph/build.py`): the exact camelCase JSON
the frontend consumes, current-taxonomy-version scoping, orphan handling via
the synthetic Unsorted topic, and the "every material lands somewhere"
invariant. Pure DB reads -- no LLM involved.
"""

from __future__ import annotations

import pytest

from brightspace_agent.db.models import Course, Material, MaterialTopic, Topic, TopicEdge
from brightspace_agent.db.session import init_db
from brightspace_agent.graph.build import UNSORTED_TOPIC_ID, UNSORTED_TOPIC_SLUG, build_graph


@pytest.fixture
def session_factory(tmp_path):
    return init_db(tmp_path / "brightspace.db")[1]


@pytest.fixture
def course_id(session_factory):
    with session_factory() as session:
        course = Course(d2l_org_unit_id=1, tenant_origin="school.d2l.com", name="CS 2110")
        session.add(course)
        session.commit()
        return course.id


def _add_topics(session, course_id, version, specs) -> dict[str, int]:
    ids = {}
    for order_index, (slug, name) in enumerate(specs):
        topic = Topic(
            course_id=course_id,
            taxonomy_version=version,
            slug=slug,
            name=name,
            description=f"What {name} covers in this course.",
            order_index=order_index,
            created_by="agent",
        )
        session.add(topic)
        session.flush()
        ids[slug] = topic.id
    return ids


def _add_material(session, course_id, *, title, kind="document", status="summarized") -> int:
    material = Material(
        course_id=course_id,
        kind=kind,
        title=title,
        sha256=f"sha-{title.lower().replace(' ', '-')}",
        status=status,
    )
    session.add(material)
    session.flush()
    return material.id


@pytest.fixture
def seeded(session_factory, course_id):
    """A course at taxonomy version 1 with three topics, two attached
    materials, one orphan link, one failed material, and two topic edges --
    plus leftover version-0 rows that must never appear in the output."""
    with session_factory() as session:
        stale_ids = _add_topics(session, course_id, 0, [("old-topic", "Old Topic")])
        topic_ids = _add_topics(
            session, course_id, 1,
            [("arrays-and-lists", "Arrays and Lists"), ("sorting", "Sorting"), ("graphs", "Graphs")],
        )
        session.get(Course, course_id).taxonomy_version = 1

        lecture_id = _add_material(session, course_id, title="Lecture 5", kind="slides")
        assignment_id = _add_material(session, course_id, title="Assignment 3", kind="assignment")
        orphan_id = _add_material(session, course_id, title="Course Website", kind="link", status="fetched")
        failed_id = _add_material(session, course_id, title="Broken Upload", status="failed")

        session.add_all(
            [
                MaterialTopic(
                    material_id=lecture_id, topic_id=topic_ids["arrays-and-lists"], taxonomy_version=1,
                    confidence=0.9, rationale="Covers dynamic arrays.", method="llm", review_status="auto",
                ),
                MaterialTopic(
                    material_id=lecture_id, topic_id=topic_ids["sorting"], taxonomy_version=1,
                    confidence=0.4, rationale="Sorts as an example.", method="llm", review_status="auto",
                ),
                MaterialTopic(
                    material_id=assignment_id, topic_id=topic_ids["sorting"], taxonomy_version=1,
                    confidence=0.7, rationale="Implements mergesort.", method="llm", review_status="auto",
                ),
                # Stale rows at version 0 and an attachment for the failed
                # material -- both must be filtered out.
                MaterialTopic(
                    material_id=assignment_id, topic_id=stale_ids["old-topic"], taxonomy_version=0,
                    confidence=1.0, rationale="Old taxonomy.", method="llm", review_status="auto",
                ),
                MaterialTopic(
                    material_id=failed_id, topic_id=topic_ids["graphs"], taxonomy_version=1,
                    confidence=0.5, rationale="Never mind.", method="llm", review_status="auto",
                ),
                TopicEdge(
                    course_id=course_id, from_topic_id=topic_ids["arrays-and-lists"],
                    to_topic_id=topic_ids["sorting"], relation="prerequisite", created_by="agent",
                ),
                TopicEdge(
                    course_id=course_id, from_topic_id=topic_ids["sorting"],
                    to_topic_id=topic_ids["graphs"], relation="related", created_by="agent",
                ),
                # An edge between version-0 topics: wrong version, excluded.
                TopicEdge(
                    course_id=course_id, from_topic_id=stale_ids["old-topic"],
                    to_topic_id=stale_ids["old-topic"], relation="related", created_by="agent",
                ),
            ]
        )
        session.commit()

        return {
            "topic_ids": topic_ids,
            "stale_topic_id": stale_ids["old-topic"],
            "lecture_id": lecture_id,
            "assignment_id": assignment_id,
            "orphan_id": orphan_id,
            "failed_id": failed_id,
        }


def _build(session_factory, course_id) -> dict:
    with session_factory() as session:
        return build_graph(session, course_id)


# --------------------------------------------------------------------------
# (1) Shape and key names
# --------------------------------------------------------------------------


def test_graph_shape_and_camel_case_keys(session_factory, course_id, seeded):
    graph = _build(session_factory, course_id)

    assert set(graph) == {"topics", "materials", "topicEdges", "attachments", "meta"}
    assert set(graph["topics"][0]) == {"id", "slug", "name", "description", "orderIndex", "materialCount"}
    assert set(graph["materials"][0]) == {"id", "title", "kind", "status", "maxConfidence"}
    assert set(graph["topicEdges"][0]) == {"fromTopicId", "toTopicId", "relation"}
    assert set(graph["attachments"][0]) == {"topicId", "materialId", "confidence", "rationale"}
    assert set(graph["meta"]) == {"taxonomyVersion", "orphanCount"}
    assert graph["meta"]["taxonomyVersion"] == 1

    # Only current-version topics (plus Unsorted); the version-0 topic is gone.
    assert [topic["slug"] for topic in graph["topics"]] == [
        "arrays-and-lists",
        "sorting",
        "graphs",
        UNSORTED_TOPIC_SLUG,
    ]
    assert [topic["orderIndex"] for topic in graph["topics"]] == [0, 1, 2, 3]
    assert seeded["stale_topic_id"] not in {topic["id"] for topic in graph["topics"]}


def test_failed_materials_are_excluded_entirely(session_factory, course_id, seeded):
    graph = _build(session_factory, course_id)

    material_ids = {material["id"] for material in graph["materials"]}
    assert seeded["failed_id"] not in material_ids
    assert material_ids == {seeded["lecture_id"], seeded["assignment_id"], seeded["orphan_id"]}
    assert all(attachment["materialId"] != seeded["failed_id"] for attachment in graph["attachments"])


# --------------------------------------------------------------------------
# (2) materialCount + maxConfidence
# --------------------------------------------------------------------------


def test_material_count_per_topic(session_factory, course_id, seeded):
    graph = _build(session_factory, course_id)

    counts = {topic["slug"]: topic["materialCount"] for topic in graph["topics"]}
    assert counts["arrays-and-lists"] == 1
    assert counts["sorting"] == 2
    assert counts["graphs"] == 0  # its only attachment belonged to a failed material
    assert counts[UNSORTED_TOPIC_SLUG] == 1


def test_max_confidence_per_material(session_factory, course_id, seeded):
    graph = _build(session_factory, course_id)

    by_id = {material["id"]: material for material in graph["materials"]}
    assert by_id[seeded["lecture_id"]]["maxConfidence"] == pytest.approx(0.9)
    assert by_id[seeded["assignment_id"]]["maxConfidence"] == pytest.approx(0.7)
    assert by_id[seeded["orphan_id"]]["maxConfidence"] is None  # orphan: unscored


# --------------------------------------------------------------------------
# (3) Orphans
# --------------------------------------------------------------------------


def test_orphan_material_lands_in_the_unsorted_topic(session_factory, course_id, seeded):
    graph = _build(session_factory, course_id)

    unsorted = next(topic for topic in graph["topics"] if topic["id"] == UNSORTED_TOPIC_ID)
    assert unsorted["slug"] == UNSORTED_TOPIC_SLUG
    assert unsorted["name"] == "Unsorted"
    assert unsorted["description"]

    orphan_attachments = [
        attachment for attachment in graph["attachments"] if attachment["topicId"] == UNSORTED_TOPIC_ID
    ]
    assert len(orphan_attachments) == 1
    assert orphan_attachments[0]["materialId"] == seeded["orphan_id"]
    assert orphan_attachments[0]["confidence"] is None
    assert orphan_attachments[0]["rationale"] is None
    assert graph["meta"]["orphanCount"] == 1


def test_no_unsorted_topic_when_every_material_is_attached(session_factory, course_id, seeded):
    with session_factory() as session:
        session.add(
            MaterialTopic(
                material_id=seeded["orphan_id"], topic_id=seeded["topic_ids"]["graphs"], taxonomy_version=1,
                confidence=0.6, rationale="Linked reference on graphs.", method="llm", review_status="auto",
            )
        )
        session.commit()

    graph = _build(session_factory, course_id)

    assert all(topic["id"] != UNSORTED_TOPIC_ID for topic in graph["topics"])
    assert graph["meta"]["orphanCount"] == 0


def test_course_without_a_taxonomy_puts_everything_in_unsorted(session_factory, course_id):
    with session_factory() as session:
        _add_material(session, course_id, title="Lecture 1")
        _add_material(session, course_id, title="Lecture 2")
        session.commit()

    graph = _build(session_factory, course_id)

    assert graph["meta"]["taxonomyVersion"] == 0
    assert [topic["id"] for topic in graph["topics"]] == [UNSORTED_TOPIC_ID]
    assert graph["meta"]["orphanCount"] == 2
    assert len(graph["attachments"]) == 2


# --------------------------------------------------------------------------
# (4) Edges reference DB ids
# --------------------------------------------------------------------------


def test_topic_edges_reference_db_topic_ids(session_factory, course_id, seeded):
    graph = _build(session_factory, course_id)

    topic_ids = seeded["topic_ids"]
    assert graph["topicEdges"] == [
        {
            "fromTopicId": topic_ids["arrays-and-lists"],
            "toTopicId": topic_ids["sorting"],
            "relation": "prerequisite",
        },
        {"fromTopicId": topic_ids["sorting"], "toTopicId": topic_ids["graphs"], "relation": "related"},
    ]
    for edge in graph["topicEdges"]:
        assert isinstance(edge["fromTopicId"], int)
        assert edge["fromTopicId"] in {topic["id"] for topic in graph["topics"]}
        assert edge["toTopicId"] in {topic["id"] for topic in graph["topics"]}


# --------------------------------------------------------------------------
# (5) Invariant: every non-failed material is reachable
# --------------------------------------------------------------------------


def test_every_non_failed_material_appears_in_at_least_one_attachment(session_factory, course_id, seeded):
    graph = _build(session_factory, course_id)

    attached = {attachment["materialId"] for attachment in graph["attachments"]}
    assert attached == {material["id"] for material in graph["materials"]}

    counts = {topic["id"]: topic["materialCount"] for topic in graph["topics"]}
    assert sum(counts.values()) == len(graph["attachments"])


def test_unknown_course_raises(session_factory):
    with session_factory() as session, pytest.raises(ValueError):
        build_graph(session, 12345)
