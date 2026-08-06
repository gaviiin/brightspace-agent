"""D2L Table-of-Contents parsing and needed-list diffing.

`parse_toc` walks the raw D2L ToC JSON tree defensively: any module or topic
missing a required field is skipped rather than raising, since we don't
control the shape of what D2L sends and per-tenant quirks are expected (see
docs/plan.md's "per-tenant API variation" risk). `compute_needed` then
diffs the parsed File-type topics against what's already stored to decide
what the extension still needs to download.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from brightspace_agent.db.models import Material

# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ModuleRef:
    """One module node in a topic's ancestor chain (root-to-parent order)."""

    d2l_module_id: int
    title: str
    sort_order: int  # index among siblings within its parent's Modules list


@dataclass(frozen=True)
class TocEntry:
    """One topic parsed from the ToC tree, carrying its module ancestry."""

    topic_id: int
    title: str
    type_identifier: str
    url: str | None
    last_modified_date: str | None
    size_bytes: int | None
    module_chain: list[ModuleRef] = field(default_factory=list)


def parse_toc(toc: dict) -> list[TocEntry]:
    """Walk `toc["Modules"]` recursively, returning every parseable topic.

    Defensive: a module or topic missing its required keys (or with the
    wrong type) is skipped, never raised on. Modules are not returned
    directly -- each TocEntry carries the full chain of ancestor modules it
    was found under (`module_chain`), which is enough for a caller to
    reconstruct/upsert the module tree.
    """
    entries: list[TocEntry] = []
    if not isinstance(toc, dict):
        return entries
    _walk_modules(toc.get("Modules"), [], entries)
    return entries


def _walk_modules(modules: object, chain: list[ModuleRef], entries: list[TocEntry]) -> None:
    if not isinstance(modules, list):
        return
    for index, module in enumerate(modules):
        if not isinstance(module, dict):
            continue
        module_id = module.get("ModuleId")
        title = module.get("Title")
        if not isinstance(module_id, int) or not isinstance(title, str):
            continue

        new_chain = [*chain, ModuleRef(d2l_module_id=module_id, title=title, sort_order=index)]

        topics = module.get("Topics")
        if isinstance(topics, list):
            for topic in topics:
                entry = _parse_topic(topic, new_chain)
                if entry is not None:
                    entries.append(entry)

        _walk_modules(module.get("Modules"), new_chain, entries)


def _parse_topic(topic: object, module_chain: list[ModuleRef]) -> TocEntry | None:
    if not isinstance(topic, dict):
        return None
    topic_id = topic.get("TopicId")
    title = topic.get("Title")
    type_identifier = topic.get("TypeIdentifier")
    if not isinstance(topic_id, int) or not isinstance(title, str) or not isinstance(type_identifier, str):
        return None

    url = topic.get("Url")
    last_modified_date = topic.get("LastModifiedDate")
    size_bytes = topic.get("SizeBytes")

    return TocEntry(
        topic_id=topic_id,
        title=title,
        type_identifier=type_identifier,
        url=url if isinstance(url, str) else None,
        last_modified_date=last_modified_date if isinstance(last_modified_date, str) else None,
        size_bytes=size_bytes if isinstance(size_bytes, int) else None,
        module_chain=list(module_chain),
    )


def is_file_topic(entry: TocEntry) -> bool:
    """True for File-type topics with enough info to be diffed/downloaded."""
    return entry.type_identifier == "File" and entry.url is not None


# --------------------------------------------------------------------------
# Diffing
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class NeededItem:
    d2l_topic_id: int
    url: str
    title: str
    size_hint: int | None
    last_modified: str | None


def compute_needed(session: Session, course_id: int, entries: list[TocEntry]) -> list[NeededItem]:
    """Return the File-type topics that still need downloading.

    A File topic is needed iff: no material row exists for its
    `d2l_topic_id` yet, OR the stored `sha256` is null (a stub row, or a
    row whose upload never actually completed -- must never become
    invisible to future diffs regardless of what `d2l_updated_at` says),
    OR the stored `d2l_updated_at` is null, OR the incoming
    `LastModifiedDate` parses to a later timestamp than what's stored.
    """
    file_entries = [e for e in entries if is_file_topic(e)]
    topic_ids = [e.topic_id for e in file_entries]

    stored: dict[int, tuple[str | None, str | None]] = {}
    if topic_ids:
        rows = session.execute(
            select(Material.d2l_topic_id, Material.d2l_updated_at, Material.sha256).where(
                Material.course_id == course_id,
                Material.d2l_topic_id.in_(topic_ids),
            )
        ).all()
        stored = {topic_id: (updated_at, sha256) for topic_id, updated_at, sha256 in rows}

    needed: list[NeededItem] = []
    for entry in file_entries:
        if entry.topic_id not in stored:
            is_needed = True
        else:
            stored_updated, stored_sha256 = stored[entry.topic_id]
            if stored_sha256 is None:
                is_needed = True
            elif stored_updated is None:
                is_needed = True
            else:
                incoming_dt = _parse_iso(entry.last_modified_date)
                stored_dt = _parse_iso(stored_updated)
                is_needed = incoming_dt is not None and stored_dt is not None and incoming_dt > stored_dt

        if is_needed:
            needed.append(NeededItem(
                d2l_topic_id=entry.topic_id,
                url=entry.url,  # type: ignore[arg-type]  # guaranteed non-None by is_file_topic
                title=entry.title,
                size_hint=entry.size_bytes,
                last_modified=entry.last_modified_date,
            ))

    return needed


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Kind inference
# --------------------------------------------------------------------------

_DOCUMENT_EXTS = {"pdf", "doc", "docx", "txt", "md", "html"}
_SLIDES_EXTS = {"ppt", "pptx", "key"}
_TRANSCRIPT_EXTS = {"vtt", "srt"}
_VIDEO_EXTS = {"mp4", "mov", "m4v"}


def infer_kind(title: str, url: str | None) -> str:
    """Guess a material's `kind` from its title/URL. Only meaningful for
    File-type topics -- Link topics are always kind='link', assigned by the
    caller rather than through this function."""
    if title and "syllabus" in title.lower():
        return "syllabus"

    ext = _extension(url) or _extension(title)
    if ext in _DOCUMENT_EXTS:
        return "document"
    if ext in _SLIDES_EXTS:
        return "slides"
    if ext in _TRANSCRIPT_EXTS:
        return "transcript"
    if ext in _VIDEO_EXTS:
        return "video"
    return "other"


def _extension(value: str | None) -> str:
    if not value:
        return ""
    path = urlparse(value).path or value
    return PurePosixPath(path).suffix.lstrip(".").lower()
