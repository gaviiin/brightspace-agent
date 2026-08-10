"""Tests for the S1 summarize stage: extract pass, summarize pass (with
llm_cache reuse), and concurrent fan-out -- all against MockBackend, so no
network access or API key is needed.
"""

from __future__ import annotations

import asyncio
import json
import threading

import fitz  # PyMuPDF
import pytest
from sqlalchemy import select

from brightspace_agent.agents.llm import MockBackend
from brightspace_agent.db.models import Course, LlmCache, Material, Module
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


class _CountingBackend:
    """Wraps an LLMBackend, counting `structured_call` invocations.

    Cache-row counts and summary text are consequences of whether the LLM
    was called, not direct evidence of it -- a regression that skipped the
    cache check entirely could still coincidentally leave those looking
    right (or fail loudly with an unrelated IntegrityError on the
    duplicate-PK insert instead of a clean assertion). This wrapper lets
    tests assert the call count itself: a cache hit must not increment it.
    """

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
    counting_backend = _CountingBackend(backend)

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

    first_stats = _run_stage(session_factory, blob_store, counting_backend, course_id)
    assert first_stats.cached_hits == 0
    assert counting_backend.calls == 2  # (a) and (b), both fresh cache misses

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
    # A genuinely new material with distinct content, seeded alongside (d)
    # so this test can assert *both* directions: the cache hit must not
    # reach the backend, and a real cache miss still must.
    new_body = "Lecture on graph coloring and the chromatic number."
    new_sha, new_size = blob_store.put_bytes(new_body.encode("utf-8"))
    material_e_id = _add_material(
        session_factory, course_id,
        kind="document", title="Lecture 6", mime="text/plain",
        sha256=new_sha, size_bytes=new_size, status="fetched",
    )

    calls_before_second_run = counting_backend.calls
    second_stats = _run_stage(session_factory, blob_store, counting_backend, course_id)

    assert second_stats.cached_hits >= 1
    # Direct evidence the cache hit skipped the LLM entirely: the only new
    # call across the whole second run is (e)'s genuine cache miss. If a
    # regression made the cache check a no-op, this would be off by one
    # (or more) regardless of what the cache-row/summary assertions below
    # happen to show.
    assert counting_backend.calls == calls_before_second_run + 1

    with session_factory() as session:
        cache_rows_after_second = session.execute(select(LlmCache)).scalars().all()
        pdf_cache_rows = session.execute(
            select(LlmCache).where(LlmCache.sha256 == pdf_sha, LlmCache.stage == "summarize")
        ).scalars().all()
    assert len(cache_rows_after_second) == cache_count_after_first + 1  # only (e)'s new row
    assert len(pdf_cache_rows) == 1  # (d)'s cache hit did not insert a duplicate row

    # (a) and (b) were already summarized before the re-run and weren't
    # touched by it (pass 2 only selects summary IS NULL).
    assert _get_material(session_factory, material_a_id).summary == summary_a_before
    assert _get_material(session_factory, material_b_id).summary == summary_b_before

    material_d = _get_material(session_factory, material_d_id)
    assert material_d.status == "summarized"
    assert material_d.summary == summary_a_before  # same content -> same cached summary

    material_e = _get_material(session_factory, material_e_id)
    assert material_e.status == "summarized"
    assert material_e.summary  # genuinely new content -> its own (mock) summary


@pytest.mark.parametrize(
    "poison",
    [
        pytest.param("{not json at all", id="unparseable"),
        pytest.param('{"summary": "no key_terms or doc_kind_guess here"}', id="off-schema"),
        pytest.param('{"summary": 42, "key_terms": "not-a-list", "doc_kind_guess": null}', id="wrong-types"),
    ],
)
def test_poisoned_cache_row_is_treated_as_a_miss_and_replaced(
    session_factory, blob_store, backend, course_id, poison
):
    """One bad cache row must not wedge the stage forever.

    The cached row used to be `json.loads`d and key-accessed directly, so a
    truncated write, a hand-edited row, or a row left over from an older
    output shape raised inside the worker -- and an exception in
    `_summarize_one` fails the WHOLE stage, on every run, for as long as
    the row exists. Worse, the write was `on_conflict_do_nothing`, so the
    bad row could never be replaced. S3's classify stage already validated
    at the point of read; this mirrors it.
    """
    counting = _CountingBackend(backend)
    body = "Lecture on red-black trees and rotation invariants."
    sha, size = blob_store.put_bytes(body.encode("utf-8"))
    blob_store.write_text(sha, body)
    material_id = _add_material(
        session_factory, course_id,
        kind="document", title="Lecture 7", mime="text/plain",
        sha256=sha, size_bytes=size, status="extracted",
    )

    _run_stage(session_factory, blob_store, counting, course_id)
    assert counting.calls == 1
    good_summary = _get_material(session_factory, material_id).summary

    # Poison the cache row, and reset the material so pass 2 selects it again.
    with session_factory() as session:
        row = session.execute(select(LlmCache).where(LlmCache.stage == "summarize")).scalar_one()
        row.output_json = poison
        material = session.get(Material, material_id)
        material.status = "extracted"
        material.summary = None
        session.commit()

    stats = _run_stage(session_factory, blob_store, counting, course_id)

    assert stats.failed == 0  # the stage didn't blow up on the bad row
    assert stats.cached_hits == 0  # it was a miss, not a hit
    assert counting.calls == 2  # the model was re-asked
    assert _get_material(session_factory, material_id).summary == good_summary

    with session_factory() as session:
        rows = list(session.execute(select(LlmCache).where(LlmCache.stage == "summarize")).scalars().all())
    assert len(rows) == 1  # replaced in place, not duplicated
    assert json.loads(rows[0].output_json)["key_terms"]  # and it's usable again


# --------------------------------------------------------------------------
# Pass 3 (M3.5a): metadata pseudo-document for text-less materials
# --------------------------------------------------------------------------


def _add_module(session_factory, course_id, *, title, d2l_module_id=1) -> int:
    with session_factory() as session:
        module = Module(course_id=course_id, d2l_module_id=d2l_module_id, title=title)
        session.add(module)
        session.commit()
        return module.id


def test_metadata_pass_summarizes_a_fetched_link_with_no_sha(session_factory, blob_store, course_id):
    module_id = _add_module(session_factory, course_id, title="Week 1 -- Intro")
    counting = _CountingBackend(MockBackend())
    material_id = _add_material(
        session_factory, course_id,
        kind="link", title="Case #2: The Weather Channel (Big Data)",
        source_url="https://example.edu/big-data-case", module_id=module_id, status="fetched",
    )

    stats = _run_stage(session_factory, blob_store, counting, course_id)

    material = _get_material(session_factory, material_id)
    assert material.status == "summarized"
    assert material.summary
    # Never set to the pseudo-doc hash -- see _promote_metadata_one's
    # docstring for why (compute_needed's re-fetch retry signal).
    assert material.sha256 is None
    meta = json.loads(material.summary_meta_json)
    assert meta["prompt_version"] == PROMPT_VERSION
    assert isinstance(meta["key_terms"], list)

    assert counting.calls == 1
    assert "Title: Case #2: The Weather Channel (Big Data)" in counting.prompts[0]
    assert "Kind: link" in counting.prompts[0]
    assert "Module: Week 1 -- Intro" in counting.prompts[0]
    assert "URL: https://example.edu/big-data-case" in counting.prompts[0]

    with session_factory() as session:
        cache_row = session.execute(
            select(LlmCache).where(LlmCache.stage == "summarize", LlmCache.prompt_version == PROMPT_VERSION)
        ).scalar_one()
    assert cache_row.sha256 != ""  # keyed on the pseudo-doc's own hash, not material.sha256 (which is None)

    assert stats.summarized == 1
    assert stats.extracted == 0  # nothing was extracted -- there was no file to extract
    assert stats.cached_hits == 0  # first run, nothing cached yet


def test_metadata_pass_no_module_or_url_renders_as_none(session_factory, blob_store, course_id):
    counting = _CountingBackend(MockBackend())
    _add_material(
        session_factory, course_id,
        kind="other", title="Untitled Stub", status="fetched",
    )

    _run_stage(session_factory, blob_store, counting, course_id)

    assert "Module: (none)" in counting.prompts[0]
    assert "URL: (none)" in counting.prompts[0]


def test_metadata_pass_second_identical_material_is_a_cache_hit(session_factory, blob_store, course_id):
    counting = _CountingBackend(MockBackend())
    module_id = _add_module(session_factory, course_id, title="Week 2")
    _add_material(
        session_factory, course_id,
        kind="link", title="Recommended Reading: Big-O Notation",
        source_url="https://en.wikipedia.org/wiki/Big_O_notation", module_id=module_id, status="fetched",
    )

    first_stats = _run_stage(session_factory, blob_store, counting, course_id)
    assert first_stats.summarized == 1
    assert counting.calls == 1

    with session_factory() as session:
        cache_rows_after_first = list(
            session.execute(select(LlmCache).where(LlmCache.stage == "summarize")).scalars().all()
        )
    assert len(cache_rows_after_first) == 1

    # A second material with the exact same title/kind/module/url -- same
    # pseudo-document, same cache key, even though it's a distinct material
    # row (own id, own sha256=None).
    mirror_id = _add_material(
        session_factory, course_id,
        kind="link", title="Recommended Reading: Big-O Notation",
        source_url="https://en.wikipedia.org/wiki/Big_O_notation", module_id=module_id, status="fetched",
    )

    second_stats = _run_stage(session_factory, blob_store, counting, course_id)

    assert counting.calls == 1  # the cache hit never reached the backend
    assert second_stats.cached_hits == 1
    with session_factory() as session:
        cache_rows_after_second = list(
            session.execute(select(LlmCache).where(LlmCache.stage == "summarize")).scalars().all()
        )
    assert len(cache_rows_after_second) == 1  # no duplicate row from the cache hit

    mirror = _get_material(session_factory, mirror_id)
    assert mirror.status == "summarized"
    assert mirror.summary  # populated from the cache, not a fresh call


def test_metadata_pseudo_doc_cache_key_changes_when_title_changes(session_factory, blob_store, course_id):
    counting = _CountingBackend(MockBackend())
    _add_material(
        session_factory, course_id, kind="link", title="Reading A",
        source_url="https://example.edu/a", status="fetched",
    )
    _add_material(
        session_factory, course_id, kind="link", title="Reading B",  # only the title differs
        source_url="https://example.edu/a", status="fetched",
    )

    _run_stage(session_factory, blob_store, counting, course_id)

    assert counting.calls == 2  # two distinct pseudo-docs, two distinct cache misses
    with session_factory() as session:
        shas = {
            row.sha256
            for row in session.execute(select(LlmCache).where(LlmCache.stage == "summarize")).scalars()
        }
    assert len(shas) == 2  # different titles -> different pseudo-doc hashes -> different cache keys


def test_metadata_pass_ignores_kinds_outside_the_allowlist(session_factory, blob_store, course_id):
    """A 'document'/'slides'/etc. material stuck at 'fetched' with no
    sha256 is a genuine gap (an upload that never completed), not something
    pass 3 should paper over with a title-only guess."""
    counting = _CountingBackend(MockBackend())
    material_id = _add_material(
        session_factory, course_id, kind="document", title="Never Uploaded", status="fetched",
    )

    stats = _run_stage(session_factory, blob_store, counting, course_id)

    assert counting.calls == 0
    assert stats.summarized == 0
    material = _get_material(session_factory, material_id)
    assert material.status == "fetched"  # untouched, left for a real upload to fix


@pytest.mark.parametrize("kind", ["link", "assignment", "announcement", "other"])
def test_metadata_pass_covers_every_allowlisted_kind(session_factory, blob_store, course_id, kind):
    counting = _CountingBackend(MockBackend())
    material_id = _add_material(
        session_factory, course_id, kind=kind, title=f"A {kind} with no content", status="fetched",
    )

    stats = _run_stage(session_factory, blob_store, counting, course_id)

    assert counting.calls == 1
    assert stats.summarized == 1
    material = _get_material(session_factory, material_id)
    assert material.status == "summarized"
    assert material.summary


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


# --------------------------------------------------------------------------
# Cache-race guard (Task 9 folded-in fix)
# --------------------------------------------------------------------------


class _BarrierBackend:
    """Wraps a backend and forces every `structured_call` to block until
    `n` calls are in flight simultaneously, before letting all of them
    proceed together.

    Without this, two workers racing to summarize identical bytes is a
    matter of thread-scheduling luck -- usually fine, occasionally a flaky
    test. The barrier makes the race deterministic: both workers are
    guaranteed to have already missed the cache before either one writes to
    it, every time this test runs.
    """

    def __init__(self, inner, n: int) -> None:
        self._inner = inner
        self._barrier = threading.Barrier(n)

    def structured_call(self, schema, *, system, user, tier):
        self._barrier.wait(timeout=5)
        return self._inner.structured_call(schema, system=system, user=user, tier=tier)

    def model_for_tier(self, tier):
        return self._inner.model_for_tier(tier)


def test_concurrent_identical_content_cache_race_does_not_fail_either_material(
    session_factory, blob_store, backend, course_id
):
    """Regression: two materials with byte-identical content, summarized
    concurrently, both miss the cache (neither has committed yet) and both
    call the LLM. Without a guard, the second `llm_cache` insert raises
    IntegrityError on the shared (sha256, stage, prompt_version, model) key
    -- which used to propagate out of `asyncio.gather` uncaught, crashing
    the whole stage rather than just that one material."""
    body = "Shared lecture content for the cache-race regression test."
    sha256, size = blob_store.put_bytes(body.encode("utf-8"))
    blob_store.write_text(sha256, body)
    material_a_id = _add_material(
        session_factory, course_id,
        kind="document", title="Copy A", mime="text/plain",
        sha256=sha256, size_bytes=size, status="extracted",
    )
    material_b_id = _add_material(
        session_factory, course_id,
        kind="document", title="Copy B", mime="text/plain",
        sha256=sha256, size_bytes=size, status="extracted",
    )

    racing_backend = _BarrierBackend(backend, 2)
    stats = _run_stage(session_factory, blob_store, racing_backend, course_id, concurrency=2)

    material_a = _get_material(session_factory, material_a_id)
    material_b = _get_material(session_factory, material_b_id)
    assert material_a.status == "summarized"
    assert material_b.status == "summarized"
    assert material_a.summary
    assert material_b.summary
    assert stats.summarized == 2
    assert stats.failed == 0

    with session_factory() as session:
        cache_rows = session.execute(
            select(LlmCache).where(LlmCache.sha256 == sha256, LlmCache.stage == "summarize")
        ).scalars().all()
    assert len(cache_rows) == 1  # the race produced exactly one row, not zero and not a crash


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


def test_cost_cap_stops_the_worklist_and_leaves_the_rest_unprocessed(
    session_factory, blob_store, backend, course_id
):
    material_ids = []
    for i in range(3):
        body = f"Distinct lecture body number {i} about topic {i}."
        sha, size = blob_store.put_bytes(body.encode("utf-8"))
        material_ids.append(
            _add_material(
                session_factory, course_id,
                kind="document", title=f"Lecture {i}", mime="text/plain",
                sha256=sha, size_bytes=size, status="extracted",
            )
        )

    costly_backend = _FixedCostBackend(backend, est_cost_usd=10.0)
    # concurrency=1: the cap check happens per material, right before that
    # material's LLM call -- serial processing is what makes "exactly one
    # call, then abort" deterministic rather than a matter of thread timing.
    stats = _run_stage(
        session_factory, blob_store, costly_backend, course_id, concurrency=1, cost_cap_usd=5.0
    )

    assert costly_backend.calls == 1  # cap (5) < per-call cost (10): only the first call happens
    assert stats.aborted is True
    assert stats.summarized == 1
    assert stats.failed == 0

    statuses = {
        material_id: _get_material(session_factory, material_id).status for material_id in material_ids
    }
    summarized_count = sum(1 for status in statuses.values() if status == "summarized")
    extracted_count = sum(1 for status in statuses.values() if status == "extracted")
    assert summarized_count == 1
    assert extracted_count == 2  # left untouched (not 'failed') for a later run to retry


def test_cost_cap_never_blocks_a_cache_hit(session_factory, blob_store, backend, course_id):
    """A cache hit spends nothing, so it must never trip the cap -- even a
    cap of 0 should still let every cache hit through."""
    body = "Cached lecture content."
    sha256, size = blob_store.put_bytes(body.encode("utf-8"))
    blob_store.write_text(sha256, body)
    _add_material(
        session_factory, course_id,
        kind="document", title="First copy", mime="text/plain",
        sha256=sha256, size_bytes=size, status="extracted",
    )
    _run_stage(session_factory, blob_store, backend, course_id)  # primes the cache

    mirror_id = _add_material(
        session_factory, course_id,
        kind="document", title="Second copy (same bytes)", mime="text/plain",
        sha256=sha256, size_bytes=size, status="extracted",
    )

    stats = _run_stage(session_factory, blob_store, backend, course_id, cost_cap_usd=0.0)

    assert stats.aborted is False
    assert stats.cached_hits == 1
    assert _get_material(session_factory, mirror_id).status == "summarized"
