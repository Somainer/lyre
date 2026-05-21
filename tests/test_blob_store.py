"""Tests for content-addressed blob storage on disk."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from lyre.runtime.blob_store import BlobStore


def test_write_returns_sha256_hex(tmp_path: Path) -> None:
    """The blob id is the lowercase sha256 hex of the bytes — that's
    the entire dedup contract. Anything else and identical uploads
    won't collide."""
    store = BlobStore(tmp_path)
    data = b"some image bytes"
    blob_id = store.write(data, "image/png")
    assert blob_id == hashlib.sha256(data).hexdigest()
    assert len(blob_id) == 64
    assert blob_id == blob_id.lower()


def test_write_then_read_round_trip(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    data = b"\x89PNG\r\n\x1a\n" + b"x" * 100
    blob_id = store.write(data, "image/png")
    assert store.read(blob_id, "image/png") == data


def test_write_is_idempotent_for_identical_bytes(tmp_path: Path) -> None:
    """Writing the same bytes twice must return the same id and not
    duplicate on disk."""
    store = BlobStore(tmp_path)
    data = b"identical"
    id1 = store.write(data, "image/png")
    id2 = store.write(data, "image/png")
    assert id1 == id2
    # Exactly one file on disk under that hash.
    matches = list(store.root.glob(f"{id1}.*"))
    assert len(matches) == 1


def test_extension_picked_by_media_type(tmp_path: Path) -> None:
    """Known media types get curated extensions; unknown falls back
    to a guess; everything else lands as .bin."""
    store = BlobStore(tmp_path)
    png_id = store.write(b"png-bytes", "image/png")
    jpg_id = store.write(b"jpg-bytes", "image/jpeg")
    pdf_id = store.write(b"pdf-bytes", "application/pdf")
    weird_id = store.write(b"weird", "application/x-unknown-z")

    assert store.path_for(png_id, "image/png").suffix == ".png"
    assert store.path_for(jpg_id, "image/jpeg").suffix == ".jpg"
    assert store.path_for(pdf_id, "application/pdf").suffix == ".pdf"
    assert store.path_for(weird_id, "application/x-unknown-z").suffix == ".bin"


def test_path_for_does_not_touch_filesystem(tmp_path: Path) -> None:
    """`path_for` is pure — it must NOT create the blobs/ directory or
    the file. Otherwise probing a non-existent blob has side effects."""
    store = BlobStore(tmp_path)
    p = store.path_for("a" * 64, "image/png")
    assert not p.parent.exists()
    assert not p.exists()


def test_read_missing_blob_raises_filenotfound(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.read("a" * 64, "image/png")


def test_write_creates_blobs_subdir_lazily(tmp_path: Path) -> None:
    """First write creates ``object_store/blobs/``; before any write
    the subdir doesn't exist (we don't want empty directories
    polluting fresh installs)."""
    store = BlobStore(tmp_path)
    assert not store.root.exists()
    store.write(b"data", "image/png")
    assert store.root.is_dir()


def test_write_atomic_under_sigkill_semantics(tmp_path: Path) -> None:
    """The atomic-rename pattern means an interrupted write leaves
    either a .tmp file OR the final file, never a truncated final
    file. Smoke-test that the tmp filename pattern is what we expect."""
    store = BlobStore(tmp_path)
    blob_id = store.write(b"safe", "image/png")
    final = store.path_for(blob_id, "image/png")
    assert final.exists()
    # No leftover .tmp after a successful write.
    leftover = final.with_suffix(final.suffix + ".tmp")
    assert not leftover.exists()
