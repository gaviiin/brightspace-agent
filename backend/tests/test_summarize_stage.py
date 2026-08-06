"""Tests for the S1 summarize stage: extract pass, summarize pass (with
llm_cache reuse), and concurrent fan-out -- all against MockBackend, so no
network access or API key is needed.
"""

from __future__ import annotations

import asyncio
import json

import fitz  # PyMuPDF
import pytest
from sqlalchemy import select

from brightspace_agent.agents.llm import MockBackend
from brightspace_agent.db.models import Course, LlmCache, Material
from brightspace_agent.db.session import init_db
from brightspace_agent.ingest.store import BlobStore
from brightspace_agent.pipeline.stages.summarize import PROMPT_VERSION, run_summarize_stage


def _make_pdf_bytes(text: str) -> bytes:
    doc = fitz.open()
    try:
        page = doc.new_page()
        page.insert_text((72, 72), text)
        return doc.tobytes()
    finally:
        doc.close()


@pytest.fixture
def db(tmp_path):
    return init_db(tmp_path / "brightspace.db")


@pytest.fixture
def session_factory(db):
    return db[1]


@pytest.fixture
def blob_store(tmp_path):
    return BlobStore(blobs_dir=tmp_path / "blobs", text_dir=tmp_path / "text")


@pytest.fixture
def course_id(session_factory):
    with session_factory() as session:
        course = Course(d2l_org_unit_id=1, tenant_origin="school.d2l.com", name="Intro to CS")
        session.add(course)
        session.commit()
        return course.id


@pytest.fixture
def backend():
    return MockBackend()


def _add_material(session_factory, course_id, **kwargs) -> int:
    with session_factory() as session:
        material = Material(course_id=course_id, **kwargs)
        session.add(material)
        session.commit()
        return material.id


def _get_material(session_factory, material_id) -> Material:
    with session_factory() as session:
        material = session.get(Material, material_id)
        session.expunge(material)
        return material


def _run_stage(session_factory, blob_store, backend, course_id, **kwargs):
    return asyncio.run(run_summarize_stage(session_factory, blob_store, backend, course_id, **kwargs))


# --------------------------------------------------------------------------
# Basic extract + summarize flow
# --------------------------------------------------------------------------


def test_stage_extracts_summarizes_and_fails_appropriately(session_factory, blob_store, backend, course_id):
    # (a) a PDF-blob material, not yet extracted.
    pdf_sha, pdf_size = blob_store.put_bytes(_make_pdf_bytes("Lecture on binary search trees"))
    material_a_id = _add_material(
        session_factory, course_id,
        kind="document", title="Lecture 3", mime="application/pdf",
        sha256=pdf_sha, size_bytes=pdf_size, status="fetched",
    )

    # (b) an announcement, already extracted with a sidecar (as the /toc
    # extras path leaves it -- see ingest/repo.py's upsert_text_material).
    announcement_body = "Homework 4 is due Friday at noon."
    ann_sha, ann_size = blob_store.put_bytes(announcement_body.encode("utf-8"))
    blob_store.write_text(ann_sha, announcement_body)
    material_b_id = _add_material(
        session_factory, course_id,
        kind="announcement", title="Homework 4 reminder", mime="text/plain",
        sha256=ann_sha, size_bytes=ann_size, status="extracted",
    )

    # (c) malformed junk bytes claiming to be a PDF -- extraction fails.
    junk_sha, junk_size = blob_store.put_bytes(b"not actually a pdf, just junk bytes")
    material_c_id = _add_material(
        session_factory, course_id,
        kind="document", title="Corrupted upload", mime="application/pdf",
        sha256=junk_sha, size_bytes=junk_size, status="fetched",
    )

    stats = _run_stage(session_factory, blob_store, backend, course_id)

    material_a = _get_material(session_factory, material_a_id)
    material_b = _get_material(session_factory, material_b_id)
    material_c = _get_material(session_factory, material_c_id)

    # (a) summarized, with summary_meta_json populated and a cache row.
    assert material_a.status == "summarized"
    assert material_a.summary
    meta_a = json.loads(material_a.summary_meta_json)
    assert meta_a["prompt_version"] == PROMPT_VERSION
    assert meta_a["doc_kind_guess"]
    assert isinstance(meta_a["key_terms"], list)
    assert "usage" in meta_a

    with session_factory() as session:
        cache_row = session.execute(
            select(LlmCache).where(LlmCache.sha256 == pdf_sha, LlmCache.stage == "summarize")
        ).scalar_one_or_none()
    assert cache_row is not None
    assert cache_row.prompt_version == PROMPT_VERSION
    assert cache_row.model == backend.model_for_tier("fast")

    # (b) summarized (flowed straight into pass 2, no special-casing needed).
    assert material_b.status == "summarized"
    assert material_b.summary

    # (c) failed, with an error set.
    assert material_c.status == "failed"
    assert material_c.error

    assert stats.extracted == 1  # only (a); (c) failed extraction, (b) started already-extracted
    assert stats.summarized == 2  # (a) and (b)
    assert stats.failed == 1  # (c)
    assert stats.cached_hits == 0  # first run, nothing cached yet


# --------------------------------------------------------------------------
# Cache reuse across runs
# --------------------------------------------------------------------------


def test_rerun_reuses_cache_for_duplicate_content_no_new_rows_summaries_unchanged(
    session_factory, blob_store, backend, course_id
):
    pdf_bytes = _make_pdf_bytes("Lecture on hash tables and collision resolution")
    pdf_sha, pdf_size = blob_store.put_bytes(pdf_bytes)
    material_a_id = _add_material(
        session_factory, course_id,
        kind="document", title="Lecture 5", mime="application/pdf",
        sha256=pdf_sha, size_bytes=pdf_size, status="fetched",
    )

    announcement_body = "Midterm moved to next Tuesday."
    ann_sha, ann_size = blob_store.put_bytes(announcement_body.encode("utf-8"))
    blob_store.write_text(ann_sha, announcement_body)
    material_b_id = _add_material(
        session_factory, course_id,
        kind="announcement", title="Midterm update", mime="text/plain",
        sha256=ann_sha, size_bytes=ann_size, status="extracted",
    )

    first_stats = _run_stage(session_factory, blob_store, backend, course_id)
    assert first_stats.cached_hits == 0

    with session_factory() as session:
        cache_rows_after_first = session.execute(select(LlmCache)).scalars().all()
    cache_count_after_first = len(cache_rows_after_first)
    assert cache_count_after_first == 2  # one row per distinct sha256

    summary_a_before = _get_material(session_factory, material_a_id).summary
    summary_b_before = _get_material(session_factory, material_b_id).summary

    # A new material lands with the *same bytes* as (a) -- e.g. the file was
    # re-synced unchanged, or the same PDF was attached under a second
    # topic. Its content-addressed sha256 already has a cached summary.
    material_d_id = _add_material(
        session_factory, course_id,
        kind="document", title="Lecture 5 (mirror)", mime="application/pdf",
        sha256=pdf_sha, size_bytes=pdf_size, status="fetched",
    )

    second_stats = _run_stage(session_factory, blob_store, backend, course_id)

    assert second_stats.cached_hits >= 1

    with session_factory() as session:
        cache_rows_after_second = session.execute(select(LlmCache)).scalars().all()
    assert len(cache_rows_after_second) == cache_count_after_first  # no new cache rows

    # (a) and (b) were already summarized before the re-run and weren't
    # touched by it (pass 2 only selects summary IS NULL).
    assert _get_material(session_factory, material_a_id).summary == summary_a_before
    assert _get_material(session_factory, material_b_id).summary == summary_b_before

    material_d = _get_material(session_factory, material_d_id)
    assert material_d.status == "summarized"
    assert material_d.summary == summary_a_before  # same content -> same cached summary


# --------------------------------------------------------------------------
# Concurrency
# --------------------------------------------------------------------------


def test_concurrent_fan_out_over_five_materials_all_complete(session_factory, blob_store, backend, course_id):
    material_ids = []
    for i in range(5):
        body = f"Course notes number {i}: covering topic {i} in depth."
        sha, size = blob_store.put_bytes(body.encode("utf-8"))
        material_ids.append(
            _add_material(
                session_factory, course_id,
                kind="document", title=f"Notes {i}", mime="text/plain",
                sha256=sha, size_bytes=size, status="fetched",
            )
        )

    stats = _run_stage(session_factory, blob_store, backend, course_id, concurrency=2)

    assert stats.extracted == 5
    assert stats.summarized == 5
    assert stats.failed == 0

    for material_id in material_ids:
        material = _get_material(session_factory, material_id)
        assert material.status == "summarized"
        assert material.summary
