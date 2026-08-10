"""S4 graph assembly: `build_graph(session, course_id)` -> the course graph as
plain JSON-able data.

Pure and deterministic -- no LLM, no writes, no HTTP concerns. Task 9's
`GET /api/courses/{id}/graph` returns this dict verbatim, so the keys here
are camelCase: they are the frontend's contract, not the database's.

Four rules shape the output:

1. **Current taxonomy version only.** Topics, edges, and assignments at older
   versions stay in the database as history but never reach the UI.
2. **Failed materials are invisible.** They have no usable content; showing
   them would just be noise hanging off the graph.
3. **Every remaining material lands somewhere.** Anything with no assignment
   at the current version -- never-summarized links, materials the classifier
   honestly placed nowhere, everything when a course has no taxonomy yet --
   attaches to the synthetic "Unsorted" topic (id 0) so nothing can silently
   disappear from the student's view. The invariant is checked, not assumed.
4. **Administrative materials get their own bucket** (M3.5a). A material S3
   flagged `is_administrative` (grades, scheduling, office hours, logistics)
   is EXCLUDED from Unsorted and from `meta.orphanCount` -- it was never a
   miss to review, so counting it there would make Unsorted, and the
   "how much is genuinely unfiled" signal, useless. It attaches instead to
   the synthetic "Logistics & admin" topic (id -1, `meta.adminCount`),
   overriding any real topic assignment it might somehow also carry (S3
   doesn't write any, but this stage doesn't trust that from a distance).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from brightspace_agent.db.models import Course, Material, MaterialTopic, Topic, TopicEdge

UNSORTED_TOPIC_ID = 0
UNSORTED_TOPIC_SLUG = "_unsorted"
UNSORTED_TOPIC_NAME = "Unsorted"
UNSORTED_TOPIC_DESCRIPTION = (
    "Materials that aren't filed under any topic yet -- links that were never "
    "summarized, or materials the classifier couldn't place. Drag them onto a "
    "topic, or edit the taxonomy to make room for them."
)

# M3.5a: the next reserved synthetic-topic id after Unsorted's 0 -- reserved
# ids count down from 0 so they can never collide with a real `topics.id`
# (an AUTOINCREMENT primary key, always >= 1).
ADMIN_TOPIC_ID = -1
ADMIN_TOPIC_SLUG = "_admin"
ADMIN_TOPIC_NAME = "Logistics & admin"
ADMIN_TOPIC_DESCRIPTION = (
    "Grades, scheduling, office hours, and other course-mechanics materials -- "
    "flagged automatically as not being course content, so they don't crowd "
    "out genuinely unfiled materials in Unsorted."
)


def build_graph(session: Session, course_id: int) -> dict:
    course = session.get(Course, course_id)
    if course is None:
        raise ValueError(f"no course with id {course_id}")
    version = course.taxonomy_version

    topic_rows = list(
        session.execute(
            select(Topic)
            .where(Topic.course_id == course_id, Topic.taxonomy_version == version)
            .order_by(Topic.order_index, Topic.id)
        ).scalars().all()
    )
    topic_ids = {topic.id for topic in topic_rows}

    material_rows = list(
        session.execute(
            select(Material)
            .where(Material.course_id == course_id, Material.status != "failed")
            .order_by(Material.id)
        ).scalars().all()
    )
    material_ids = {material.id for material in material_rows}

    # M3.5a: administrative materials always go to the admin bucket, never a
    # real topic -- excluded here (not just at orphan-computation time below)
    # so materialCount/attachments for real topics stay correct even if a
    # material somehow carries both the flag and a material_topics row (S3
    # doesn't write one alongside the flag, but this stage doesn't lean on
    # that from a distance).
    admin_ids = [material.id for material in material_rows if material.is_administrative]
    admin_id_set = set(admin_ids)

    assignment_rows = [
        row
        for row in session.execute(
            select(MaterialTopic)
            .where(MaterialTopic.taxonomy_version == version)
            .order_by(MaterialTopic.material_id, MaterialTopic.id)
        ).scalars().all()
        if row.topic_id in topic_ids
        and row.material_id in material_ids
        and row.material_id not in admin_id_set
    ]

    edge_rows = [
        row
        for row in session.execute(
            select(TopicEdge).where(TopicEdge.course_id == course_id).order_by(TopicEdge.id)
        ).scalars().all()
        if row.from_topic_id in topic_ids and row.to_topic_id in topic_ids
    ]

    attachments_by_topic: dict[int, list[dict]] = {topic.id: [] for topic in topic_rows}
    max_confidence: dict[int, float | None] = {}
    for row in assignment_rows:
        attachments_by_topic[row.topic_id].append(
            {
                "topicId": row.topic_id,
                "materialId": row.material_id,
                "confidence": row.confidence,
                "rationale": row.rationale,
            }
        )
        if row.confidence is not None:
            current = max_confidence.get(row.material_id)
            max_confidence[row.material_id] = (
                row.confidence if current is None else max(current, row.confidence)
            )

    attached_material_ids = {row.material_id for row in assignment_rows}
    orphan_ids = [
        material.id
        for material in material_rows
        if material.id not in attached_material_ids and material.id not in admin_id_set
    ]

    topics = [
        {
            "id": topic.id,
            "slug": topic.slug,
            "name": topic.name,
            "description": topic.description,
            "orderIndex": index,
            "materialCount": len(attachments_by_topic[topic.id]),
        }
        for index, topic in enumerate(topic_rows)
    ]

    attachments: list[dict] = []
    for topic in topic_rows:
        attachments.extend(attachments_by_topic[topic.id])

    # Admin bucket before Unsorted -- order doesn't matter functionally
    # (orderIndex is read off `len(topics)` at append time either way), but
    # this keeps the two synthetic buckets in a stable, predictable order.
    if admin_ids:
        topics.append(
            {
                "id": ADMIN_TOPIC_ID,
                "slug": ADMIN_TOPIC_SLUG,
                "name": ADMIN_TOPIC_NAME,
                "description": ADMIN_TOPIC_DESCRIPTION,
                "orderIndex": len(topics),
                "materialCount": len(admin_ids),
            }
        )
        attachments.extend(
            {
                "topicId": ADMIN_TOPIC_ID,
                "materialId": material_id,
                "confidence": None,
                "rationale": None,
            }
            for material_id in admin_ids
        )

    if orphan_ids:
        topics.append(
            {
                "id": UNSORTED_TOPIC_ID,
                "slug": UNSORTED_TOPIC_SLUG,
                "name": UNSORTED_TOPIC_NAME,
                "description": UNSORTED_TOPIC_DESCRIPTION,
                "orderIndex": len(topics),
                "materialCount": len(orphan_ids),
            }
        )
        attachments.extend(
            {
                "topicId": UNSORTED_TOPIC_ID,
                "materialId": material_id,
                "confidence": None,
                "rationale": None,
            }
            for material_id in orphan_ids
        )

    materials = [
        {
            "id": material.id,
            "title": material.title,
            "kind": material.kind,
            "status": material.status,
            "maxConfidence": max_confidence.get(material.id),
        }
        for material in material_rows
    ]

    graph = {
        "topics": topics,
        "materials": materials,
        "topicEdges": [
            {"fromTopicId": row.from_topic_id, "toTopicId": row.to_topic_id, "relation": row.relation}
            for row in edge_rows
        ],
        "attachments": attachments,
        "meta": {
            "taxonomyVersion": version,
            "orphanCount": len(orphan_ids),  # genuinely unfiled -- excludes admin materials
            "adminCount": len(admin_ids),
        },
    }

    _check_every_material_is_reachable(graph, material_ids, course_id)
    return graph


def _check_every_material_is_reachable(graph: dict, material_ids: set[int], course_id: int) -> None:
    """A material that reaches the UI attached to nothing is invisible to the
    student -- the exact failure mode the Unsorted topic exists to prevent,
    so it is checked rather than trusted."""
    attached = {attachment["materialId"] for attachment in graph["attachments"]}
    if attached != material_ids:
        missing = sorted(material_ids - attached)
        raise RuntimeError(
            f"graph for course {course_id} would hide {len(missing)} material(s) "
            f"with no attachment: {missing[:10]}"
        )

    counted = sum(topic["materialCount"] for topic in graph["topics"])
    if counted != len(graph["attachments"]):
        raise RuntimeError(
            f"graph for course {course_id} has inconsistent materialCount "
            f"({counted}) vs attachments ({len(graph['attachments'])})"
        )
