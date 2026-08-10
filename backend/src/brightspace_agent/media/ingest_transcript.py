"""M2.3 transcript ingest: turns a WebVTT file (from platform captions via
`media/fetch.py` OR local ASR via `media/transcribe.py` -- either source
lands here through the exact same path) into a `kind='transcript'` material
that S1's summarize stage picks up unchanged, and points the owning
`media_sources` row at it.

This module does NOT fetch, transcribe, keep/delete the audio file, or run
the pipeline -- that's `media/fetch.py`/`media/transcribe.py` and (M2.4) the
orchestration that calls all three of those plus this one in sequence.
`ingest_transcript` only knows how to turn an already-written `.vtt` file
into a database row.

Two things worth knowing:

- **`d2l_topic_id` is always NULL on the transcript material** -- it's
  app-created, not something D2L's ToC ever names a topic id for. Verified
  safe against the sync diff:
  - `ingest/diff.py`'s `compute_needed` only ever diffs `TocEntry`s parsed
    straight from a real D2L ToC tree, each carrying a real integer
    `topic_id` (`_parse_topic` requires `isinstance(topic_id, int)`) -- it
    never produces or looks up `None`.
  - `ingest/repo.py`'s `upsert_link_material`/`upsert_file_stub_material`/
    `upsert_file_material` all key their existing-row lookup on
    `Material.d2l_topic_id == entry.topic_id` (or `== d2l_topic_id`), always
    a real int from the ToC -- `Material.d2l_topic_id == None` can never be
    the condition SQL evaluates, so a NULL-topic row is structurally
    unreachable from any of them.
  - `schema.sql`'s own uniqueness rule is a *partial* unique index --
    `CREATE UNIQUE INDEX ux_materials_course_topic ON materials(course_id,
    d2l_topic_id) WHERE d2l_topic_id IS NOT NULL` -- so any number of
    NULL-topic materials in one course coexist without ever colliding, by
    construction, independent of the above.
  Together: a resync can never match, update, or collide with a transcript
  material.
- **`status='extracted'`, not `'fetched'`.** The VTT's text is extracted
  right here (via the same `extract_text` the S1 extract pass calls on
  synced materials, given mime='text/vtt'), so this material skips straight
  to S1's summarize pass -- see `run_summarize_stage`'s second worklist
  query (`status='extracted'`, `summary IS NULL`). This is the exact
  convention `ingest/repo.py`'s `upsert_text_material` already uses for
  extras (announcements/assignments): "the body IS the extracted text ...
  so this material re-enters the pipeline at the summarize pass."
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from brightspace_agent.db.models import Material, MediaSource
from brightspace_agent.ingest.extract import extract_text
from brightspace_agent.ingest.repo import now_iso, reset_pipeline_progress
from brightspace_agent.ingest.store import BlobStore

_VTT_MIME = "text/vtt"


def ingest_transcript(
    session_factory: sessionmaker[Session],
    blob_store: BlobStore,
    media_source_id: int,
    vtt_path: Path,
) -> int:
    """Store `vtt_path` as `media_source_id`'s transcript material. Returns
    the material's id.

    Idempotent: if the `media_sources` row already has a
    `transcript_material_id`, that SAME material is updated in place (new
    blob/sha/text/status) rather than a second row being inserted -- calling
    this twice for one media source (a re-transcribe, a later re-detected
    caption) never leaves an orphaned duplicate transcript material behind.
    """
    with session_factory() as session:
        media_source = session.get(MediaSource, media_source_id)
        if media_source is None:
            raise ValueError(f"no media_sources row with id {media_source_id}")

        source_material = session.get(Material, media_source.material_id)
        if source_material is None:
            raise ValueError(f"no materials row with id {media_source.material_id}")

        vtt_bytes = vtt_path.read_bytes()
        sha256, size = blob_store.put_bytes(vtt_bytes)
        text = extract_text(blob_store.path_for(sha256), _VTT_MIME, "transcript") or ""
        blob_store.write_text(sha256, text)

        title = f"{source_material.title} (transcript)"

        material = None
        if media_source.transcript_material_id is not None:
            material = session.get(Material, media_source.transcript_material_id)
        sha_unchanged = material is not None and material.sha256 == sha256

        if material is None:
            material = Material(
                course_id=source_material.course_id,
                module_id=source_material.module_id,
                d2l_topic_id=None,
                kind="transcript",
                title=title,
                status="extracted",
            )
            session.add(material)

        material.course_id = source_material.course_id
        material.module_id = source_material.module_id
        material.d2l_topic_id = None
        material.kind = "transcript"
        material.title = title
        material.sha256 = sha256
        material.mime = _VTT_MIME
        material.size_bytes = size
        material.fetched_at = now_iso()
        session.flush()

        if not sha_unchanged:
            # Bytes actually changed (or this is a brand new transcript
            # material): reset pipeline progress -- topic assignments
            # included -- so a re-transcribed recording is re-summarized/
            # re-classified from its new content instead of keeping a stale
            # summary forever. Mirrors upsert_file_material/
            # upsert_text_material's identical sha_unchanged gate.
            reset_pipeline_progress(session, source_material.course_id, material, status="extracted")
            session.flush()

        media_source.transcript_material_id = material.id
        media_source.status = "done"
        media_source.error = None
        media_source.updated_at = now_iso()

        session.commit()
        return material.id
