"""Shared filesystem primitives that uphold the kill-test law.

One canonical atomic text write. Three modules used to carry private
copies of this pattern (wakeup_summary, fs_personas) while a fourth
(introspect's update_scratchpad) forgot it entirely and truncated a
durability-tier file on SIGKILL — exactly the divergence a single
helper prevents.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a same-dir temp file + os.replace.

    A SIGKILL (or ENOSPC) at any instant leaves either the prior complete
    file or the new complete file — never a truncated/empty one. fsync
    before replace also covers power loss, not just process death: rename
    durability without flushed data blocks can surface a zero-length file
    after a crash on some filesystems.
    """
    fd, tmp = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
