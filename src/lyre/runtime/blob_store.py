"""Content-addressed blob storage on disk.

Bytes for image/document attachments live at
``${object_store}/blobs/<sha256>.<ext>``. The DB row in the ``blobs``
table is just the metadata index — the filesystem path is derived from
the row's ``id`` (sha256 hex) and ``media_type`` (extension).

Trust boundary:

  * **New blobs** can only be produced by trusted entry points —
    today that's the dashboard upload route (owner action). A future
    PR may add tool-returned bytes (screenshot, file-read on image).
    Agents calling ``mailbox_send(attachments=[...])`` can ONLY
    reference existing ids; they cannot fabricate bytes via base64
    input, which would otherwise let a model burn vision tokens on
    arbitrary content without owner approval.
  * **Reads** are unrestricted within the runtime — anyone with a
    valid blob_id can ``load`` it, since once an agent sees a mail
    that references the blob it implicitly has access.

The on-disk format is deliberately filesystem-only (no DB blob columns)
so backups / object-store sync / `du` all work the obvious way, and
SQLite stays small + fast.
"""

from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path

# Extensions we know how to write — clamps the disk filename's suffix
# so we don't surprise filesystems with weird MIME-derived extensions.
# Anything not in here writes as `.bin` (still loadable; just a generic
# extension). Keep this list ASCII-stable; it ends up in user paths.
_EXTENSION_BY_MEDIA_TYPE: dict[str, str] = {
    "image/png":            "png",
    "image/jpeg":           "jpg",
    "image/jpg":            "jpg",
    "image/gif":            "gif",
    "image/webp":           "webp",
    "image/heic":           "heic",
    "image/heif":           "heif",
    "application/pdf":      "pdf",
}


def _extension_for(media_type: str) -> str:
    """Best-effort extension for `media_type`. Falls back to mimetypes
    stdlib for less common types, then to ``bin``."""
    if media_type in _EXTENSION_BY_MEDIA_TYPE:
        return _EXTENSION_BY_MEDIA_TYPE[media_type]
    guess = mimetypes.guess_extension(media_type)
    if guess:
        # mimetypes returns e.g. ".jpe" or ".pdf" — strip the dot.
        return guess.lstrip(".")
    return "bin"


class BlobStore:
    """Thin filesystem wrapper for the blob directory.

    Construction is a no-op; the directory is created lazily on first
    write so tests / read-only contexts don't need to mkdir.
    """

    def __init__(self, object_store_path: Path) -> None:
        self.root = Path(object_store_path) / "blobs"

    def path_for(self, blob_id: str, media_type: str) -> Path:
        """Where on disk this id lives. Does NOT touch the filesystem."""
        return self.root / f"{blob_id}.{_extension_for(media_type)}"

    def exists(self, blob_id: str, media_type: str) -> bool:
        return self.path_for(blob_id, media_type).exists()

    def write(self, data: bytes, media_type: str) -> str:
        """Hash, write (idempotent), return the blob_id.

        ``write(b'...', 'image/png')`` writes to
        ``blobs/<sha256>.png`` and returns the sha256 hex. If the same
        bytes are written twice the second call is a no-op (file
        already exists, content-addressed → identical).
        """
        blob_id = hashlib.sha256(data).hexdigest()
        path = self.path_for(blob_id, media_type)
        if not path.exists():
            self.root.mkdir(parents=True, exist_ok=True)
            # Write through a temp file + atomic rename so a partial
            # write under SIGKILL doesn't leave a half-written blob
            # that future reads would silently truncate. (Kill-test
            # principle: any process can die at any moment.)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        return blob_id

    def read(self, blob_id: str, media_type: str) -> bytes:
        """Read raw bytes. Raises FileNotFoundError if missing — the
        caller (adapter, dashboard route) should treat that as a hard
        error since a referenced-but-missing blob means inconsistency
        between DB metadata and the on-disk store."""
        return self.path_for(blob_id, media_type).read_bytes()
