"""Content-addressed blob store: sha256-keyed binary blobs plus text sidecars.

Blobs live at `blobs_dir/<sha[:2]>/<sha>`. Writes go to a temp file first and
are moved into place with `os.replace` for atomicity, and are skipped
entirely if the destination already exists (dedupe).
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import BinaryIO

DEFAULT_CHUNK_SIZE = 1024 * 1024  # 1 MiB


class BlobStore:
    def __init__(self, blobs_dir: Path, text_dir: Path) -> None:
        self.blobs_dir = Path(blobs_dir)
        self.text_dir = Path(text_dir)
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        self.text_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, sha256: str) -> Path:
        return self.blobs_dir / sha256[:2] / sha256

    def exists(self, sha256: str) -> bool:
        return self.path_for(sha256).exists()

    def open(self, sha256: str) -> BinaryIO:
        return self.path_for(sha256).open("rb")

    def put_bytes(self, data: bytes) -> tuple[str, int]:
        """Store `data`, returning (sha256_hex, size_bytes). Dedupes on sha256."""
        sha256 = hashlib.sha256(data).hexdigest()
        dest = self.path_for(sha256)
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=dest.parent)
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                os.replace(tmp_path, dest)
            except BaseException:
                tmp_path.unlink(missing_ok=True)
                raise
        return sha256, len(data)

    def put_stream(
        self, fileobj: BinaryIO, chunk_size: int = DEFAULT_CHUNK_SIZE
    ) -> tuple[str, int]:
        """Hash+store `fileobj` without holding the whole file in memory.

        Reads in `chunk_size`-sized chunks, spooling to a temp file while
        hashing, then atomically moves the temp file into place once the
        final sha256 (and therefore destination path) is known. Dedupes on
        sha256, same as put_bytes.
        """
        hasher = hashlib.sha256()
        size = 0
        fd, tmp_name = tempfile.mkstemp(dir=self.blobs_dir)
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as f:
                while chunk := fileobj.read(chunk_size):
                    hasher.update(chunk)
                    size += len(chunk)
                    f.write(chunk)

            sha256 = hasher.hexdigest()
            dest = self.path_for(sha256)
            if dest.exists():
                tmp_path.unlink(missing_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(tmp_path, dest)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        return sha256, size

    def write_text(self, sha256: str, text: str) -> None:
        """Write the extracted-text sidecar for `sha256`, atomically."""
        dest = self.text_dir / f"{sha256}.txt"
        fd, tmp_name = tempfile.mkstemp(dir=self.text_dir)
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp_path, dest)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    def read_text(self, sha256: str) -> str | None:
        dest = self.text_dir / f"{sha256}.txt"
        if not dest.exists():
            return None
        return dest.read_text(encoding="utf-8")
