"""RB-3: per-agent notes rotation → cold-archive.

Verifies `wakeup_summary._maybe_rotate_notes`:

  - over-threshold `## Auto-summary log` → oldest entries move to
    `object_store/notes_archive/agent-<id>.md`; hot file keeps newest half
    + a pointer; the hand-written region above the log header is untouched
  - disabled (max_entries<=0 / no object store) → no-op
  - kill-safe / idempotent: re-archiving the same overflow (the crash-between
    -archive-and-hot-rewrite case) never duplicates an entry in the archive
"""

from __future__ import annotations

from pathlib import Path

from lyre.runtime.wakeup_summary import (
    SUMMARY_SECTION_HEADER,
    _append_to_notes,
    _maybe_rotate_notes,
)

AGENT = "worker-maintainer-backend-1"


def _notes_file(memory: Path) -> Path:
    return memory / "facts" / f"agent-{AGENT}-notes.md"


def _seed_notes(memory: Path, n: int, *, handwritten: str = "") -> Path:
    """Build a notes file with `n` auto-summary entries (wakeup ids
    aaaa0001..aaaa000N, oldest appended first so newest ends up on top)."""
    (memory / "facts").mkdir(parents=True, exist_ok=True)
    notes = _notes_file(memory)
    if handwritten:
        notes.write_text(handwritten, encoding="utf-8")
    for i in range(1, n + 1):
        _append_to_notes(
            memory_path=memory,
            agent_id=AGENT,
            wakeup_id=f"aaaa{i:04d}",
            summary=f"- SUMMARY-w{i}",
        )
    return notes


def _archive_file(object_store: Path) -> Path:
    return object_store / "notes_archive" / f"agent-{AGENT}.md"


def test_rotation_moves_oldest_entries_to_cold_archive(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    object_store = tmp_path / "object_store"
    notes = _seed_notes(memory, 6)

    _maybe_rotate_notes(
        memory_path=memory,
        object_store_path=object_store,
        agent_id=AGENT,
        max_entries=4,
    )

    hot = notes.read_text(encoding="utf-8")
    archive = _archive_file(object_store).read_text(encoding="utf-8")

    # keep_recent = max_entries // 2 = 2 → newest two stay hot.
    assert "SUMMARY-w6" in hot and "SUMMARY-w5" in hot
    for old in ("w4", "w3", "w2", "w1"):
        assert f"SUMMARY-{old}" not in hot, f"{old} should have rotated out"
        assert f"SUMMARY-{old}" in archive, f"{old} should be archived"
    # A pointer to the archive is left behind in the hot file.
    assert "notes_archive/" in hot
    # The log header itself survives.
    assert SUMMARY_SECTION_HEADER in hot


def test_rotation_archives_oldest_first_chronological(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    object_store = tmp_path / "object_store"
    _seed_notes(memory, 6)
    _maybe_rotate_notes(
        memory_path=memory, object_store_path=object_store,
        agent_id=AGENT, max_entries=4,
    )
    archive = _archive_file(object_store).read_text(encoding="utf-8")
    # Archive reads oldest→newest (the reverse of the newest-first hot log).
    assert (
        archive.index("SUMMARY-w1")
        < archive.index("SUMMARY-w2")
        < archive.index("SUMMARY-w3")
        < archive.index("SUMMARY-w4")
    )


def test_rotation_preserves_handwritten_region(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    object_store = tmp_path / "object_store"
    handwritten = (
        "# Notes for backend-1\n\n"
        "## Owner preferences\n"
        "- prefers terse status updates\n"
        "- repo lives at git@github.com:somainer/lyre.git\n\n"
    )
    notes = _seed_notes(memory, 6, handwritten=handwritten)
    _maybe_rotate_notes(
        memory_path=memory, object_store_path=object_store,
        agent_id=AGENT, max_entries=4,
    )
    hot = notes.read_text(encoding="utf-8")
    # Every hand-written line is byte-preserved, and stays ABOVE the log header.
    assert "prefers terse status updates" in hot
    assert "git@github.com:somainer/lyre.git" in hot
    assert hot.index("Owner preferences") < hot.index(SUMMARY_SECTION_HEADER)


def test_no_rotation_below_threshold(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    object_store = tmp_path / "object_store"
    notes = _seed_notes(memory, 3)
    before = notes.read_text(encoding="utf-8")
    _maybe_rotate_notes(
        memory_path=memory, object_store_path=object_store,
        agent_id=AGENT, max_entries=4,
    )
    assert notes.read_text(encoding="utf-8") == before
    assert not _archive_file(object_store).exists()


def test_rotation_disabled_when_max_entries_zero(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    object_store = tmp_path / "object_store"
    notes = _seed_notes(memory, 10)
    before = notes.read_text(encoding="utf-8")
    _maybe_rotate_notes(
        memory_path=memory, object_store_path=object_store,
        agent_id=AGENT, max_entries=0,
    )
    assert notes.read_text(encoding="utf-8") == before
    assert not (object_store / "notes_archive").exists()


def test_rotation_noop_without_object_store(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    notes = _seed_notes(memory, 10)
    before = notes.read_text(encoding="utf-8")
    _maybe_rotate_notes(
        memory_path=memory, object_store_path=None,
        agent_id=AGENT, max_entries=4,
    )
    assert notes.read_text(encoding="utf-8") == before


def test_rotation_dedups_already_archived_entries(tmp_path: Path) -> None:
    """Kill-safety: archive-then-rewrite means a crash before the hot rewrite
    re-archives the same overflow next time. The dedup-by-wakeup-id must keep
    each entry in the archive exactly once (at-least-once, not at-least-twice)."""
    memory = tmp_path / "memory"
    object_store = tmp_path / "object_store"
    _seed_notes(memory, 6)

    # Simulate a prior crashed rotation: w1 & w2 already in the archive, but
    # the hot file was never trimmed (still holds all 6 entries).
    archive = _archive_file(object_store)
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_text(
        "### 2026-06-04T12:00:00Z · wakeup aaaa0001\n- SUMMARY-w1\n"
        "### 2026-06-04T12:00:00Z · wakeup aaaa0002\n- SUMMARY-w2\n",
        encoding="utf-8",
    )

    _maybe_rotate_notes(
        memory_path=memory, object_store_path=object_store,
        agent_id=AGENT, max_entries=4,
    )

    text = archive.read_text(encoding="utf-8")
    # w1 / w2 appear exactly once (not re-appended); w3 / w4 newly archived.
    assert text.count("wakeup aaaa0001") == 1
    assert text.count("wakeup aaaa0002") == 1
    assert "SUMMARY-w3" in text and "SUMMARY-w4" in text


def test_repeated_rotation_keeps_archive_growing_without_loss(
    tmp_path: Path,
) -> None:
    """Two rotation rounds with fresh entries in between: every entry ends up
    either hot or archived exactly once — nothing is lost across rounds."""
    memory = tmp_path / "memory"
    object_store = tmp_path / "object_store"
    _seed_notes(memory, 6)
    _maybe_rotate_notes(
        memory_path=memory, object_store_path=object_store,
        agent_id=AGENT, max_entries=4,
    )
    # Add three more wakeups, pushing the section back over the ceiling.
    for i in range(7, 10):
        _append_to_notes(
            memory_path=memory, agent_id=AGENT,
            wakeup_id=f"aaaa{i:04d}", summary=f"- SUMMARY-w{i}",
        )
    _maybe_rotate_notes(
        memory_path=memory, object_store_path=object_store,
        agent_id=AGENT, max_entries=4,
    )

    hot = _notes_file(memory).read_text(encoding="utf-8")
    archive = _archive_file(object_store).read_text(encoding="utf-8")
    for i in range(1, 10):
        present = (f"SUMMARY-w{i}" in hot) + (f"SUMMARY-w{i}" in archive)
        assert present == 1, f"w{i} should be in exactly one of hot/archive"
