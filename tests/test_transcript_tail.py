"""TranscriptTailer: the read-side companion to TranscriptWriter, used by
``lyre tail`` and the dashboard live stream. The contract under test: poll()
returns each event exactly once, survives partial lines and multibyte UTF-8
split across reads, and the reader derives the same path the writer uses —
``wakeups.transcript_uri`` is NULL while a wakeup is running, so live readers
cannot rely on it (the bug that made ``lyre tail`` crash on active wakeups)."""

from __future__ import annotations

from pathlib import Path

from lyre.runtime.transcript import TranscriptWriter, transcript_path
from lyre.transcript_tail import TranscriptTailer


def test_reader_path_matches_writer_path(tmp_path: Path) -> None:
    writer = TranscriptWriter(tmp_path, "wakeup-ssot")
    assert transcript_path(tmp_path, "wakeup-ssot") == writer.path
    writer.close()


def test_poll_returns_each_event_exactly_once(tmp_path: Path) -> None:
    writer = TranscriptWriter(tmp_path, "w1")
    tailer = TranscriptTailer(transcript_path(tmp_path, "w1"))

    writer.write_delta("hello")
    writer.write_tool_use("t1", "python_exec", {"code": "print(1)"})
    first = tailer.poll()
    assert [e["type"] for e in first] == ["content_delta", "tool_use"]

    # Nothing new → empty, not a re-read.
    assert tailer.poll() == []

    writer.write_delta("world")
    second = tailer.poll()
    assert [e["text"] for e in second] == ["world"]
    writer.close()


def test_partial_trailing_line_is_buffered_until_complete(tmp_path: Path) -> None:
    path = tmp_path / "transcript.jsonl"
    tailer = TranscriptTailer(path)

    with path.open("ab") as fp:
        fp.write(b'{"type": "content_delta", "text": "a"}\n{"type": "no')
    events = tailer.poll()
    assert len(events) == 1  # the half-written second line must NOT surface

    with path.open("ab") as fp:
        fp.write(b'te", "text": "done"}\n')
    events = tailer.poll()
    assert events == [{"type": "note", "text": "done"}]


def test_multibyte_utf8_split_across_polls_does_not_corrupt(
    tmp_path: Path,
) -> None:
    """The old ``lyre tail`` decoded each byte slice independently, so a CJK
    character split across two reads decoded as U+FFFD. The tailer buffers
    bytes until the line completes."""
    path = tmp_path / "transcript.jsonl"
    tailer = TranscriptTailer(path)
    line = '{"type": "content_delta", "text": "中文流式输出"}\n'.encode()
    # Split INSIDE the first multibyte character: one past its lead byte.
    split = next(i for i, b in enumerate(line) if b >= 0x80) + 1
    assert 0x80 <= line[split] <= 0xBF  # continuation byte → mid-character

    with path.open("ab") as fp:
        fp.write(line[:split])
    assert tailer.poll() == []

    with path.open("ab") as fp:
        fp.write(line[split:])
    events = tailer.poll()
    assert events[0]["text"] == "中文流式输出"


def test_missing_file_and_corrupt_lines(tmp_path: Path) -> None:
    tailer = TranscriptTailer(tmp_path / "not-yet-created.jsonl")
    assert tailer.poll() == []  # writer hasn't created the file yet

    path = tmp_path / "not-yet-created.jsonl"
    with path.open("ab") as fp:
        fp.write(b'this is not json\n{"type": "note", "text": "ok"}\n42\n')
    events = tailer.poll()
    # Corrupt line skipped, non-dict JSON (42) skipped — audit's policy.
    assert events == [{"type": "note", "text": "ok"}]
