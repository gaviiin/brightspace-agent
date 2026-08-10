"""Tests for the M2.3 transcript ingest (media/ingest_transcript.py): turning
a VTT file into a kind='transcript' material and wiring the owning
`media_sources` row to it.

Fixtures mirror test_media_detect.py's direct DB/blob-store setup (no HTTP).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from brightspace_agent.db.models import (
    Course,
    LlmCache,
    Material,
    MaterialTopic,
    MediaSource,
    Module,
    Topic,
)
from brightspace_agent.db.session import init_db
from brightspace_agent.ingest.store import BlobStore
from brightspace_agent.media.ingest_transcript import ingest_transcript


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BSA_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def db(data_dir):
    return init_db(data_dir / "brightspace.db")


@pytest.fixture
def session_factory(db):
    return db[1]


@pytest.fixture
def blob_store(data_dir):
    return BlobStore(blobs_dir=data_dir / "blobs", text_dir=data_dir / "text")


@pytest.fixture
def course_id(session_factory):
    with session_factory() as session:
        course = Course(d2l_org_unit_id=1, tenant_origin="school.d2l.com", name="Intro to CS")
        session.add(course)
        session.commit()
        return course.id


@pytest.fixture
def module_id(session_factory, course_id):
    with session_factory() as session:
        module = Module(course_id=course_id, d2l_module_id=42, title="Week 3", sort_order=0)
        session.add(module)
        session.commit()
        return module.id


@pytest.fixture
def source_material_id(session_factory, course_id, module_id):
    with session_factory() as session:
        material = Material(
            course_id=course_id,
            module_id=module_id,
            d2l_topic_id=99,
            kind="video",
            title="Lecture 3 Recording",
            source_url="https://zoom.us/rec/share/abc123",
            status="fetched",
        )
        session.add(material)
        session.commit()
        return material.id


@pytest.fixture
def media_source_id(session_factory, course_id, source_material_id):
    with session_factory() as session:
        media_source = MediaSource(
            course_id=course_id,
            material_id=source_material_id,
            platform="zoom",
            url="https://zoom.us/rec/share/abc123",
            status="transcribing",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        session.add(media_source)
        session.commit()
        return media_source.id


def _write_vtt(tmp_path, name: str, cue_text: str):
    path = tmp_path / name
    path.write_text(
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:02.000\n"
        f"{cue_text}\n",
        encoding="utf-8",
    )
    return path


def _get_material(session_factory, material_id) -> Material:
    with session_factory() as session:
        material = session.get(Material, material_id)
        session.expunge(material)
        return material


def _material_topic_count(session_factory, material_id) -> int:
    with session_factory() as session:
        return len(
            session.execute(
                select(MaterialTopic).where(MaterialTopic.material_id == material_id)
            ).scalars().all()
        )


def _get_media_source(session_factory, media_source_id) -> MediaSource:
    with session_factory() as session:
        media_source = session.get(MediaSource, media_source_id)
        session.expunge(media_source)
        return media_source


def _all_materials(session_factory, course_id) -> list[Material]:
    with session_factory() as session:
        return list(
            session.execute(select(Material).where(Material.course_id == course_id)).scalars().all()
        )


# --------------------------------------------------------------------------
# Creates the material
# --------------------------------------------------------------------------


def test_ingest_creates_transcript_material(
    session_factory, blob_store, course_id, module_id, source_material_id, media_source_id, tmp_path
):
    vtt_path = _write_vtt(tmp_path, "captions.vtt", "This is the first cue of the lecture.")

    material_id = ingest_transcript(session_factory, blob_store, media_source_id, vtt_path)

    material = _get_material(session_factory, material_id)
    assert material.kind == "transcript"
    assert material.title == "Lecture 3 Recording (transcript)"
    assert material.module_id == module_id
    assert material.course_id == course_id
    assert material.mime == "text/vtt"
    assert material.d2l_topic_id is None
    assert material.status == "extracted"
    assert material.sha256 is not None
    assert material.summary is None


def test_ingest_blob_stored_and_text_sidecar_readable_and_timestamp_free(
    session_factory, blob_store, media_source_id, tmp_path
):
    vtt_path = _write_vtt(tmp_path, "captions.vtt", "A sentence spoken during the lecture.")

    material_id = ingest_transcript(session_factory, blob_store, media_source_id, vtt_path)
    material = _get_material(session_factory, material_id)

    assert blob_store.exists(material.sha256)
    text = blob_store.read_text(material.sha256)
    assert text is not None
    assert "A sentence spoken during the lecture." in text
    assert "-->" not in text
    assert "WEBVTT" not in text
    assert "00:00:00.000" not in text


# --------------------------------------------------------------------------
# Updates the media_sources row
# --------------------------------------------------------------------------


def test_ingest_updates_media_source_row(session_factory, blob_store, media_source_id, tmp_path):
    vtt_path = _write_vtt(tmp_path, "captions.vtt", "Some transcribed words.")

    material_id = ingest_transcript(session_factory, blob_store, media_source_id, vtt_path)

    media_source = _get_media_source(session_factory, media_source_id)
    assert media_source.transcript_material_id == material_id
    assert media_source.status == "done"
    assert media_source.error is None
    assert media_source.updated_at != "2026-01-01T00:00:00+00:00"


def test_ingest_clears_preexisting_error(session_factory, blob_store, course_id, media_source_id, tmp_path):
    with session_factory() as session:
        media_source = session.get(MediaSource, media_source_id)
        media_source.error = "some earlier transcription failure"
        session.commit()

    vtt_path = _write_vtt(tmp_path, "captions.vtt", "Recovered after a retry.")
    ingest_transcript(session_factory, blob_store, media_source_id, vtt_path)

    media_source = _get_media_source(session_factory, media_source_id)
    assert media_source.error is None
    assert media_source.status == "done"


# --------------------------------------------------------------------------
# Idempotent re-ingest
# --------------------------------------------------------------------------


def test_reingest_updates_same_material_in_place(
    session_factory, blob_store, course_id, media_source_id, tmp_path
):
    first_vtt = _write_vtt(tmp_path, "first.vtt", "The original transcript content.")
    first_id = ingest_transcript(session_factory, blob_store, media_source_id, first_vtt)

    second_vtt = _write_vtt(tmp_path, "second.vtt", "A completely different re-transcription.")
    second_id = ingest_transcript(session_factory, blob_store, media_source_id, second_vtt)

    assert second_id == first_id

    material = _get_material(session_factory, first_id)
    text = blob_store.read_text(material.sha256)
    assert "A completely different re-transcription." in text
    assert "The original transcript content." not in text

    materials = [m for m in _all_materials(session_factory, course_id) if m.kind == "transcript"]
    assert len(materials) == 1


def test_reingest_resets_stale_summary_and_topic_assignments(
    session_factory, blob_store, course_id, media_source_id, tmp_path
):
    first_vtt = _write_vtt(tmp_path, "first.vtt", "Content before the re-transcribe.")
    material_id = ingest_transcript(session_factory, blob_store, media_source_id, first_vtt)

    # Simulate S1/S3 having already run on the first version: a summary, a
    # cache entry, and -- the "topic assignments" half this test's name
    # promises -- a real material_topics row at the course's taxonomy
    # version, which is what actually files the transcript under a topic in
    # the graph. Left behind across a re-transcribe, it would keep the new
    # content attached to topics derived from the OLD content.
    with session_factory() as session:
        material = session.get(Material, material_id)
        material.status = "summarized"
        material.summary = "An old, now-stale summary."
        course = session.get(Course, course_id)
        topic = Topic(
            course_id=course_id, taxonomy_version=course.taxonomy_version, slug="sorting", name="Sorting"
        )
        session.add(topic)
        session.flush()
        session.add(
            MaterialTopic(
                material_id=material_id,
                topic_id=topic.id,
                taxonomy_version=course.taxonomy_version,
                confidence=0.9,
                rationale="assigned from the first transcription",
            )
        )
        session.add(
            LlmCache(
                sha256=material.sha256,
                stage="summarize",
                prompt_version="s1.v1",
                model="mock-fast",
                output_json="{}",
                created_at="2026-01-01T00:00:00+00:00",
            )
        )
        session.commit()

    assert _material_topic_count(session_factory, material_id) == 1  # the precondition really held

    second_vtt = _write_vtt(tmp_path, "second.vtt", "Brand new re-transcribed content.")
    ingest_transcript(session_factory, blob_store, media_source_id, second_vtt)

    material = _get_material(session_factory, material_id)
    assert material.status == "extracted"
    assert material.summary is None
    assert _material_topic_count(session_factory, material_id) == 0


# --------------------------------------------------------------------------
# Missing media_source id
# --------------------------------------------------------------------------


def test_missing_media_source_id_raises_clear_error(session_factory, blob_store, tmp_path):
    vtt_path = _write_vtt(tmp_path, "captions.vtt", "Doesn't matter, should never be read.")

    with pytest.raises(ValueError, match="404404"):
        ingest_transcript(session_factory, blob_store, 404404, vtt_path)


# --------------------------------------------------------------------------
# M2.6a: media_source.material_id may be NULL (a manually-added URL/channel
# entry has no backing `materials` row -- see api/media.py's POST
# .../media/add). ingest_transcript must handle that end to end rather than
# raising the "no materials row" ValueError meant for a genuinely dangling
# (non-NULL but unresolved) material_id.
# --------------------------------------------------------------------------


def _add_manual_media_source(session_factory, course_id, url, **overrides) -> int:
    defaults = dict(
        course_id=course_id,
        material_id=None,
        platform="mediasite",
        url=url,
        status="transcribing",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    defaults.update(overrides)
    with session_factory() as session:
        media_source = MediaSource(**defaults)
        session.add(media_source)
        session.commit()
        return media_source.id


def test_ingest_with_null_source_material_derives_title_from_url_last_segment(
    session_factory, blob_store, course_id, tmp_path
):
    media_source_id = _add_manual_media_source(
        session_factory, course_id, "https://mock.mediasite.example/Mediasite/Play/lecture-9"
    )
    vtt_path = _write_vtt(tmp_path, "captions.vtt", "A manually-added recording's first cue.")

    material_id = ingest_transcript(session_factory, blob_store, media_source_id, vtt_path)

    material = _get_material(session_factory, material_id)
    assert material.title == "lecture-9 (transcript)"
    assert material.course_id == course_id
    assert material.module_id is None
    assert material.d2l_topic_id is None
    assert material.kind == "transcript"
    assert material.status == "extracted"
    assert material.sha256 is not None


def test_ingest_with_null_source_material_and_no_path_segment_falls_back_to_recording_id(
    session_factory, blob_store, course_id, tmp_path
):
    media_source_id = _add_manual_media_source(session_factory, course_id, "https://mock.mediasite.example")
    vtt_path = _write_vtt(tmp_path, "captions.vtt", "Another manually-added recording.")

    material_id = ingest_transcript(session_factory, blob_store, media_source_id, vtt_path)

    material = _get_material(session_factory, material_id)
    assert material.title == f"Recording {media_source_id} (transcript)"


def test_ingest_with_null_source_material_url_decodes_the_path_segment(
    session_factory, blob_store, course_id, tmp_path
):
    media_source_id = _add_manual_media_source(
        session_factory, course_id, "https://mock.mediasite.example/Mediasite/Play/Week%201%20Intro"
    )
    vtt_path = _write_vtt(tmp_path, "captions.vtt", "Percent-encoded path segment.")

    material_id = ingest_transcript(session_factory, blob_store, media_source_id, vtt_path)

    material = _get_material(session_factory, material_id)
    assert material.title == "Week 1 Intro (transcript)"


def test_ingest_with_null_source_material_updates_the_media_source_row(
    session_factory, blob_store, course_id, tmp_path
):
    media_source_id = _add_manual_media_source(
        session_factory, course_id, "https://mock.mediasite.example/Mediasite/Play/abc"
    )
    vtt_path = _write_vtt(tmp_path, "captions.vtt", "Some transcribed words.")

    material_id = ingest_transcript(session_factory, blob_store, media_source_id, vtt_path)

    media_source = _get_media_source(session_factory, media_source_id)
    assert media_source.transcript_material_id == material_id
    assert media_source.status == "done"
    assert media_source.error is None


def test_reingest_with_null_source_material_updates_same_material_in_place(
    session_factory, blob_store, course_id, tmp_path
):
    media_source_id = _add_manual_media_source(
        session_factory, course_id, "https://mock.mediasite.example/Mediasite/Play/abc"
    )
    first_vtt = _write_vtt(tmp_path, "first.vtt", "The original transcript content.")
    first_id = ingest_transcript(session_factory, blob_store, media_source_id, first_vtt)

    second_vtt = _write_vtt(tmp_path, "second.vtt", "A completely different re-transcription.")
    second_id = ingest_transcript(session_factory, blob_store, media_source_id, second_vtt)

    assert second_id == first_id
    materials = [m for m in _all_materials(session_factory, course_id) if m.kind == "transcript"]
    assert len(materials) == 1
