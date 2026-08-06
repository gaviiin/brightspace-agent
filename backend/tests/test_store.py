"""Tests for the content-addressed blob store."""

import hashlib
import io
import os

import pytest

from brightspace_agent.ingest.store import BlobStore


@pytest.fixture
def store(tmp_path):
    return BlobStore(blobs_dir=tmp_path / "blobs", text_dir=tmp_path / "text")


class _CountingReader:
    """File-like wrapper that counts .read() calls, to prove chunked reads."""

    def __init__(self, data: bytes) -> None:
        self._buf = io.BytesIO(data)
        self.read_calls = 0

    def read(self, size=-1):
        self.read_calls += 1
        return self._buf.read(size)


def test_put_bytes_returns_correct_sha256_and_lands_at_sharded_path(store):
    data = b"hello brightspace agent"
    expected_sha = hashlib.sha256(data).hexdigest()

    sha256, size = store.put_bytes(data)

    assert sha256 == expected_sha
    assert size == len(data)
    expected_path = store.blobs_dir / expected_sha[:2] / expected_sha
    assert expected_path.exists()
    assert expected_path.read_bytes() == data


def test_put_bytes_dedupes_identical_content(store):
    data = b"duplicate me please"

    sha_first, _ = store.put_bytes(data)
    shard_dir = store.blobs_dir / sha_first[:2]
    files_after_first = sorted(p.name for p in shard_dir.iterdir())

    sha_second, size_second = store.put_bytes(data)
    files_after_second = sorted(p.name for p in shard_dir.iterdir())

    assert sha_second == sha_first
    assert size_second == len(data)
    assert files_after_second == files_after_first
    assert len(files_after_second) == 1


def test_put_stream_matches_hashlib_and_reads_in_bounded_chunks(store):
    data = os.urandom(3 * 1024 * 1024)  # 3 MiB: forces multiple 1 MiB chunk reads
    expected_sha = hashlib.sha256(data).hexdigest()
    reader = _CountingReader(data)

    sha256, size = store.put_stream(reader)

    assert sha256 == expected_sha
    assert size == len(data)
    assert reader.read_calls >= 3  # proves the store read in chunks, not one big blob
    assert store.path_for(sha256).exists()
    assert store.path_for(sha256).read_bytes() == data


def test_write_text_read_text_roundtrip_and_unknown_sha_returns_none(store):
    sha256, _ = store.put_bytes(b"some material bytes")

    assert store.read_text(sha256) is None  # no sidecar written yet

    store.write_text(sha256, "extracted text content")
    assert store.read_text(sha256) == "extracted text content"

    assert store.read_text("0" * 64) is None
