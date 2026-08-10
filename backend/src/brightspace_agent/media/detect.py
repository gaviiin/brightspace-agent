"""M2.1 recording-URL detector: finds lecture-recording links (Mediasite/
Zoom/Google Drive) in already-synced course content and upserts them into
`media_sources`, so a later M2 task can fetch and transcribe them.

Fully deterministic -- no network, no LLM -- so `detect_media_sources` can be
re-run for a course any time at no cost (e.g. on every sync completion; see
api/ingest.py's `/complete` handler).

Scans two places per course, matching the brief's exact rules:

1. Link materials (`kind == 'link'`): the material's `source_url`.
2. HTML page materials: any material whose blob is HTML. Real dispatch is
   mime-driven, same reasoning as `ingest/extract.py`'s `_detect_format`
   docstring -- blob-store paths are sha256-named with no extension, so
   there's no extension fallback to fall back to. Hrefs are parsed with
   BeautifulSoup (no regex-over-HTML), and the page's visible text is also
   scanned for a nearby Zoom passcode.

Classification (`classify_url`) is pure string/host/path matching -- no
network calls, so a bad or unreachable URL still gets recorded. See that
function for the exact per-platform rules.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from brightspace_agent.config import Settings
from brightspace_agent.db.models import Material, MediaSource
from brightspace_agent.ingest.store import BlobStore

logger = logging.getLogger(__name__)

# "Passcode: aBc123" / "password = aBc123" / "Passcode aBc123" (colon/equals
# optional, case-insensitive keyword). \S+ grabs the token whole; trailing
# punctuation swept up with it (a sentence's closing "." or ")") is trimmed
# by _clean_passcode -- but not "!" or "?", since those are plausible
# passcode characters in their own right, not sentence punctuation.
_PASSCODE_RE = re.compile(r"(?:passcode|password)\s*[:=]?\s*(\S+)", re.IGNORECASE)
_TRAILING_PUNCT_RE = re.compile(r"[.,;:)\]\"'’]+$")

_MEDIASITE_PATH_MARKERS = ("/mediasite/play/", "/mediasite/showcase/", "/mediasite/catalog/")
_ZOOM_PATH_PREFIXES = ("/rec/share/", "/rec/play/", "/recording/play/", "/clips/share/")


@dataclass
class DetectStats:
    scanned_materials: int
    found: int  # candidate URLs seen (before dedup against existing rows)
    added: int  # new media_sources rows inserted


def detect_media_sources(session_factory: sessionmaker[Session], course_id: int) -> DetectStats:
    """Scan every material in `course_id` for recording URLs and upsert them
    into `media_sources`. Safe to call repeatedly -- see `_upsert` for the
    exact "never touch status/error/transcript_material_id" rule that makes
    re-detection non-destructive of downstream (fetch/transcribe) progress.
    """
    blob_store = _open_blob_store()
    stats = DetectStats(scanned_materials=0, found=0, added=0)

    with session_factory() as session:
        materials = list(
            session.execute(select(Material).where(Material.course_id == course_id)).scalars().all()
        )
        for material in materials:
            if material.kind != "link" and not _is_html_material(material):
                continue
            stats.scanned_materials += 1

            for candidate in _find_candidates(material, blob_store):
                stats.found += 1
                if _upsert(session, course_id, material.id, candidate):
                    stats.added += 1

        session.commit()

    return stats


def _open_blob_store() -> BlobStore:
    """A `BlobStore` built straight from `Settings()`, matching main.py's own
    construction. `detect_media_sources` takes only (session_factory,
    course_id) per its public API, so unlike a request handler (which reads
    `app.state.blob_store` via api/deps.py's `get_blob_store`), it can't be
    handed one -- it builds its own from the same environment-derived
    Settings the app used at startup, which resolves to the identical
    blobs_dir/text_dir.
    """
    settings = Settings()
    return BlobStore(settings.blobs_dir, settings.text_dir)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Per-material candidate extraction
# --------------------------------------------------------------------------


@dataclass
class _Candidate:
    platform: str  # 'mediasite' | 'zoom' | 'gdrive'
    url: str
    passcode: str | None = None


def _is_html_material(material: Material) -> bool:
    """True if `material`'s blob is an HTML page. Mime-only (no extension
    fallback): blob-store paths are sha256-named with no extension, so mime
    is what actually drives this in the real pipeline -- exactly
    extract.py's `_detect_format` reasoning, re-derived here rather than
    imported since that function is private to extract.py."""
    if not material.sha256 or not material.mime:
        return False
    return material.mime.split(";", 1)[0].strip().lower() == "text/html"


def _find_candidates(material: Material, blob_store: BlobStore) -> list[_Candidate]:
    if material.kind == "link":
        if not material.source_url:
            return []
        candidate = classify_url(material.source_url)
        return [candidate] if candidate else []

    # HTML page material (the only other branch `detect_media_sources`
    # dispatches here for -- see its `_is_html_material` gate).
    if not material.sha256 or not blob_store.exists(material.sha256):
        return []
    path = blob_store.path_for(material.sha256)
    try:
        raw_html = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        logger.warning("media detect: could not read blob for material %s", material.id)
        return []

    return _candidates_from_html(raw_html)


def _candidates_from_html(raw_html: str) -> list[_Candidate]:
    soup = BeautifulSoup(raw_html, "html.parser")
    candidates: list[_Candidate] = []

    for a_tag in soup.find_all("a", href=True):
        candidate = classify_url(a_tag["href"])
        if candidate is None:
            continue
        # Link materials have no surrounding text to search (handled by
        # `classify_url`'s query-param check only); HTML pages do -- fill
        # in a passcode from nearby text unless the URL's own query string
        # already supplied one.
        if candidate.platform == "zoom" and candidate.passcode is None:
            candidate.passcode = _passcode_near(a_tag, soup)
        candidates.append(candidate)

    return candidates


def _passcode_near(a_tag, soup: BeautifulSoup) -> str | None:
    """First passcode-looking token near `a_tag`: its immediate parent
    block's text is searched first -- a match there is "nearer" the link by
    construction -- falling back to the whole page's text."""
    parent = a_tag.parent
    if parent is not None:
        match = _PASSCODE_RE.search(parent.get_text(" ", strip=True))
        if match:
            return _clean_passcode(match.group(1))

    match = _PASSCODE_RE.search(soup.get_text(" ", strip=True))
    if match:
        return _clean_passcode(match.group(1))
    return None


def _clean_passcode(raw: str) -> str:
    return _TRAILING_PUNCT_RE.sub("", raw)


# --------------------------------------------------------------------------
# URL classification -- deterministic, no network. Case-insensitive host
# matching throughout (urlparse already lowercases `.hostname`).
# --------------------------------------------------------------------------


def classify_url(raw_url: str) -> _Candidate | None:
    """Public as of M2.6a: api/media.py's manual-add endpoint reuses this
    exact classifier for a user-pasted or channel-expanded URL rather than
    duplicating the platform rules here."""
    raw_url = raw_url.strip()
    if not raw_url:
        return None

    parsed = urlparse(raw_url)
    if not parsed.scheme or not parsed.netloc:
        return None  # not a fully-qualified URL -- nothing to record verbatim
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""

    if _is_mediasite_path(path):
        return _Candidate(platform="mediasite", url=_strip_fragment(raw_url))

    if _is_zoom_host(host) and _is_zoom_path(path):
        return _Candidate(
            platform="zoom", url=_strip_fragment(raw_url), passcode=_query_value(parsed.query, "pwd")
        )

    gdrive_url = _classify_gdrive(host, path, parsed.query)
    if gdrive_url is not None:
        return _Candidate(platform="gdrive", url=gdrive_url)

    return None


def _is_mediasite_path(path: str) -> bool:
    lowered = path.lower()
    return any(marker in lowered for marker in _MEDIASITE_PATH_MARKERS)


def _is_zoom_host(host: str) -> bool:
    return host == "zoom.us" or host.endswith(".zoom.us")


def _is_zoom_path(path: str) -> bool:
    return path.lower().startswith(_ZOOM_PATH_PREFIXES)


def _classify_gdrive(host: str, path: str, query: str) -> str | None:
    """Every Drive form this task supports, normalized to one canonical
    `https://drive.google.com/file/d/<id>/view` -- see the module docstring.
    Folders are explicitly out of scope (`/drive/folders/`)."""
    if host == "drive.google.com":
        if path.startswith("/drive/folders/"):
            return None
        match = re.match(r"^/file/d/([^/]+)", path)
        if match:
            return _gdrive_view_url(match.group(1))
        if path == "/uc" or path.startswith("/uc/"):
            file_id = _query_value(query, "id")
            if file_id:
                return _gdrive_view_url(file_id)
        return None

    if host == "drive.usercontent.google.com":
        file_id = _query_value(query, "id")
        if file_id:
            return _gdrive_view_url(file_id)

    return None


def _gdrive_view_url(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"


def _query_value(query: str, key: str) -> str | None:
    values = parse_qs(query).get(key)
    return values[0] if values else None


def _strip_fragment(url: str) -> str:
    scheme, netloc, path, query, _fragment = urlsplit(url)
    return urlunsplit((scheme, netloc, path, query, ""))


# --------------------------------------------------------------------------
# Upsert -- UNIQUE(course_id, url). Re-detection must never disturb a row's
# downstream (fetch/transcribe) progress.
# --------------------------------------------------------------------------


def _upsert(session: Session, course_id: int, material_id: int, candidate: _Candidate) -> bool:
    """Insert `candidate` as a new row, or fold it into an existing one keyed
    on (course_id, url). Returns True iff a new row was inserted.

    On an existing row: `status`/`error`/`transcript_material_id` are never
    touched -- those belong to whatever fetch/transcribe progress a later
    task has made, and re-detection must not reset it. `passcode` is filled
    in only if it was NULL (a first detection with no text-derived passcode
    followed by a later one that has one, or vice versa, must not clobber a
    value already captured). `updated_at` only moves when something actually
    changed, so a no-op re-detection leaves the row byte-for-byte identical.
    """
    existing = session.execute(
        select(MediaSource).where(MediaSource.course_id == course_id, MediaSource.url == candidate.url)
    ).scalar_one_or_none()

    if existing is None:
        now = _now_iso()
        session.add(
            MediaSource(
                course_id=course_id,
                material_id=material_id,
                platform=candidate.platform,
                url=candidate.url,
                passcode=candidate.passcode,
                created_at=now,
                updated_at=now,
            )
        )
        return True

    if existing.passcode is None and candidate.passcode is not None:
        existing.passcode = candidate.passcode
        existing.updated_at = _now_iso()

    return False
