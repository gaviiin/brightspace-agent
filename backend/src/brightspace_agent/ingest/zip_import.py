"""Manual zip-import fallback (deferred from Task 3; built in Task 13): for
a course that can't be synced from the extension, walk an uploaded zip's
tree straight into modules and materials.

Rules (see the Task 13 brief):
- Every directory (at any depth) becomes a module; a file's parent
  directory chain is walked even if the zip has no explicit entry for it
  (most zip tools don't write one for a non-empty directory).
- Sibling modules are ordered by directory name (`sort_order`).
- Every file becomes a material: `kind` via the same `infer_kind` the ToC
  path uses, `title` is the filename, `source_url` is `zip:{path}` (the
  identity a re-upload of the same zip resolves back to -- see
  `repo.upsert_zip_material`), bytes go through the same content-addressed
  blob store as everything else.
- Unsafe paths (zip-slip: absolute paths, `..` traversal, a Windows drive
  letter) and oversized entries (> `MAX_ENTRY_SIZE`) are skipped, each
  recorded as a `{"path", "message"}` entry in the returned stats rather
  than aborting the whole import. Common macOS zip noise (`__MACOSX/`,
  `.DS_Store`) is dropped silently -- it's not a user error.
"""

from __future__ import annotations

import mimetypes
import re
import zipfile
from pathlib import PurePosixPath
from typing import BinaryIO

from sqlalchemy.orm import Session

from brightspace_agent.ingest import repo
from brightspace_agent.ingest.diff import infer_kind
from brightspace_agent.ingest.store import BlobStore

MAX_ENTRY_SIZE = 200 * 1024 * 1024  # 200MB

# Extensions extract.py actually knows how to parse -- mapped straight to
# their canonical MIME type so `Material.mime` (not the extension-less,
# sha256-named blob path on disk) is what drives format detection at S1.
# Anything else falls back to the stdlib's best guess, then to None.
_EXT_TO_MIME: dict[str, str] = {
    "pdf": "application/pdf",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "html": "text/html",
    "htm": "text/html",
    "vtt": "text/vtt",
    "srt": "application/x-subrip",
    "txt": "text/plain",
    "md": "text/markdown",
}

_DRIVE_LETTER_RE = re.compile(r"^[a-zA-Z]:")


def import_zip(session: Session, blob_store: BlobStore, course_id: int, fileobj: BinaryIO) -> dict:
    """Walk the zip in `fileobj` into modules/materials for `course_id`.

    Returns `{"modules": int, "files": int, "bytes": int, "errors": [...]}`
    -- `modules` counts distinct directories seen (created or already
    existing), `files`/`bytes` count entries actually imported, `errors`
    lists skipped entries. Raises `zipfile.BadZipFile` if `fileobj` isn't a
    valid zip.
    """
    with zipfile.ZipFile(fileobj) as zf:
        all_infos = zf.infolist()

        errors: list[dict] = []
        usable_infos: list[zipfile.ZipInfo] = []
        for info in all_infos:
            if _is_noise(info.filename):
                continue
            reason = _unsafe_reason(info.filename)
            if reason is not None:
                if not info.is_dir():
                    errors.append({"path": info.filename, "message": reason})
                continue
            usable_infos.append(info)

        dir_paths = _all_dir_paths(usable_infos)
        resolver = _ModuleResolver(session, course_id, dir_paths)

        files = 0
        total_bytes = 0
        for info in sorted(usable_infos, key=lambda i: i.filename):
            if info.is_dir():
                continue
            if info.file_size > MAX_ENTRY_SIZE:
                errors.append(
                    {
                        "path": info.filename,
                        "message": f"file too large ({info.file_size} bytes; max {MAX_ENTRY_SIZE})",
                    }
                )
                continue

            path = PurePosixPath(info.filename)
            data = zf.read(info)
            sha256, size = blob_store.put_bytes(data)
            title = path.name
            module_id = resolver.resolve(path.parent)

            repo.upsert_zip_material(
                session,
                course_id,
                module_id=module_id,
                source_url=f"zip:{path}",
                title=title,
                kind=infer_kind(title, str(path)),
                sha256=sha256,
                mime=_guess_mime(title),
                size_bytes=size,
            )
            files += 1
            total_bytes += size

        return {"modules": len(dir_paths), "files": files, "bytes": total_bytes, "errors": errors}


# --------------------------------------------------------------------------
# Path safety / noise filtering
# --------------------------------------------------------------------------


def _is_noise(filename: str) -> bool:
    """macOS zip artifacts nobody wants imported as course material."""
    if filename.startswith("__MACOSX/"):
        return True
    return PurePosixPath(filename).name == ".DS_Store"


def _unsafe_reason(filename: str) -> str | None:
    """None if `filename` is safe to extract under the zip's own root;
    otherwise a short reason it was skipped (zip-slip prevention)."""
    if filename.startswith("/"):
        return "absolute path"
    if _DRIVE_LETTER_RE.match(filename):
        return "absolute path"
    if ".." in PurePosixPath(filename).parts:
        return "path traversal"
    return None


def _guess_mime(filename: str) -> str | None:
    ext = PurePosixPath(filename).suffix.lstrip(".").lower()
    if ext in _EXT_TO_MIME:
        return _EXT_TO_MIME[ext]
    return mimetypes.guess_type(filename)[0]


# --------------------------------------------------------------------------
# Directory tree -> modules
# --------------------------------------------------------------------------


def _all_dir_paths(infos: list[zipfile.ZipInfo]) -> set[PurePosixPath]:
    """Every directory implied by `infos`: explicit directory entries, plus
    every file's ancestor chain (most zip tools don't write an explicit
    entry for a non-empty directory)."""
    dirs: set[PurePosixPath] = set()
    for info in infos:
        path = PurePosixPath(info.filename)
        if info.is_dir():
            if str(path) not in (".", ""):
                dirs.add(path)
            continue
        for ancestor in path.parents:
            if str(ancestor) not in (".", ""):
                dirs.add(ancestor)
    return dirs


class _ModuleResolver:
    """Resolves a directory path (e.g. `week2/labs`) to a `modules.id`,
    upserting ancestors first (memoized) so a deeply nested path only ever
    walks its uncached prefix. `sort_order` per sibling group (by directory
    name) is precomputed once from the full `dir_paths` set."""

    def __init__(self, session: Session, course_id: int, dir_paths: set[PurePosixPath]) -> None:
        self._session = session
        self._course_id = course_id
        self._cache: dict[str, int | None] = {}

        by_parent: dict[str | None, list[PurePosixPath]] = {}
        for path in dir_paths:
            by_parent.setdefault(self._key(path.parent), []).append(path)
        self._sort_order: dict[str, int] = {
            str(child): index
            for children in by_parent.values()
            for index, child in enumerate(sorted(children, key=lambda p: p.name))
        }

    @staticmethod
    def _key(path: PurePosixPath) -> str | None:
        s = str(path)
        return None if s in (".", "") else s

    def resolve(self, dir_path: PurePosixPath) -> int | None:
        key = self._key(dir_path)
        if key is None:
            return None
        if key in self._cache:
            return self._cache[key]

        parent_id = self.resolve(dir_path.parent)
        module = repo.upsert_zip_module(
            self._session,
            self._course_id,
            path=key,
            parent_id=parent_id,
            title=dir_path.name,
            sort_order=self._sort_order[key],
        )
        self._cache[key] = module.id
        return module.id
