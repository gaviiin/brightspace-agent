"""Tests for the S2 taxonomy stage: prompt assembly from syllabus + module
tree + summaries, post-validation of the model's proposal, versioned writes,
and llm_cache reuse -- all against MockBackend or small stub backends, so no
network access or API key is needed.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import select

from brightspace_agent.agents.llm import MockBackend
from brightspace_agent.agents.schemas import TaxonomyOut, TopicDef, TopicEdgeDef
from brightspace_agent.db.models import Course, LlmCache, Material, Module, Topic, TopicEdge
from brightspace_agent.db.session import init_db
from brightspace_agent.ingest.store import BlobStore
from brightspace_agent.pipeline.stages.taxonomy import (
    PROMPT_VERSION,
    TaxonomyStageError,
    run_taxonomy_stage,
)


@pytest.fixture
def session_factory(tmp_path):
    return init_db(tmp_path / "brightspace.db")[1]


@pytest.fixture
def blob_store(tmp_path):
    return BlobStore(blobs_dir=tmp_path / "blobs", text_dir=tmp_path / "text")


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
    """Wraps a backend, counting `structured_call` invocations and recording
    the user prompts it saw. A cache hit must not increment `calls`."""

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
    """Returns a caller-supplied schema instance, so a test can drive the
    stage's post-validation with exactly the (mis)shaped model output it
    wants to exercise."""

    def __init__(self, result) -> None:
        self._result = result
        self.calls = 0
        self.prompts: list[str] = []

    def structured_call(self, schema, *, system, user, tier):
        self.calls += 1
        self.prompts.append(user)
        usage = {
            "model": self.model_for_tier(tier),
            "input_tokens": 0,
            "output_tokens": 0,
            "est_cost_usd": 0.0,
        }
        return self._result, usage

    def model_for_tier(self, tier):
        return f"stub-{tier}"


# --------------------------------------------------------------------------
# Seeding helpers
# --------------------------------------------------------------------------


def _add_module(session_factory, course_id, *, d2l_module_id, title, sort_order=0, parent_id=None) -> int:
    with session_factory() as session:
        module = Module(
            course_id=course_id,
            d2l_module_id=d2l_module_id,
            title=title,
            sort_order=sort_order,
            parent_id=parent_id,
        )
        session.add(module)
        session.commit()
        return module.id


def _add_summarized_material(
    session_factory,
    blob_store,
    course_id,
    *,
    title,
    kind="document",
    body="Body text.",
    key_terms=(),
    module_id=None,
    summary=None,
) -> int:
    sha256, size = blob_store.put_bytes(body.encode("utf-8"))
    blob_store.write_text(sha256, body)
    with session_factory() as session:
        material = Material(
            course_id=course_id,
            module_id=module_id,
            kind=kind,
            title=title,
            mime="text/plain",
            sha256=sha256,
            size_bytes=size,
            summary=summary or f"{title}: first summary line.\nSecond summary line.\nThird line.",
            summary_meta_json=json.dumps(
                {
                    "model": "mock-fast",
                    "prompt_version": "s1.v1",
                    "key_terms": list(key_terms),
                    "doc_kind_guess": kind,
                    "usage": {},
                }
            ),
            status="summarized",
        )
        session.add(material)
        session.commit()
        return material.id


def _seed_course_content(session_factory, blob_store, course_id, *, with_syllabus=True) -> None:
    """Four top-level modules (one with a child) plus a summarized material
    per module -- the shape the mock taxonomy builder derives slugs from."""
    arrays = _add_module(session_factory, course_id, d2l_module_id=10, title="Arrays and Lists", sort_order=0)
    sorting = _add_module(session_factory, course_id, d2l_module_id=20, title="Sorting Algorithms", sort_order=1)
    _add_module(
        session_factory, course_id,
        d2l_module_id=21, title="Quicksort Deep Dive", sort_order=0, parent_id=sorting,
    )
    graphs = _add_module(session_factory, course_id, d2l_module_id=30, title="Graph Algorithms", sort_order=2)
    dp = _add_module(session_factory, course_id, d2l_module_id=40, title="Dynamic Programming", sort_order=3)

    if with_syllabus:
        _add_summarized_material(
            session_factory, blob_store, course_id,
            title="CS 2110 Syllabus",
            kind="syllabus",
            body="CS 2110 course outline. Week 1 arrays. Week 5 quicksort. Grading: 40% assignments.",
            key_terms=["grading", "office hours"],
            # Line 3 is the fallback marker: the compact summary lines in the
            # materials section only carry the first two lines, so seeing it
            # in the prompt means the *syllabus* section used this summary.
            summary=(
                "Syllabus summary line one.\n"
                "Syllabus summary line two.\n"
                "SYLLABUS-FALLBACK-MARKER: assessment breakdown and policies."
            ),
        )

    _add_summarized_material(
        session_factory, blob_store, course_id,
        title="Lecture 1: Arrays", kind="slides", module_id=arrays,
        body="Arrays, dynamic arrays, amortized append cost.",
        key_terms=["dynamic array", "amortized analysis"],
    )
    _add_summarized_material(
        session_factory, blob_store, course_id,
        title="Lecture 5: Quicksort", kind="slides", module_id=sorting,
        body="Quicksort, pivot selection, partitioning, average case analysis.",
        key_terms=["quicksort", "pivot", "partition"],
    )
    _add_summarized_material(
        session_factory, blob_store, course_id,
        title="Assignment 3: BFS", kind="assignment", module_id=graphs,
        body="Implement breadth-first search over an adjacency list.",
        key_terms=["bfs", "adjacency list"],
    )
    _add_summarized_material(
        session_factory, blob_store, course_id,
        title="Lecture 9: Memoization", kind="slides", module_id=dp,
        body="Top-down memoization versus bottom-up tabulation.",
        key_terms=["memoization", "tabulation"],
    )


def _run(session_factory, backend, course_id, blob_store=None, *, force=False):
    return asyncio.run(
        run_taxonomy_stage(session_factory, backend, course_id, blob_store=blob_store, force=force)
    )


def _topics(session_factory, course_id, version) -> list[Topic]:
    with session_factory() as session:
        rows = list(
            session.execute(
                select(Topic)
                .where(Topic.course_id == course_id, Topic.taxonomy_version == version)
                .order_by(Topic.order_index)
            ).scalars().all()
        )
        for row in rows:
            session.expunge(row)
        return rows


def _taxonomy_version(session_factory, course_id) -> int:
    with session_factory() as session:
        return session.get(Course, course_id).taxonomy_version


# --------------------------------------------------------------------------
# (1) Happy path
# --------------------------------------------------------------------------


def test_happy_path_writes_topics_and_edges_at_version_one(session_factory, blob_store, backend, course_id):
    _seed_course_content(session_factory, blob_store, course_id)

    stats = _run(session_factory, backend, course_id, blob_store)

    assert _taxonomy_version(session_factory, course_id) == 1

    topics = _topics(session_factory, course_id, 1)
    assert len(topics) == 4
    # The mock derives slugs from the module outline, depth-first in sort
    # order -- so the child module lands between its parent and the next
    # top-level module.
    assert [topic.slug for topic in topics] == [
        "arrays-and-lists",
        "sorting-algorithms",
        "quicksort-deep-dive",
        "graph-algorithms",
    ]
    assert [topic.order_index for topic in topics] == [0, 1, 2, 3]
    assert all(topic.created_by == "agent" for topic in topics) is True
    assert all(topic.name for topic in topics)
    assert all(topic.description for topic in topics)

    # The mock proposes one valid prerequisite edge and one edge pointing at
    # a slug that isn't in the taxonomy; only the valid one survives.
    with session_factory() as session:
        edges = list(session.execute(select(TopicEdge).where(TopicEdge.course_id == course_id)).scalars().all())
        topic_by_id = {topic.id: topic for topic in session.execute(select(Topic)).scalars().all()}
        assert len(edges) == 1
        edge = edges[0]
        assert edge.relation == "prerequisite"
        assert edge.created_by == "agent"
        assert topic_by_id[edge.from_topic_id].slug == "arrays-and-lists"
        assert topic_by_id[edge.to_topic_id].slug == "sorting-algorithms"

    assert stats.topics == 4
    assert stats.edges == 1
    assert stats.taxonomy_version == 1
    assert stats.cached_hits == 0


def test_prompt_carries_syllabus_module_outline_and_summaries(session_factory, blob_store, backend, course_id):
    _seed_course_content(session_factory, blob_store, course_id)
    counting = _CountingBackend(backend)

    _run(session_factory, counting, course_id, blob_store)

    assert counting.calls == 1
    prompt = counting.prompts[0]
    # syllabus: the sidecar *text*, not merely its summary
    assert "Week 5 quicksort" in prompt
    assert "SYLLABUS-FALLBACK-MARKER" not in prompt
    # module outline, with child modules indented under their parent
    assert "- Sorting Algorithms" in prompt
    assert "  - Quicksort Deep Dive" in prompt
    # per-material summary lines with kind, title and key terms
    assert '[slides] "Lecture 5: Quicksort"' in prompt
    assert "quicksort, pivot, partition" in prompt
    assert "Data Structures and Algorithms" in prompt  # course name


def test_prompt_omits_metadata_only_summaries(session_factory, blob_store, backend, course_id):
    """M3.5a: a material S1 summarized from a metadata pseudo-document
    (`sha256 IS NULL`) is left out of the prompt. Its summary restates the
    title the outline already carries, and letting it in re-digests the
    taxonomy -- a new version, and a full-course re-classify, for zero new
    signal. See `_material_summary_lines`."""
    _seed_course_content(session_factory, blob_store, course_id)
    with session_factory() as session:
        session.add(
            Material(
                course_id=course_id, kind="link", title="PSEUDO-DOC-LINK",
                source_url="https://example.edu/reading",
                summary="A link titled PSEUDO-DOC-LINK.\nNothing else is known about it.",
                summary_meta_json=json.dumps({"key_terms": ["pseudo", "doc"]}),
                status="summarized",  # summarized, but with no sha256: pass 3's output
            )
        )
        session.commit()
    counting = _CountingBackend(backend)

    _run(session_factory, counting, course_id, blob_store)

    prompt = counting.prompts[0]
    assert "PSEUDO-DOC-LINK" not in prompt
    assert '[slides] "Lecture 5: Quicksort"' in prompt  # real materials still listed


# --------------------------------------------------------------------------
# (2) Slug normalization + dedupe
# --------------------------------------------------------------------------


def test_slugs_normalized_and_duplicates_dropped(session_factory, blob_store, course_id):
    _seed_course_content(session_factory, blob_store, course_id)
    stub = _StubBackend(
        TaxonomyOut(
            topics=[
                TopicDef(slug="Dup Slug", name="Dup A", description="First occurrence wins."),
                TopicDef(slug="dup-slug", name="Dup B", description="Second occurrence is dropped."),
                TopicDef(slug="  Graph  Algorithms!  ", name="Graphs", description="Punctuation stripped."),
                TopicDef(slug="dynamic-programming", name="DP", description="Already clean."),
            ],
            edges=[],
        )
    )

    stats = _run(session_factory, stub, course_id, blob_store)

    topics = _topics(session_factory, course_id, 1)
    assert [topic.slug for topic in topics] == ["dup-slug", "graph-algorithms", "dynamic-programming"]
    assert topics[0].name == "Dup A"  # the first occurrence survived, not the second
    assert stats.topics == 3


def test_absurdly_long_slug_is_capped_and_its_edges_still_resolve(session_factory, blob_store, course_id):
    _seed_course_content(session_factory, blob_store, course_id)
    long_slug = "-".join(["extremely-verbose-topic-name"] * 6)  # ~170 chars
    stub = _StubBackend(
        TaxonomyOut(
            topics=[
                TopicDef(slug=long_slug, name="Verbose", description="a"),
                TopicDef(slug="beta", name="Beta", description="b"),
                TopicDef(slug="gamma", name="Gamma", description="c"),
            ],
            edges=[TopicEdgeDef(from_slug=long_slug, to_slug="beta", relation="prerequisite")],
        )
    )

    stats = _run(session_factory, stub, course_id, blob_store)

    topics = _topics(session_factory, course_id, 1)
    assert len(topics[0].slug) <= 80
    assert long_slug.startswith(topics[0].slug)
    assert stats.edges == 1  # the edge was normalized the same way, so it resolved


def test_self_loop_and_duplicate_edges_dropped(session_factory, blob_store, course_id):
    _seed_course_content(session_factory, blob_store, course_id)
    stub = _StubBackend(
        TaxonomyOut(
            topics=[
                TopicDef(slug="alpha", name="Alpha", description="a"),
                TopicDef(slug="beta", name="Beta", description="b"),
                TopicDef(slug="gamma", name="Gamma", description="c"),
            ],
            edges=[
                TopicEdgeDef(from_slug="alpha", to_slug="alpha", relation="prerequisite"),  # self-loop
                TopicEdgeDef(from_slug="Alpha", to_slug="beta", relation="prerequisite"),  # normalized
                TopicEdgeDef(from_slug="alpha", to_slug="beta", relation="prerequisite"),  # duplicate
                TopicEdgeDef(from_slug="beta", to_slug="nowhere", relation="related"),  # unknown slug
                TopicEdgeDef(from_slug="beta", to_slug="gamma", relation="related"),
            ],
        )
    )

    stats = _run(session_factory, stub, course_id, blob_store)

    with session_factory() as session:
        edges = list(session.execute(select(TopicEdge)).scalars().all())
        slug_by_id = {topic.id: topic.slug for topic in session.execute(select(Topic)).scalars().all()}
    assert sorted((slug_by_id[e.from_topic_id], slug_by_id[e.to_topic_id], e.relation) for e in edges) == [
        ("alpha", "beta", "prerequisite"),
        ("beta", "gamma", "related"),
    ]
    assert stats.edges == 2


# --------------------------------------------------------------------------
# (3) Too few topics -> stage fails, nothing written
# --------------------------------------------------------------------------


def test_fewer_than_three_topics_fails_stage_without_writing(session_factory, blob_store, course_id):
    _seed_course_content(session_factory, blob_store, course_id)
    stub = _StubBackend(
        TaxonomyOut(
            topics=[
                TopicDef(slug="only-one", name="Only One", description="x"),
                TopicDef(slug="only-two", name="Only Two", description="y"),
            ],
            edges=[],
        )
    )

    with pytest.raises(TaxonomyStageError):
        _run(session_factory, stub, course_id, blob_store)

    assert _taxonomy_version(session_factory, course_id) == 0
    with session_factory() as session:
        assert session.execute(select(Topic)).scalars().all() == []
        assert session.execute(select(TopicEdge)).scalars().all() == []
        # A junk taxonomy must not be cached either -- a retry should get a
        # fresh model call, not a replay of the bad answer.
        assert session.execute(select(LlmCache)).scalars().all() == []


def test_more_than_thirty_topics_truncated_to_thirty(session_factory, blob_store, course_id):
    _seed_course_content(session_factory, blob_store, course_id)
    stub = _StubBackend(
        TaxonomyOut(
            topics=[
                TopicDef(slug=f"topic-{i:02d}", name=f"Topic {i}", description="d") for i in range(35)
            ],
            edges=[TopicEdgeDef(from_slug="topic-00", to_slug="topic-34", relation="related")],
        )
    )

    stats = _run(session_factory, stub, course_id, blob_store)

    topics = _topics(session_factory, course_id, 1)
    assert len(topics) == 30
    assert topics[-1].slug == "topic-29"
    assert stats.topics == 30
    # topic-34 was truncated away, so the edge referencing it is now unknown.
    with session_factory() as session:
        assert session.execute(select(TopicEdge)).scalars().all() == []


# --------------------------------------------------------------------------
# (4) Cache reuse -- no LLM call, but a fresh version each run
# --------------------------------------------------------------------------


def test_rerun_with_unchanged_inputs_is_a_no_op(session_factory, blob_store, backend, course_id):
    """An unchanged course must cost nothing and change nothing.

    A new version per run would be invisible in the UI but expensive
    everywhere else: S3's worklist is "materials with no row at the current
    version", so every bump re-classifies the entire course.
    """
    _seed_course_content(session_factory, blob_store, course_id)
    counting = _CountingBackend(backend)

    _run(session_factory, counting, course_id, blob_store)
    assert counting.calls == 1

    with session_factory() as session:
        cache_rows = list(session.execute(select(LlmCache).where(LlmCache.stage == "taxonomy")).scalars().all())
    assert len(cache_rows) == 1
    assert cache_rows[0].prompt_version == PROMPT_VERSION
    assert cache_rows[0].model == counting.model_for_tier("smart")

    second_stats = _run(session_factory, counting, course_id, blob_store)

    assert counting.calls == 1  # cache hit: no new LLM call
    assert second_stats.cached_hits == 1
    assert second_stats.unchanged is True
    assert second_stats.topics == 0  # counters report rows written
    assert second_stats.taxonomy_version == 1
    assert _taxonomy_version(session_factory, course_id) == 1  # not bumped

    assert len(_topics(session_factory, course_id, 1)) == 4  # v1 intact
    assert _topics(session_factory, course_id, 2) == []  # and no v2 was minted

    with session_factory() as session:
        cache_rows_after = list(
            session.execute(select(LlmCache).where(LlmCache.stage == "taxonomy")).scalars().all()
        )
        assert len(cache_rows_after) == 1  # the hit did not insert a duplicate
        edges = list(session.execute(select(TopicEdge)).scalars().all())
    assert len(edges) == 1  # no duplicate edge set either


def test_changed_taxonomy_content_still_bumps_the_version(session_factory, blob_store, course_id):
    _seed_course_content(session_factory, blob_store, course_id)
    first = _StubBackend(
        TaxonomyOut(
            topics=[
                TopicDef(slug="alpha", name="Alpha", description="a"),
                TopicDef(slug="beta", name="Beta", description="b"),
                TopicDef(slug="gamma", name="Gamma", description="c"),
            ],
            edges=[],
        )
    )
    _run(session_factory, first, course_id, blob_store)
    assert _taxonomy_version(session_factory, course_id) == 1

    # New material -> different prompt -> cache miss -> a genuinely different
    # proposal, which must land as a new version.
    _add_summarized_material(
        session_factory, blob_store, course_id,
        title="Lecture 12: Hash Tables", body="Hashing and load factors.", key_terms=["hashing"],
    )
    second = _StubBackend(
        TaxonomyOut(
            topics=[
                TopicDef(slug="alpha", name="Alpha", description="a"),
                TopicDef(slug="beta", name="Beta", description="b"),
                TopicDef(slug="hash-tables", name="Hash Tables", description="new unit"),
            ],
            edges=[],
        )
    )

    stats = _run(session_factory, second, course_id, blob_store)

    assert stats.unchanged is False
    assert stats.topics == 3
    assert stats.taxonomy_version == 2
    assert _taxonomy_version(session_factory, course_id) == 2
    assert [topic.slug for topic in _topics(session_factory, course_id, 1)] == ["alpha", "beta", "gamma"]
    assert [topic.slug for topic in _topics(session_factory, course_id, 2)] == [
        "alpha", "beta", "hash-tables",
    ]


def test_edge_only_change_still_bumps_the_version_and_writes_new_edges(session_factory, blob_store, course_id):
    """Regression for the S2 edge-digest gap: the unchanged-comparison used
    to hash topics only, so an edge-only change (same topics, a different
    prerequisite/related link) digested identically to the taxonomy already
    on disk. The guarded write treats "unchanged" as "write nothing", so the
    new edges were silently discarded -- and could never be repaired by
    re-running S2, since every subsequent run saw the same "unchanged"
    topics and skipped the write again."""
    _seed_course_content(session_factory, blob_store, course_id)
    same_topics = [
        TopicDef(slug="alpha", name="Alpha", description="a"),
        TopicDef(slug="beta", name="Beta", description="b"),
        TopicDef(slug="gamma", name="Gamma", description="c"),
    ]
    first = _StubBackend(
        TaxonomyOut(
            topics=same_topics,
            edges=[TopicEdgeDef(from_slug="alpha", to_slug="beta", relation="prerequisite")],
        )
    )
    _run(session_factory, first, course_id, blob_store)
    assert _taxonomy_version(session_factory, course_id) == 1

    # Force a cache miss (identical inputs would otherwise just replay the
    # cached first proposal, edges and all) without changing the topics the
    # stub returns -- only the edges differ.
    _add_summarized_material(
        session_factory, blob_store, course_id,
        title="Lecture 12: Hash Tables", body="Hashing and load factors.", key_terms=["hashing"],
    )
    second = _StubBackend(
        TaxonomyOut(
            topics=same_topics,
            edges=[TopicEdgeDef(from_slug="beta", to_slug="gamma", relation="related")],
        )
    )
    stats = _run(session_factory, second, course_id, blob_store)

    assert stats.unchanged is False
    assert stats.taxonomy_version == 2
    assert _taxonomy_version(session_factory, course_id) == 2
    assert [topic.slug for topic in _topics(session_factory, course_id, 2)] == ["alpha", "beta", "gamma"]

    with session_factory() as session:
        edges_v2 = list(session.execute(select(TopicEdge)).scalars().all())
        topic_by_id = {topic.id: topic for topic in session.execute(select(Topic)).scalars().all()}
    v2_topic_ids = {topic.id for topic in _topics(session_factory, course_id, 2)}
    edges_v2 = [e for e in edges_v2 if e.from_topic_id in v2_topic_ids and e.to_topic_id in v2_topic_ids]
    assert len(edges_v2) == 1
    assert topic_by_id[edges_v2[0].from_topic_id].slug == "beta"
    assert topic_by_id[edges_v2[0].to_topic_id].slug == "gamma"


def test_edge_only_change_at_a_new_version_is_still_a_no_op_when_edges_match(
    session_factory, blob_store, course_id
):
    """Sanity check on the other side of the same fix: identical topics AND
    identical edges must still be a true no-op (no spurious version bump)."""
    _seed_course_content(session_factory, blob_store, course_id)
    same_topics = [
        TopicDef(slug="alpha", name="Alpha", description="a"),
        TopicDef(slug="beta", name="Beta", description="b"),
        TopicDef(slug="gamma", name="Gamma", description="c"),
    ]
    same_edges = [TopicEdgeDef(from_slug="alpha", to_slug="beta", relation="prerequisite")]
    first = _StubBackend(TaxonomyOut(topics=same_topics, edges=same_edges))
    _run(session_factory, first, course_id, blob_store)
    assert _taxonomy_version(session_factory, course_id) == 1

    second = _StubBackend(TaxonomyOut(topics=same_topics, edges=same_edges))
    stats = _run(session_factory, second, course_id, blob_store)

    assert stats.unchanged is True
    assert _taxonomy_version(session_factory, course_id) == 1


def test_two_courses_with_identical_module_titles_do_not_share_a_taxonomy(
    session_factory, blob_store, backend
):
    """Regression: the cache key used to be a digest of module titles and
    material shas, so an "Organic Chemistry" course whose modules happened to
    be named like a "Data Structures" course silently inherited its taxonomy,
    permanently and without a warning."""
    shared_modules = ["Unit One", "Unit Two", "Unit Three", "Unit Four"]
    course_ids = []
    for org_unit_id, name, code in [(11, "Data Structures", "CS2110"), (22, "Organic Chemistry", "CHEM241")]:
        with session_factory() as session:
            course = Course(d2l_org_unit_id=org_unit_id, tenant_origin="school.d2l.com", name=name, code=code)
            session.add(course)
            session.commit()
            course_ids.append(course.id)
        for index, title in enumerate(shared_modules):
            _add_module(
                session_factory, course_ids[-1],
                d2l_module_id=1000 * org_unit_id + index, title=title, sort_order=index,
            )

    counting = _CountingBackend(backend)
    for course in course_ids:
        _run(session_factory, counting, course, blob_store)

    assert counting.calls == 2  # each course asked for its own taxonomy
    with session_factory() as session:
        cache_rows = list(session.execute(select(LlmCache).where(LlmCache.stage == "taxonomy")).scalars().all())
    assert len(cache_rows) == 2
    assert "Organic Chemistry" in counting.prompts[1]  # the prompts genuinely differ
    for course in course_ids:
        assert _taxonomy_version(session_factory, course) == 1
        assert len(_topics(session_factory, course, 1)) == 4


def test_syllabus_source_change_is_a_cache_miss(session_factory, blob_store, backend, course_id):
    """The summary fallback and the full sidecar are materially different
    prompts; they must not share a cached answer."""
    _seed_course_content(session_factory, blob_store, course_id)
    counting = _CountingBackend(backend)

    _run(session_factory, counting, course_id)  # no blob_store: summary fallback
    _run(session_factory, counting, course_id, blob_store)  # sidecar text

    assert counting.calls == 2
    assert "SYLLABUS-FALLBACK-MARKER" in counting.prompts[0]
    assert "Week 5 quicksort" in counting.prompts[1]


def test_unparseable_cache_row_is_replaced_rather_than_wedging_the_stage(
    session_factory, blob_store, backend, course_id
):
    _seed_course_content(session_factory, blob_store, course_id)
    counting = _CountingBackend(backend)
    _run(session_factory, counting, course_id, blob_store)

    with session_factory() as session:
        row = session.execute(select(LlmCache).where(LlmCache.stage == "taxonomy")).scalar_one()
        row.output_json = "{not json at all"
        session.commit()

    stats = _run(session_factory, counting, course_id, blob_store)

    assert counting.calls == 2  # treated as a miss, not an unrecoverable error
    assert stats.cached_hits == 0
    assert stats.unchanged is True  # same proposal as v1, so still no new version
    with session_factory() as session:
        rows = list(session.execute(select(LlmCache).where(LlmCache.stage == "taxonomy")).scalars().all())
    assert len(rows) == 1  # the poisoned row was overwritten, not duplicated
    assert json.loads(rows[0].output_json)["topics"]


def test_changed_inputs_miss_the_cache(session_factory, blob_store, backend, course_id):
    _seed_course_content(session_factory, blob_store, course_id)
    counting = _CountingBackend(backend)

    _run(session_factory, counting, course_id, blob_store)
    assert counting.calls == 1

    _add_summarized_material(
        session_factory, blob_store, course_id,
        title="Lecture 12: Hash Tables", kind="slides",
        body="Hashing, collision resolution, load factor.",
        key_terms=["hashing", "load factor"],
    )

    _run(session_factory, counting, course_id, blob_store)

    assert counting.calls == 2  # new material -> new key -> real call


# --------------------------------------------------------------------------
# (5) A user-edited taxonomy outranks the agent's
# --------------------------------------------------------------------------


def _mark_one_topic_user_authored(session_factory, course_id, version) -> None:
    """Simulate what pipeline/taxonomy_apply.py leaves behind after a
    student's structural edit: at least one topic at the current version
    with created_by='user'."""
    with session_factory() as session:
        topic = session.execute(
            select(Topic)
            .where(Topic.course_id == course_id, Topic.taxonomy_version == version)
            .order_by(Topic.order_index)
        ).scalars().first()
        topic.name = "Arrays, Lists and Friends"
        topic.created_by = "user"
        session.commit()


def test_full_run_leaves_a_user_edited_taxonomy_alone(session_factory, blob_store, backend, course_id):
    """Regression: a full pipeline run used to silently revert taxonomy edits.

    After a student's structural edit minted v2 with created_by='user'
    topics, the next full run re-proposed from the same summaries, digested
    differently from the edited map, and wrote the AGENT's taxonomy at v3 --
    the student's edit gone, plus a re-classification bill for the whole
    course. The stage now declines before it even asks the model.
    """
    _seed_course_content(session_factory, blob_store, course_id)
    _run(session_factory, backend, course_id, blob_store)
    assert _taxonomy_version(session_factory, course_id) == 1
    _mark_one_topic_user_authored(session_factory, course_id, 1)
    edited_slugs = [topic.slug for topic in _topics(session_factory, course_id, 1)]

    # A stub that would happily propose something completely different --
    # if it were ever asked.
    stub = _StubBackend(
        TaxonomyOut(
            topics=[
                TopicDef(slug="agent-one", name="Agent One", description="a"),
                TopicDef(slug="agent-two", name="Agent Two", description="b"),
                TopicDef(slug="agent-three", name="Agent Three", description="c"),
            ],
            edges=[],
        )
    )

    stats = _run(session_factory, stub, course_id, blob_store)

    assert stub.calls == 0  # not asked at all: declining is free
    assert stats.skipped_user_taxonomy is True
    assert stats.unchanged is True
    assert stats.taxonomy_version == 1
    assert stats.topics == 0
    assert _taxonomy_version(session_factory, course_id) == 1
    assert [topic.slug for topic in _topics(session_factory, course_id, 1)] == edited_slugs
    assert _topics(session_factory, course_id, 2) == []  # no v3-style revert


def test_force_re_proposes_over_a_user_edited_taxonomy(session_factory, blob_store, backend, course_id):
    """The escape hatch: an explicit force run does write the agent's
    taxonomy at a new version, user edits and all."""
    _seed_course_content(session_factory, blob_store, course_id)
    _run(session_factory, backend, course_id, blob_store)
    _mark_one_topic_user_authored(session_factory, course_id, 1)

    stub = _StubBackend(
        TaxonomyOut(
            topics=[
                TopicDef(slug="agent-one", name="Agent One", description="a"),
                TopicDef(slug="agent-two", name="Agent Two", description="b"),
                TopicDef(slug="agent-three", name="Agent Three", description="c"),
            ],
            edges=[],
        )
    )

    stats = _run(session_factory, stub, course_id, blob_store, force=True)

    assert stub.calls == 1
    assert stats.skipped_user_taxonomy is False
    assert stats.unchanged is False
    assert stats.taxonomy_version == 2
    assert _taxonomy_version(session_factory, course_id) == 2
    v2 = _topics(session_factory, course_id, 2)
    assert [topic.slug for topic in v2] == ["agent-one", "agent-two", "agent-three"]
    assert all(topic.created_by == "agent" for topic in v2)
    # The edited version is history, not deleted.
    assert len(_topics(session_factory, course_id, 1)) == 4


def test_agent_authored_taxonomy_is_not_treated_as_user_edited(
    session_factory, blob_store, backend, course_id
):
    """Sanity check on the other side of the guard: an ordinary
    agent-authored taxonomy must still be re-proposable without `force`."""
    _seed_course_content(session_factory, blob_store, course_id)
    _run(session_factory, backend, course_id, blob_store)
    counting = _CountingBackend(backend)

    stats = _run(session_factory, counting, course_id, blob_store)

    assert stats.skipped_user_taxonomy is False
    assert stats.cached_hits == 1  # it really did go through the normal path
    assert stats.unchanged is True


# --------------------------------------------------------------------------
# (6) Degraded inputs
# --------------------------------------------------------------------------


def test_runs_without_a_syllabus(session_factory, blob_store, backend, course_id):
    _seed_course_content(session_factory, blob_store, course_id, with_syllabus=False)
    counting = _CountingBackend(backend)

    stats = _run(session_factory, counting, course_id, blob_store)

    assert stats.topics == 4
    assert _taxonomy_version(session_factory, course_id) == 1
    assert "no syllabus" in counting.prompts[0].lower()


def test_runs_without_a_blob_store_falling_back_to_the_syllabus_summary(
    session_factory, blob_store, backend, course_id
):
    _seed_course_content(session_factory, blob_store, course_id)
    counting = _CountingBackend(backend)

    stats = _run(session_factory, counting, course_id)  # no blob_store passed

    assert stats.topics == 4
    prompt = counting.prompts[0]
    assert "Week 5 quicksort" not in prompt  # sidecar unavailable
    assert "SYLLABUS-FALLBACK-MARKER" in prompt  # but the syllabus summary still is
