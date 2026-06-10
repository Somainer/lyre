"""Read-side companion to ``runtime/transcript.py`` — incremental tailing of a
wakeup's transcript.jsonl.

Used by ``lyre tail`` and the dashboard live stream. Lives at the package root
(like ``fsutil``) rather than under ``runtime/`` because transcripts are
runtime-write-only — the runtime never reads them back (RUNTIME_CURRENT.md);
readers are observation-side tooling.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class TranscriptTailer:
    """Incremental reader for one transcript.jsonl file.

    Each ``poll()`` returns the events whose lines were COMPLETED since the
    last poll. The writer flushes one JSON line per stream event, so polling
    the file is the cross-process live channel (wakeups run in a subprocess
    by default — there is no in-memory path to their stream events).

    Bytes are buffered until a full line is available before decoding, so a
    multibyte UTF-8 character split across two reads never corrupts (the old
    ``lyre tail`` decoded each slice independently and mangled CJK text at
    chunk boundaries).
    """

    def __init__(self, path: Path):
        self.path = path
        self._offset = 0
        self._buf = b""

    def poll(self) -> list[dict[str, Any]]:
        """Return newly completed events. Missing file → ``[]`` (the writer
        may not have created it yet). Lines that fail to parse are skipped —
        same policy as ``lyre audit``. Blocking file I/O: callers on an event
        loop should wrap in ``asyncio.to_thread``."""
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size <= self._offset:
            return []
        with self.path.open("rb") as fp:
            fp.seek(self._offset)
            chunk = fp.read(size - self._offset)
        self._offset += len(chunk)
        self._buf += chunk
        events: list[dict[str, Any]] = []
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                evt = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if isinstance(evt, dict):
                events.append(evt)
        return events
