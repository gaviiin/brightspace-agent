"""Tests for the LangGraph pipeline orchestration (pipeline/graph.py):
node order on a full run, and the conditional routing that skips
classify+assemble on a taxonomy failure. Against MockBackend/stub backends
directly (no PipelineRunner, no HTTP) -- no network access, no API key.
"""

from __future__ import annotations

import asyncio
import re

import fitz  # PyMuPDF
import pytest
from sqlalchemy import select

from brightspace_agent.agents.llm import MockBackend
from brightspace_agent.agents.promptfmt import SECTION_MATERIAL_SUMMARIES, section_body, slugify
from brightspace_agent.agents.schemas import TaxonomyOut, TopicDef
from brightspace_agent.config import Settings
from brightspace_agent.db.models import Course, Material, MaterialTopic, Topic
from brightspace_agent.db.session import init_db
from brightspace_agent.ingest.store import BlobStore
from brightspace_agent.pipeline.graph import PipelineDeps, PipelineState, build_pipeline_graph


def _make_pdf_bytes(text: str) -> bytes:
    doc = fitz.open()
    try:
        page = doc.new_page()
        page.insert_text((72, 72), text)
        return doc.tobytes()
    finally:
        doc.close()


@pytest.fixture
def session_factory(tmp_path):
    return init_db(tmp_path / "brightspace.db")[1]


@pytest.fixture
def blob_store(tmp_path):
    return BlobStore(blobs_dir=tmp_path / "blobs", text_dir=tmp_path / "text")


@pytest.fixture
def course_id(session_factory):
    with session_factory() as session:
        course = Course(d2l_org_unit_id=1, tenant_origin="school.d2l.com", name="Intro to CS", code="CS100")
        session.add(course)
        session.commit()
        return course.id


def _add_fetched_pdf(session_factory, blob_store, course_id, *, title, text, kind="document") -> int:
    sha256, size = blob_store.put_bytes(_make_pdf_bytes(text))
    with session_factory() as session:
        material = Material(
            course_id=course_id, kind=kind, title=title, mime="application/pdf",
            sha256=sha256, size_bytes=size, status="fetched",
        )
        session.add(material)
        session.commit()
        return material.id


def _seed_summarizable_course(session_factory, blob_store, course_id) -> None:
    _add_fetched_pdf(
        session_factory, blob_store, course_id,
        title="Course Syllabus", text="CS100 syllabus. Week 1 intro. Week 2 loops.", kind="syllabus",
    )
    _add_fetched_pdf(
        session_factory, blob_store, course_id,
        title="Lecture 1", text="Introduction to programming and variables.",
    )
    _add_fetched_pdf(
        session_factory, blob_store, course_id,
        title="Lecture 2", text="Loops, conditionals, and control flow.",
    )


class _FailingTaxonomyBackend:
    """Delegates everything to `inner` except S2, which it always answers
    with a too-small proposal (fewer than the stage's minimum topic count),
    forcing `run_taxonomy_stage` to raise `TaxonomyStageError` -- regardless
    of what's seeded, unlike relying on MockBackend's taxonomy fallback
    (which always manufactures 4 topics even with no usable input)."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def structured_call(self, schema, *, system, user, tier):
        if schema is TaxonomyOut:
            parsed = TaxonomyOut(topics=[TopicDef(slug="only-one", name="Only One", description="x")], edges=[])
            usage = {
                "model": self.model_for_tier(tier), "input_tokens": 0, "output_tokens": 0, "est_cost_usd": 0.0,
            }
            return parsed, usage
        return self._inner.structured_call(schema, system=system, user=user, tier=tier)

    def model_for_tier(self, tier):
        return self._inner.model_for_tier(tier)


_SUMMARY_LINE_TITLE_RE = re.compile(r'^- \[[^\]]*\] "(?P<title>[^"]*)"')


class _SummaryDrivenTaxonomyBackend:
    """Delegates everything to `inner` except S2, whose proposal is built
    from the prompt's MATERIAL SUMMARIES section -- one topic per material
    line, slugged off that material's title.

    MockBackend's own taxonomy builder reads only the MODULE OUTLINE, which
    makes any change to the material list invisible in its output. This one
    makes "what went into the S2 prompt" observable in the taxonomy that
    comes back out, which is the whole hinge of the regression below: if a
    material's summary reaches the prompt, the taxonomy changes, the digest
    changes, and the course is re-versioned and re-billed.
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.taxonomy_calls = 0

    def structured_call(self, schema, *, system, user, tier):
        if schema is not TaxonomyOut:
            return self._inner.structured_call(schema, system=system, user=user, tier=tier)
        self.taxonomy_calls += 1
        topics: list[TopicDef] = []
        seen: set[str] = set()
        for line in section_body(user, SECTION_MATERIAL_SUMMARIES).splitlines():
            match = _SUMMARY_LINE_TITLE_RE.match(line.strip())
            if match is None:
                continue
            slug = slugify(match.group("title"))
            if not slug or slug in seen:
                continue
            seen.add(slug)
            topics.append(TopicDef(slug=slug, name=match.group("title"), description=f"Covers {slug}."))
        usage = {
            "model": self.model_for_tier(tier), "input_tokens": 0, "output_tokens": 0, "est_cost_usd": 0.0,
        }
        return TaxonomyOut(topics=topics, edges=[]), usage

    def model_for_tier(self, tier):
        return self._inner.model_for_tier(tier)


def _deps(session_factory, blob_store, backend) -> PipelineDeps:
    return PipelineDeps(session_factory=session_factory, blob_store=blob_store, backend=backend, settings=Settings())


def _run_graph(deps: PipelineDeps, course_id: int) -> PipelineState:
    graph = build_pipeline_graph(deps)
    initial_state: PipelineState = {"course_id": course_id, "stage_stats": {}, "error": None}
    return asyncio.run(graph.ainvoke(initial_state))


# --------------------------------------------------------------------------
# (1) Full run
# --------------------------------------------------------------------------


def test_full_run_executes_all_four_nodes_in_order(session_factory, blob_store, course_id):
    _seed_summarizable_course(session_factory, blob_store, course_id)
    deps = _deps(session_factory, blob_store, MockBackend())

    final_state = _run_graph(deps, course_id)

    assert final_state["error"] is None
    assert list(final_state["stage_stats"].keys()) == ["summarize", "taxonomy", "classify", "assemble"]

    with session_factory() as session:
        materials = list(session.execute(select(Material).where(Material.course_id == course_id)).scalars().all())
        assert all(m.status == "summarized" for m in materials)
        assert all(m.summary for m in materials)

        course = session.get(Course, course_id)
        assert course.taxonomy_version == 1
        topics = list(session.execute(select(Topic).where(Topic.course_id == course_id)).scalars().all())
        assert len(topics) >= 3

        material_topics = list(session.execute(select(MaterialTopic)).scalars().all())
        assert len(material_topics) > 0

    summarize_stats = final_state["stage_stats"]["summarize"]
    assert summarize_stats["summarized"] == 3
    taxonomy_stats = final_state["stage_stats"]["taxonomy"]
    assert taxonomy_stats["taxonomy_version"] == 1
    classify_stats = final_state["stage_stats"]["classify"]
    assert classify_stats["classified"] > 0
    assemble_stats = final_state["stage_stats"]["assemble"]
    assert assemble_stats["materials"] == 3


# --------------------------------------------------------------------------
# (1b) M3.5a regression: a metadata-only summary must not churn the taxonomy
# --------------------------------------------------------------------------


def _assignments(session_factory, material_ids) -> set[tuple[int, int, int]]:
    with session_factory() as session:
        return {
            (row.material_id, row.topic_id, row.taxonomy_version)
            for row in session.execute(select(MaterialTopic)).scalars().all()
            if row.material_id in material_ids
        }


def test_a_text_less_link_does_not_re_version_an_existing_taxonomy(
    session_factory, blob_store, course_id
):
    """The first run after upgrading to M3.5a finds every link summarized by
    S1's new metadata pass. Those summaries are title restatements with no
    taxonomy signal of their own -- but if they reach the S2 prompt they
    still change the proposal, which digests differently from the taxonomy
    the course is already on, which mints a new version, which re-classifies
    (and re-bills) every material in the course. So `_material_summary_lines`
    filters `sha256 IS NULL` materials out.

    The link itself must still be summarized and classified at the EXISTING
    version -- excluding it from the taxonomy prompt is not excluding it from
    the course.
    """
    _seed_summarizable_course(session_factory, blob_store, course_id)
    backend = _SummaryDrivenTaxonomyBackend(MockBackend())
    deps = _deps(session_factory, blob_store, backend)

    _run_graph(deps, course_id)

    with session_factory() as session:
        assert session.get(Course, course_id).taxonomy_version == 1
        original_ids = {
            material.id
            for material in session.execute(
                select(Material).where(Material.course_id == course_id)
            ).scalars().all()
        }
        topics_v1 = sorted(
            topic.slug
            for topic in session.execute(
                select(Topic).where(Topic.course_id == course_id, Topic.taxonomy_version == 1)
            ).scalars().all()
        )
    assert len(topics_v1) == 3  # one per seeded material -- the stub's whole point
    assignments_v1 = _assignments(session_factory, original_ids)
    assert assignments_v1
    taxonomy_calls_after_first_run = backend.taxonomy_calls

    # A newly-synced link: no bytes, no sha256, so only S1's metadata pass
    # can reach it.
    with session_factory() as session:
        link = Material(
            course_id=course_id, kind="link", title="Recommended Reading: Big-O Notation",
            source_url="https://en.wikipedia.org/wiki/Big_O_notation", status="fetched",
        )
        session.add(link)
        session.commit()
        link_id = link.id

    _run_graph(deps, course_id)

    with session_factory() as session:
        course = session.get(Course, course_id)
        assert course.taxonomy_version == 1  # NOT bumped by the link's pseudo-doc summary
        assert sorted(
            topic.slug
            for topic in session.execute(
                select(Topic).where(Topic.course_id == course_id)
            ).scalars().all()
        ) == topics_v1  # and no second version was written at all

        link_material = session.get(Material, link_id)
        assert link_material.status == "summarized"  # pass 3 still ran for it
        assert link_material.sha256 is None

    # The S2 prompt was byte-identical, so the cache answered it: the smart
    # model was never asked a second time.
    assert backend.taxonomy_calls == taxonomy_calls_after_first_run

    # Every pre-existing assignment survives, unchanged and un-re-billed...
    assert _assignments(session_factory, original_ids) == assignments_v1
    # ...and the link is classified at the SAME version, not left unsorted.
    link_assignments = _assignments(session_factory, {link_id})
    assert link_assignments
    assert all(version == 1 for _material_id, _topic_id, version in link_assignments)


# --------------------------------------------------------------------------
# (2) Taxonomy failure short-circuits classify + assemble
# --------------------------------------------------------------------------


def test_taxonomy_failure_skips_classify_and_assemble(session_factory, blob_store, course_id):
    _seed_summarizable_course(session_factory, blob_store, course_id)
    deps = _deps(session_factory, blob_store, _FailingTaxonomyBackend(MockBackend()))

    final_state = _run_graph(deps, course_id)

    assert final_state["error"] is not None
    assert "taxonomy" in final_state["error"]
    # The failed node returns {"error": ...} without a stage_stats entry --
    # there's nothing successful to report for it.
    assert set(final_state["stage_stats"].keys()) == {"summarize"}

    with session_factory() as session:
        course = session.get(Course, course_id)
        assert course.taxonomy_version == 0  # nothing was written
        assert session.execute(select(Topic)).scalars().all() == []
        assert session.execute(select(MaterialTopic)).scalars().all() == []

        # summarize still ran (it's upstream of the failure) and its work
        # was committed -- a graph-level failure must not roll that back.
        materials = list(session.execute(select(Material).where(Material.course_id == course_id)).scalars().all())
        assert all(m.status == "summarized" for m in materials)
