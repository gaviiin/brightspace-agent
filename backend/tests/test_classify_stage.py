"""Tests for the S3 classify stage: worklist selection against the current
taxonomy version, multi-label writes, post-validation, per-item failure
isolation, and llm_cache reuse keyed on (material sha, taxonomy version).
All against MockBackend or small stub backends -- no network, no API key.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import select

from brightspace_agent.agents.llm import LLMCallError, MockBackend
from brightspace_agent.agents.schemas import ClassificationOut, TopicAssignment
from brightspace_agent.db.models import Course, LlmCache, Material, MaterialTopic, Topic
from brightspace_agent.db.session import init_db
from brightspace_agent.pipeline.stages.classify import PROMPT_VERSION, run_classify_stage


@pytest.fixture
def session_factory(tmp_path):
    return init_db(tmp_path / "brightspace.db")[1]


@pytest.fixture
def course_id(session_factory):
    with session_factory() as session:
        course = Course(
            d2l_org_unit_id=1,
            tenant_origin="school.d2l.com",
            name="Data Structures and Algorithms",
            code="CS 2110",
        )
        session.add(course)
        session.commit()
        return course.id


@pytest.fixture
def backend():
    return MockBackend()


# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------


class _CountingBackend:
    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls = 0
        self.prompts: list[str] = []

    def structured_call(self, schema, *, system, user, tier):
        self.calls += 1
        self.prompts.append(user)
        return self._inner.structured_call(schema, system=system, user=user, tier=tier)

    def model_for_tier(self, tier):
        return self._inner.model_for_tier(tier)


class _StubBackend:
    """Returns `result` (or `result(user)` when callable) for every call."""

    def __init__(self, result) -> None:
        self._result = result
        self.calls = 0

    def structured_call(self, schema, *, system, user, tier):
        self.calls += 1
        parsed = self._result(user) if callable(self._result) else self._result
        usage = {
            "model": self.model_for_tier(tier),
            "input_tokens": 0,
            "output_tokens": 0,
            "est_cost_usd": 0.0,
        }
        return parsed, usage

    def model_for_tier(self, tier):
        return f"stub-{tier}"


# --------------------------------------------------------------------------
# Seeding helpers
# --------------------------------------------------------------------------

TAXONOMY_V1 = [
    ("arrays-and-lists", "Arrays and Lists", "Contiguous storage, dynamic arrays, amortized cost."),
    ("sorting-algorithms", "Sorting Algorithms", "Comparison sorts, quicksort, mergesort, lower bounds."),
    ("graph-algorithms", "Graph Algorithms", "Traversal, shortest paths, spanning trees."),
]


def _write_taxonomy(session_factory, course_id, version, topics) -> dict[str, int]:
    with session_factory() as session:
        ids = {}
        for order_index, (slug, name, description) in enumerate(topics):
            topic = Topic(
                course_id=course_id,
                taxonomy_version=version,
                slug=slug,
                name=name,
                description=description,
                order_index=order_index,
                created_by="agent",
            )
            session.add(topic)
            session.flush()
            ids[slug] = topic.id
        course = session.get(Course, course_id)
        course.taxonomy_version = version
        session.commit()
        return ids


def _add_material(
    session_factory, course_id, *, title, kind="document", sha256=None, status="summarized", summary=None,
    key_terms=("alpha", "beta"),
) -> int:
    with session_factory() as session:
        material = Material(
            course_id=course_id,
            kind=kind,
            title=title,
            mime="text/plain",
            sha256=sha256 if sha256 is not None else f"sha-{title.lower().replace(' ', '-')}",
            size_bytes=100,
            summary=summary or f"{title}: a summary describing what this material covers.",
            summary_meta_json=json.dumps(
                {
                    "model": "mock-fast",
                    "prompt_version": "s1.v1",
                    "key_terms": list(key_terms),
                    "doc_kind_guess": kind,
                    "usage": {},
                }
            ),
            status=status,
        )
        session.add(material)
        session.commit()
        return material.id


def _run(session_factory, backend, course_id, **kwargs):
    return asyncio.run(run_classify_stage(session_factory, backend, course_id, **kwargs))


def _rows(session_factory, *, material_id=None, version=None) -> list[MaterialTopic]:
    with session_factory() as session:
        stmt = select(MaterialTopic)
        if material_id is not None:
            stmt = stmt.where(MaterialTopic.material_id == material_id)
        if version is not None:
            stmt = stmt.where(MaterialTopic.taxonomy_version == version)
        rows = list(session.execute(stmt).scalars().all())
        for row in rows:
            session.expunge(row)
        return rows


def _slug_of(session_factory, topic_id) -> str:
    with session_factory() as session:
        return session.get(Topic, topic_id).slug


# --------------------------------------------------------------------------
# (1) Happy path: multi-label rows at the current version
# --------------------------------------------------------------------------


def test_writes_multi_label_rows_at_current_version(session_factory, backend, course_id):
    _write_taxonomy(session_factory, course_id, 1, TAXONOMY_V1)
    material_id = _add_material(session_factory, course_id, title="Lecture 5 Quicksort", kind="slides")

    stats = _run(session_factory, backend, course_id)

    rows = _rows(session_factory, material_id=material_id)
    assert len(rows) == 2  # the mock assigns the first two taxonomy slugs
    by_slug = {_slug_of(session_factory, row.topic_id): row for row in rows}
    assert set(by_slug) == {"arrays-and-lists", "sorting-algorithms"}
    assert by_slug["arrays-and-lists"].confidence == pytest.approx(0.9)
    assert by_slug["sorting-algorithms"].confidence == pytest.approx(0.4)
    for row in rows:
        assert row.taxonomy_version == 1
        assert row.method == "llm"
        assert row.review_status == "auto"
        assert row.rationale

    assert stats.classified == 1
    assert stats.assignments == 2
    assert stats.failed == 0


def test_prompt_carries_numbered_taxonomy_and_material_details(session_factory, backend, course_id):
    _write_taxonomy(session_factory, course_id, 1, TAXONOMY_V1)
    _add_material(
        session_factory, course_id,
        title="Lecture 5 Quicksort", kind="slides",
        summary="Covers quicksort partitioning and pivot choice.",
        key_terms=["quicksort", "pivot"],
    )
    counting = _CountingBackend(backend)

    _run(session_factory, counting, course_id)

    prompt = counting.prompts[0]
    assert "1. arrays-and-lists" in prompt
    assert "2. sorting-algorithms" in prompt
    assert "3. graph-algorithms" in prompt
    assert "Comparison sorts, quicksort, mergesort" in prompt  # topic descriptions
    assert "Lecture 5 Quicksort" in prompt
    assert "slides" in prompt
    assert "quicksort, pivot" in prompt  # key terms
    assert "Covers quicksort partitioning" in prompt  # summary


def test_only_summarized_materials_are_classified(session_factory, backend, course_id):
    _write_taxonomy(session_factory, course_id, 1, TAXONOMY_V1)
    summarized_id = _add_material(session_factory, course_id, title="Summarized One")
    link_id = _add_material(session_factory, course_id, title="Course Website", kind="link", status="fetched")
    failed_id = _add_material(session_factory, course_id, title="Broken Upload", status="failed")

    stats = _run(session_factory, backend, course_id)

    assert len(_rows(session_factory, material_id=summarized_id)) == 2
    assert _rows(session_factory, material_id=link_id) == []
    assert _rows(session_factory, material_id=failed_id) == []
    assert stats.classified == 1


# --------------------------------------------------------------------------
# (2) Post-validation
# --------------------------------------------------------------------------


def test_unknown_slug_assignments_are_dropped(session_factory, course_id):
    _write_taxonomy(session_factory, course_id, 1, TAXONOMY_V1)
    material_id = _add_material(session_factory, course_id, title="Lecture 1")
    stub = _StubBackend(
        ClassificationOut(
            assignments=[
                TopicAssignment(topic_slug="hallucinated-topic", confidence=0.95, rationale="not real"),
                TopicAssignment(topic_slug="graph-algorithms", confidence=0.8, rationale="mentions BFS"),
            ]
        )
    )

    stats = _run(session_factory, stub, course_id)

    rows = _rows(session_factory, material_id=material_id)
    assert len(rows) == 1
    assert _slug_of(session_factory, rows[0].topic_id) == "graph-algorithms"
    assert stats.assignments == 1


def test_confidence_is_clamped_to_unit_interval(session_factory, course_id):
    _write_taxonomy(session_factory, course_id, 1, TAXONOMY_V1)
    material_id = _add_material(session_factory, course_id, title="Lecture 1")
    stub = _StubBackend(
        ClassificationOut(
            assignments=[
                TopicAssignment(topic_slug="arrays-and-lists", confidence=1.7, rationale="over"),
                TopicAssignment(topic_slug="sorting-algorithms", confidence=-0.2, rationale="under"),
            ]
        )
    )

    _run(session_factory, stub, course_id)

    confidences = sorted(row.confidence for row in _rows(session_factory, material_id=material_id))
    assert confidences == [0.0, 1.0]


def test_empty_and_all_dropped_assignments_write_nothing(session_factory, course_id):
    _write_taxonomy(session_factory, course_id, 1, TAXONOMY_V1)
    empty_id = _add_material(session_factory, course_id, title="Fits Nothing")
    stub = _StubBackend(ClassificationOut(assignments=[]))

    stats = _run(session_factory, stub, course_id)

    assert _rows(session_factory, material_id=empty_id) == []
    assert stats.unassigned == 1
    assert stats.assignments == 0
    # The material is left alone for S4 to orphan; its status is untouched.
    with session_factory() as session:
        assert session.get(Material, empty_id).status == "summarized"


# --------------------------------------------------------------------------
# (3) + (4) Cache reuse and idempotent re-runs
# --------------------------------------------------------------------------


def test_same_sha_reuses_cache_without_a_new_call(session_factory, backend, course_id):
    _write_taxonomy(session_factory, course_id, 1, TAXONOMY_V1)
    _add_material(session_factory, course_id, title="Lecture 5", sha256="shared-sha")
    counting = _CountingBackend(backend)

    _run(session_factory, counting, course_id)
    assert counting.calls == 1

    with session_factory() as session:
        cache_rows = list(session.execute(select(LlmCache).where(LlmCache.stage == "classify")).scalars().all())
    assert len(cache_rows) == 1
    assert cache_rows[0].sha256 == "shared-sha"
    assert cache_rows[0].prompt_version.startswith(f"{PROMPT_VERSION}:tax1")

    # The same bytes appear again (re-synced, or attached under a second
    # module): identical content, so the cached classification is reused.
    mirror_id = _add_material(session_factory, course_id, title="Lecture 5 (mirror)", sha256="shared-sha")

    _run(session_factory, counting, course_id)

    assert counting.calls == 1  # no new LLM call
    assert len(_rows(session_factory, material_id=mirror_id)) == 2


def test_rerun_is_a_no_op_with_no_duplicate_rows(session_factory, backend, course_id):
    _write_taxonomy(session_factory, course_id, 1, TAXONOMY_V1)
    material_id = _add_material(session_factory, course_id, title="Lecture 5")
    counting = _CountingBackend(backend)

    first = _run(session_factory, counting, course_id)
    assert first.classified == 1
    assert counting.calls == 1

    second = _run(session_factory, counting, course_id)

    assert counting.calls == 1  # worklist empty: not even a cache lookup is needed
    assert second.classified == 0
    assert len(_rows(session_factory, material_id=material_id)) == 2


# --------------------------------------------------------------------------
# (5) Taxonomy version bump
# --------------------------------------------------------------------------


def test_version_bump_reclassifies_and_keeps_old_rows(session_factory, backend, course_id):
    _write_taxonomy(session_factory, course_id, 1, TAXONOMY_V1)
    material_id = _add_material(session_factory, course_id, title="Lecture 5")
    counting = _CountingBackend(backend)

    _run(session_factory, counting, course_id)
    assert counting.calls == 1

    _write_taxonomy(
        session_factory, course_id, 2,
        [
            ("sorting-and-searching", "Sorting and Searching", "Merged sorting/searching unit."),
            ("graphs", "Graphs", "Graph representations and traversal."),
            ("complexity", "Complexity Analysis", "Asymptotic reasoning."),
        ],
    )

    stats = _run(session_factory, counting, course_id)

    assert counting.calls == 2  # the taxonomy version is part of the cache key
    v1_rows = _rows(session_factory, material_id=material_id, version=1)
    v2_rows = _rows(session_factory, material_id=material_id, version=2)
    assert len(v1_rows) == 2  # history retained
    assert len(v2_rows) == 2
    assert {_slug_of(session_factory, row.topic_id) for row in v2_rows} == {
        "sorting-and-searching",
        "graphs",
    }
    assert stats.classified == 1


# --------------------------------------------------------------------------
# (6) Per-item failure isolation
# --------------------------------------------------------------------------


def test_one_failing_material_does_not_abort_the_stage(session_factory, backend, course_id):
    _write_taxonomy(session_factory, course_id, 1, TAXONOMY_V1)
    doomed_id = _add_material(session_factory, course_id, title="Doomed Material")
    ok_ids = [_add_material(session_factory, course_id, title=f"Fine Material {i}") for i in range(3)]

    class _FlakyBackend:
        def __init__(self, inner):
            self._inner = inner
            self.calls = 0

        def structured_call(self, schema, *, system, user, tier):
            self.calls += 1
            if "Doomed Material" in user:
                raise LLMCallError("simulated model failure")
            return self._inner.structured_call(schema, system=system, user=user, tier=tier)

        def model_for_tier(self, tier):
            return self._inner.model_for_tier(tier)

    flaky = _FlakyBackend(backend)

    stats = _run(session_factory, flaky, course_id, concurrency=2)

    assert _rows(session_factory, material_id=doomed_id) == []
    with session_factory() as session:
        assert session.get(Material, doomed_id).status == "summarized"  # left retryable
    for material_id in ok_ids:
        assert len(_rows(session_factory, material_id=material_id)) == 2
    assert stats.failed == 1
    assert stats.classified == 3

    # The failure was not cached, so a later run retries it.
    with session_factory() as session:
        cached_shas = {
            row.sha256 for row in session.execute(select(LlmCache).where(LlmCache.stage == "classify")).scalars()
        }
    assert "sha-doomed-material" not in cached_shas


# --------------------------------------------------------------------------
# Degenerate input
# --------------------------------------------------------------------------


def test_no_taxonomy_yet_is_a_no_op(session_factory, backend, course_id):
    _add_material(session_factory, course_id, title="Lecture 5")
    counting = _CountingBackend(backend)

    stats = _run(session_factory, counting, course_id)

    assert counting.calls == 0
    assert stats.classified == 0
    assert _rows(session_factory) == []


def test_progress_callback_fires_per_material(session_factory, backend, course_id):
    _write_taxonomy(session_factory, course_id, 1, TAXONOMY_V1)
    for i in range(3):
        _add_material(session_factory, course_id, title=f"Lecture {i}")
    seen: list[str] = []

    _run(session_factory, backend, course_id, progress=seen.append)

    assert len(seen) == 3
