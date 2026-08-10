"""Tests for the S3 classify stage: worklist selection against the current
taxonomy version, multi-label writes, post-validation, per-item failure
isolation, and llm_cache reuse keyed on (material sha, taxonomy version).
All against MockBackend or small stub backends -- no network, no API key.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import delete, select

from brightspace_agent.agents.llm import LLMCallError, MockBackend
from brightspace_agent.agents.schemas import ClassificationOut, TopicAssignment
from brightspace_agent.db.models import Course, LlmCache, Material, MaterialTopic, MediaSource, Topic
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
    key_terms=("alpha", "beta"), is_administrative=0,
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
            is_administrative=is_administrative,
        )
        session.add(material)
        session.commit()
        return material.id


def _add_media_source(
    session_factory, course_id, *, material_id, transcript_material_id, url,
    platform="zoom", status="done",
) -> int:
    with session_factory() as session:
        source = MediaSource(
            course_id=course_id,
            material_id=material_id,
            transcript_material_id=transcript_material_id,
            platform=platform,
            url=url,
            status=status,
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        session.add(source)
        session.commit()
        return source.id


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
# (2b) M3.5a: administrative materials
# --------------------------------------------------------------------------


def test_administrative_material_gets_flag_and_no_topic_rows(session_factory, course_id):
    _write_taxonomy(session_factory, course_id, 1, TAXONOMY_V1)
    material_id = _add_material(session_factory, course_id, title="Final Grades Posted")
    # The model may (wrongly) also return assignments alongside
    # is_administrative=True -- classify.py must drop them regardless.
    stub = _StubBackend(
        ClassificationOut(
            assignments=[TopicAssignment(topic_slug="graph-algorithms", confidence=0.9, rationale="noise")],
            is_administrative=True,
        )
    )

    stats = _run(session_factory, stub, course_id)

    assert _rows(session_factory, material_id=material_id) == []
    with session_factory() as session:
        assert session.get(Material, material_id).is_administrative == 1
    assert stats.assignments == 0
    assert stats.classified == 0
    assert stats.unassigned == 1


def test_non_administrative_material_classifies_as_before(session_factory, course_id):
    _write_taxonomy(session_factory, course_id, 1, TAXONOMY_V1)
    material_id = _add_material(session_factory, course_id, title="Lecture 5 Quicksort")
    stub = _StubBackend(
        ClassificationOut(
            assignments=[TopicAssignment(topic_slug="sorting-algorithms", confidence=0.9, rationale="quicksort")],
            is_administrative=False,
        )
    )

    stats = _run(session_factory, stub, course_id)

    rows = _rows(session_factory, material_id=material_id)
    assert len(rows) == 1
    assert stats.classified == 1
    with session_factory() as session:
        assert session.get(Material, material_id).is_administrative == 0


def test_reclassify_clears_then_rederives_the_administrative_flag(session_factory, course_id):
    """A material re-synced with changed bytes must not keep a stale
    administrative flag from its old content: reset_pipeline_progress clears
    it immediately (same lifecycle as material_topics rows), and the next
    classify run re-derives it fresh from the new content."""
    from brightspace_agent.ingest import repo

    _write_taxonomy(session_factory, course_id, 1, TAXONOMY_V1)
    material_id = _add_material(session_factory, course_id, title="Office Hours Notice", sha256="a" * 64)
    with session_factory() as session:
        material = session.get(Material, material_id)
        material.d2l_topic_id = 2001
        session.commit()

    admin_stub = _StubBackend(ClassificationOut(assignments=[], is_administrative=True))
    _run(session_factory, admin_stub, course_id)

    with session_factory() as session:
        assert session.get(Material, material_id).is_administrative == 1

    # Re-synced with genuinely different bytes -- reset_pipeline_progress
    # runs, and must clear the stale flag right away, before any
    # reclassification happens.
    with session_factory() as session:
        repo.upsert_file_material(
            session,
            course_id=course_id,
            d2l_topic_id=2001,
            sha256="b" * 64,
            mime="text/plain",
            size_bytes=42,
            source_url="https://tenant.example/topics/office-hours",
            title="Office Hours Notice",
            d2l_updated_at="2026-03-01T00:00:00.000Z",
        )
        session.commit()

    with session_factory() as session:
        material = session.get(Material, material_id)
        assert material.is_administrative == 0
        assert material.status == "fetched"

    # S1 would re-summarize the new bytes; simulate that directly so S3 gets
    # its turn against genuinely different (non-administrative) content.
    with session_factory() as session:
        material = session.get(Material, material_id)
        material.status = "summarized"
        material.summary = "Covers dynamic array resizing and amortized cost."
        session.commit()

    content_stub = _StubBackend(
        ClassificationOut(
            assignments=[TopicAssignment(topic_slug="arrays-and-lists", confidence=0.8, rationale="resizing")],
            is_administrative=False,
        )
    )
    stats = _run(session_factory, content_stub, course_id)

    rows = _rows(session_factory, material_id=material_id)
    assert len(rows) == 1
    assert stats.classified == 1
    with session_factory() as session:
        assert session.get(Material, material_id).is_administrative == 0


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
    # Keyed on the taxonomy's *content*, not its version number.
    assert cache_rows[0].prompt_version.startswith(f"{PROMPT_VERSION}:")
    assert "tax1" not in cache_rows[0].prompt_version

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


def test_unusable_cache_row_is_replaced_rather_than_wedging_the_material(session_factory, backend, course_id):
    """A JSON-valid but schema-invalid row must be a miss, not a permanent
    failure: validating only at the point of use would raise on every run,
    and an insert-or-ignore write would never replace the bad row."""
    _write_taxonomy(session_factory, course_id, 1, TAXONOMY_V1)
    material_id = _add_material(session_factory, course_id, title="Lecture 5", sha256="poison-sha")
    counting = _CountingBackend(backend)
    _run(session_factory, counting, course_id)
    assert counting.calls == 1

    with session_factory() as session:
        session.execute(delete(MaterialTopic).where(MaterialTopic.material_id == material_id))
        row = session.execute(select(LlmCache).where(LlmCache.stage == "classify")).scalar_one()
        row.output_json = json.dumps({"assignments": "not a list at all"})
        session.commit()

    stats = _run(session_factory, counting, course_id)

    assert counting.calls == 2  # re-asked the model instead of raising
    assert stats.failed == 0
    assert len(_rows(session_factory, material_id=material_id)) == 2
    with session_factory() as session:
        rows = list(session.execute(select(LlmCache).where(LlmCache.stage == "classify")).scalars().all())
    assert len(rows) == 1
    assert json.loads(rows[0].output_json)["assignments"]  # the poison was overwritten


def test_more_than_three_assignments_keeps_the_top_three(session_factory, course_id):
    _write_taxonomy(
        session_factory, course_id, 1,
        [*TAXONOMY_V1, ("complexity", "Complexity", "Asymptotics."), ("recursion", "Recursion", "Base cases.")],
    )
    material_id = _add_material(session_factory, course_id, title="Everything Lecture")
    stub = _StubBackend(
        ClassificationOut(
            assignments=[
                TopicAssignment(topic_slug="arrays-and-lists", confidence=0.5, rationale="d"),
                TopicAssignment(topic_slug="sorting-algorithms", confidence=0.95, rationale="a"),
                TopicAssignment(topic_slug="graph-algorithms", confidence=0.3, rationale="e"),
                TopicAssignment(topic_slug="complexity", confidence=0.9, rationale="b"),
                TopicAssignment(topic_slug="recursion", confidence=0.7, rationale="c"),
            ]
        )
    )

    stats = _run(session_factory, stub, course_id)

    rows = _rows(session_factory, material_id=material_id)
    assert len(rows) == 3
    assert {_slug_of(session_factory, row.topic_id) for row in rows} == {
        "sorting-algorithms",
        "complexity",
        "recursion",
    }
    assert stats.assignments == 3


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


def test_resynced_material_with_changed_bytes_is_reclassified_from_scratch(
    session_factory, backend, course_id
):
    """Regression: changed content used to keep its old topics forever.

    `upsert_file_material` reset status/summary on a sha change but left the
    `material_topics` rows at the current version in place -- and this
    stage's worklist skips any material that already has rows there. So a
    re-uploaded file was re-extracted and re-summarized from the new bytes,
    then never re-classified: it kept the topics its OLD contents earned,
    with no way to repair it short of a taxonomy version bump.
    """
    from brightspace_agent.ingest import repo

    _write_taxonomy(session_factory, course_id, 1, TAXONOMY_V1)
    material_id = _add_material(
        session_factory, course_id, title="Lecture 5 Quicksort", kind="slides", sha256="a" * 64
    )
    with session_factory() as session:
        material = session.get(Material, material_id)
        material.d2l_topic_id = 1001
        session.commit()

    _run(session_factory, backend, course_id)
    assert len(_rows(session_factory, material_id=material_id, version=1)) == 2
    # A stale row at an OLDER version: history, and not this stage's
    # business -- it must survive the re-sync untouched.
    with session_factory() as session:
        session.add(
            MaterialTopic(
                material_id=material_id,
                topic_id=_rows(session_factory, material_id=material_id)[0].topic_id,
                taxonomy_version=0,
                confidence=0.5,
                rationale="from an earlier taxonomy",
                method="llm",
                review_status="auto",
            )
        )
        session.commit()

    # The re-sync: same topic, genuinely different bytes.
    with session_factory() as session:
        repo.upsert_file_material(
            session,
            course_id=course_id,
            d2l_topic_id=1001,
            sha256="b" * 64,
            mime="text/plain",
            size_bytes=120,
            source_url="https://tenant.example/topics/1001/file",
            title="Lecture 5 Quicksort",
            d2l_updated_at="2026-03-01T00:00:00.000Z",
        )
        session.commit()

    assert _rows(session_factory, material_id=material_id, version=1) == []
    assert len(_rows(session_factory, material_id=material_id, version=0)) == 1  # history intact

    # S1 re-extracts and re-summarizes the new bytes; then S3 gets its turn.
    with session_factory() as session:
        material = session.get(Material, material_id)
        assert material.status == "fetched"
        assert material.summary is None
        material.status = "summarized"
        material.summary = "Rewritten lecture: now about graph traversal."
        session.commit()

    stats = _run(session_factory, backend, course_id)

    fresh = _rows(session_factory, material_id=material_id, version=1)
    assert len(fresh) == 2
    assert stats.classified == 1


def test_same_taxonomy_content_at_a_new_version_reuses_the_cache(session_factory, backend, course_id):
    """A version number is not a reason to re-bill a course. Only the
    taxonomy's content is."""
    _write_taxonomy(session_factory, course_id, 1, TAXONOMY_V1)
    material_id = _add_material(session_factory, course_id, title="Lecture 5")
    counting = _CountingBackend(backend)

    _run(session_factory, counting, course_id)
    assert counting.calls == 1

    # Byte-identical taxonomy, written again at version 2 (as an older S2
    # would have done on every sync).
    _write_taxonomy(session_factory, course_id, 2, TAXONOMY_V1)

    stats = _run(session_factory, counting, course_id)

    assert counting.calls == 1  # no new LLM call
    assert stats.cached_hits == 1
    assert len(_rows(session_factory, material_id=material_id, version=2)) == 2
    assert len(_rows(session_factory, material_id=material_id, version=1)) == 2


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


# --------------------------------------------------------------------------
# Cost cap (Task 9)
# --------------------------------------------------------------------------


class _FixedCostBackend:
    """Wraps a backend, reporting a fixed `est_cost_usd` per call regardless
    of what the inner backend actually used."""

    def __init__(self, inner, est_cost_usd: float) -> None:
        self._inner = inner
        self._est_cost_usd = est_cost_usd
        self.calls = 0

    def structured_call(self, schema, *, system, user, tier):
        self.calls += 1
        parsed, usage = self._inner.structured_call(schema, system=system, user=user, tier=tier)
        usage = {**usage, "est_cost_usd": self._est_cost_usd}
        return parsed, usage

    def model_for_tier(self, tier):
        return self._inner.model_for_tier(tier)


def test_cost_cap_stops_the_worklist_and_leaves_the_rest_unprocessed(session_factory, backend, course_id):
    _write_taxonomy(session_factory, course_id, 1, TAXONOMY_V1)
    material_ids = [_add_material(session_factory, course_id, title=f"Lecture {i}") for i in range(3)]

    costly_backend = _FixedCostBackend(backend, est_cost_usd=10.0)
    # concurrency=1: makes "exactly one call, then abort" deterministic
    # rather than a matter of thread timing.
    stats = _run(session_factory, costly_backend, course_id, concurrency=1, cost_cap_usd=5.0)

    assert costly_backend.calls == 1
    assert stats.aborted is True
    assert stats.classified == 1

    rows_by_material = {material_id: _rows(session_factory, material_id=material_id) for material_id in material_ids}
    processed = [material_id for material_id, rows in rows_by_material.items() if rows]
    untouched = [material_id for material_id, rows in rows_by_material.items() if not rows]
    assert len(processed) == 1
    assert len(untouched) == 2
    with session_factory() as session:
        for material_id in untouched:
            assert session.get(Material, material_id).status == "summarized"  # left for a later retry


def test_cost_cap_never_blocks_a_cache_hit(session_factory, backend, course_id):
    _write_taxonomy(session_factory, course_id, 1, TAXONOMY_V1)
    _add_material(session_factory, course_id, title="Lecture 5", sha256="shared-sha")
    _run(session_factory, backend, course_id)  # primes the cache

    mirror_id = _add_material(session_factory, course_id, title="Lecture 5 (mirror)", sha256="shared-sha")

    stats = _run(session_factory, backend, course_id, cost_cap_usd=0.0)

    assert stats.aborted is False
    assert len(_rows(session_factory, material_id=mirror_id)) == 2


# --------------------------------------------------------------------------
# M3.5b: recording topic inheritance
# --------------------------------------------------------------------------


def test_recording_source_inherits_transcript_topics_and_clears_admin_flag(session_factory, backend, course_id):
    """A recording's own material (the link/page the sync found, pointed at
    by media_sources.material_id) rarely has classifiable content of its
    own; a stale S3 run may even have flagged it administrative from a thin
    pseudo-doc. This material is left at status='fetched' (never reaches
    this run's classify worklist) so the post-pass is the ONLY thing that
    can give it real topics."""
    _write_taxonomy(session_factory, course_id, 1, TAXONOMY_V1)
    source_id = _add_material(
        session_factory, course_id, title="Lecture 5 Recording", kind="link", status="fetched",
        is_administrative=1,
    )
    transcript_id = _add_material(
        session_factory, course_id, title="Lecture 5 Recording (transcript)", kind="transcript",
    )
    _add_media_source(
        session_factory, course_id, material_id=source_id, transcript_material_id=transcript_id,
        url="https://zoom.us/rec/share/lecture5",
    )

    _run(session_factory, backend, course_id)

    transcript_rows = _rows(session_factory, material_id=transcript_id)
    assert len(transcript_rows) == 2  # MockBackend's usual two assignments
    assert all(row.method == "llm" for row in transcript_rows)

    source_rows = _rows(session_factory, material_id=source_id)
    assert len(source_rows) == 2
    assert all(row.method == "inherited" for row in source_rows)
    assert all(row.rationale == "inherited from the lecture transcript" for row in source_rows)
    assert {row.topic_id for row in source_rows} == {row.topic_id for row in transcript_rows}
    by_topic = {row.topic_id: row.confidence for row in source_rows}
    for row in transcript_rows:
        assert by_topic[row.topic_id] == pytest.approx(row.confidence)

    with session_factory() as session:
        assert session.get(Material, source_id).is_administrative == 0  # cleared


def test_inheritance_adds_only_missing_topics_and_preserves_direct_assignments(
    session_factory, backend, course_id
):
    """A source material that already carries its own direct assignment at
    this version (e.g. a weak title-only classification) keeps it
    untouched; inheritance only fills in the topics the source itself
    lacks."""
    topic_ids = _write_taxonomy(session_factory, course_id, 1, TAXONOMY_V1)
    source_id = _add_material(
        session_factory, course_id, title="Lecture 5 Recording", kind="link", status="summarized",
    )
    transcript_id = _add_material(
        session_factory, course_id, title="Lecture 5 Recording (transcript)", kind="transcript",
    )
    _add_media_source(
        session_factory, course_id, material_id=source_id, transcript_material_id=transcript_id,
        url="https://zoom.us/rec/share/lecture5",
    )
    with session_factory() as session:
        # A pre-existing direct assignment on the source, at the SAME topic
        # MockBackend will also give the transcript -- this is the row that
        # must survive unmodified.
        session.add(
            MaterialTopic(
                material_id=source_id, topic_id=topic_ids["arrays-and-lists"], taxonomy_version=1,
                confidence=0.3, rationale="weak title match", method="llm", review_status="auto",
            )
        )
        session.commit()

    _run(session_factory, backend, course_id)

    # The transcript, having its own real content, gets classified normally.
    transcript_rows = _rows(session_factory, material_id=transcript_id)
    assert {row.topic_id for row in transcript_rows} == {
        topic_ids["arrays-and-lists"], topic_ids["sorting-algorithms"],
    }

    source_rows = _rows(session_factory, material_id=source_id)
    by_topic = {row.topic_id: row for row in source_rows}
    assert set(by_topic) == {topic_ids["arrays-and-lists"], topic_ids["sorting-algorithms"]}

    # The pre-existing direct row: untouched, not overwritten by the
    # transcript's own (higher) confidence for the same topic.
    direct = by_topic[topic_ids["arrays-and-lists"]]
    assert direct.method == "llm"
    assert direct.confidence == pytest.approx(0.3)
    assert direct.rationale == "weak title match"

    # The gap: filled in by inheritance.
    inherited = by_topic[topic_ids["sorting-algorithms"]]
    assert inherited.method == "inherited"
    assert inherited.rationale == "inherited from the lecture transcript"
    assert inherited.confidence == pytest.approx(0.4)


def test_inheritance_rerun_is_idempotent_no_duplicate_rows(session_factory, backend, course_id):
    _write_taxonomy(session_factory, course_id, 1, TAXONOMY_V1)
    source_id = _add_material(
        session_factory, course_id, title="Lecture 5 Recording", kind="link", status="fetched",
    )
    transcript_id = _add_material(
        session_factory, course_id, title="Lecture 5 Recording (transcript)", kind="transcript",
    )
    _add_media_source(
        session_factory, course_id, material_id=source_id, transcript_material_id=transcript_id,
        url="https://zoom.us/rec/share/lecture5",
    )

    _run(session_factory, backend, course_id)
    first = _rows(session_factory, material_id=source_id)
    assert len(first) == 2

    _run(session_factory, backend, course_id)  # transcript already classified: no new LLM call needed
    second = _rows(session_factory, material_id=source_id)

    assert len(second) == 2  # no duplicates
    assert {row.topic_id for row in second} == {row.topic_id for row in first}
    assert {row.confidence for row in second} == {row.confidence for row in first}


def test_one_source_material_linked_to_two_transcripts_gets_the_union_at_highest_confidence(
    session_factory, course_id
):
    """Review fix: a source material can be linked from SEVERAL
    media_sources rows sharing the same material_id -- e.g. an HTML
    "Recordings" page material where detect.py's page-scan created one row
    per linked video, all pointing material_id at that one page. Each
    linked transcript's assignments must be UNIONED onto the source in one
    pass, not applied one transcript at a time (which would have each
    later transcript's delete-and-rewrite wipe out the previous one's
    inherited rows -- and with no deterministic ordering, "which transcript
    wins" wasn't even reproducible)."""
    topic_ids = _write_taxonomy(session_factory, course_id, 1, TAXONOMY_V1)
    source_id = _add_material(
        session_factory, course_id, title="Recordings Page", kind="link", status="fetched",
    )
    transcript_a_id = _add_material(
        session_factory, course_id, title="Video 1 (transcript)", kind="transcript",
    )
    transcript_b_id = _add_material(
        session_factory, course_id, title="Video 2 (transcript)", kind="transcript",
    )
    _add_media_source(
        session_factory, course_id, material_id=source_id, transcript_material_id=transcript_a_id,
        url="https://zoom.us/rec/share/video1",
    )
    _add_media_source(
        session_factory, course_id, material_id=source_id, transcript_material_id=transcript_b_id,
        url="https://zoom.us/rec/share/video2",
    )

    def _result(user):
        # Transcript A: a topic it shares with B (at a LOWER confidence --
        # B's must win) plus one it alone carries.
        if "Video 1 (transcript)" in user:
            return ClassificationOut(
                assignments=[
                    TopicAssignment(topic_slug="arrays-and-lists", confidence=0.6, rationale="video 1: arrays"),
                    TopicAssignment(topic_slug="graph-algorithms", confidence=0.7, rationale="video 1: graphs"),
                ]
            )
        # Transcript B: the shared topic at a HIGHER confidence, plus one
        # it alone carries.
        if "Video 2 (transcript)" in user:
            return ClassificationOut(
                assignments=[
                    TopicAssignment(topic_slug="arrays-and-lists", confidence=0.9, rationale="video 2: arrays"),
                    TopicAssignment(topic_slug="sorting-algorithms", confidence=0.5, rationale="video 2: sorting"),
                ]
            )
        raise AssertionError(f"unexpected prompt: {user[:200]}")

    stub = _StubBackend(_result)

    _run(session_factory, stub, course_id)

    source_rows = _rows(session_factory, material_id=source_id)
    by_topic = {row.topic_id: row for row in source_rows}
    assert set(by_topic) == {
        topic_ids["arrays-and-lists"], topic_ids["graph-algorithms"], topic_ids["sorting-algorithms"],
    }  # the union of both transcripts' topics
    assert all(row.method == "inherited" for row in source_rows)
    # The overlapping topic keeps the HIGHER of the two transcripts' confidences.
    assert by_topic[topic_ids["arrays-and-lists"]].confidence == pytest.approx(0.9)
    assert by_topic[topic_ids["graph-algorithms"]].confidence == pytest.approx(0.7)
    assert by_topic[topic_ids["sorting-algorithms"]].confidence == pytest.approx(0.5)

    # Deterministic across re-runs: same union, same winning confidence,
    # still no duplicates.
    _run(session_factory, stub, course_id)
    rerun_rows = _rows(session_factory, material_id=source_id)
    assert len(rerun_rows) == 3
    rerun_by_topic = {row.topic_id: row.confidence for row in rerun_rows}
    assert rerun_by_topic == {topic_id: row.confidence for topic_id, row in by_topic.items()}


def test_media_source_with_null_material_id_or_null_transcript_material_id_is_a_noop(
    session_factory, backend, course_id
):
    _write_taxonomy(session_factory, course_id, 1, TAXONOMY_V1)
    transcript_id = _add_material(
        session_factory, course_id, title="Some Transcript", kind="transcript",
    )
    source_id = _add_material(
        session_factory, course_id, title="Some Recording", kind="link", status="fetched",
    )

    # A manually-added media source (M2.6a): no backing materials row yet.
    _add_media_source(
        session_factory, course_id, material_id=None, transcript_material_id=transcript_id,
        url="https://zoom.us/rec/share/no-material-yet",
    )
    # A media source whose recording hasn't been transcribed yet.
    _add_media_source(
        session_factory, course_id, material_id=source_id, transcript_material_id=None,
        url="https://zoom.us/rec/share/not-transcribed-yet",
    )

    stats = _run(session_factory, backend, course_id)  # must not raise

    assert stats.failed == 0
    assert _rows(session_factory, material_id=source_id) == []
    # The transcript material itself still gets classified normally --
    # inheritance just has nothing to attach it to.
    assert len(_rows(session_factory, material_id=transcript_id)) == 2
