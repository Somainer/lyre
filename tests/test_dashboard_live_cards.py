"""Live wakeup cards: the incremental transcript folder and the card
assembly that feed the dashboard's streaming view.

The folder is the per-wakeup fold of transcript rows the broadcaster
maintains BETWEEN poll ticks — these tests pin the incremental contract
(open delta runs survive ingest boundaries, close on non-delta rows, and
surface as streaming=True in snapshots) that the batch tail never needed."""

from __future__ import annotations

from pathlib import Path

import pytest

from lyre.dashboard.activity import (
    LiveTranscriptFolder,
    build_live_cards,
)
from lyre.persistence.db import init_db
from lyre.persistence.models import Persona, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.transcript import TranscriptWriter


def _folder() -> LiveTranscriptFolder:
    return LiveTranscriptFolder(
        wakeup_id="w1", persona="worker", task_id="t1",
        started_at="2026-06-11T00:00:00.000Z",
    )


def test_open_run_grows_across_ingests_and_streams() -> None:
    f = _folder()
    f.ingest([{"type": "content_delta", "text": "Hello "}])
    snap1 = f.snapshot()
    assert [e.kind for e in snap1] == ["assistant_text"]
    assert snap1[0].detail["streaming"] is True
    assert snap1[0].detail["text"] == "Hello"

    # Next poll tick: the run keeps growing — same single event, longer.
    f.ingest([{"type": "content_delta", "text": "world"}])
    snap2 = f.snapshot()
    assert len(snap2) == 1
    assert snap2[0].detail["text"] == "Hello world"
    assert snap2[0].detail["streaming"] is True


def test_non_delta_row_closes_the_run() -> None:
    f = _folder()
    f.ingest([
        {"type": "content_delta", "text": "done thinking"},
        {"type": "tool_use", "id": "x", "name": "python_exec",
         "input": {"code": "print(1)"}},
    ])
    snap = f.snapshot()
    assert [e.kind for e in snap] == ["assistant_text", "tool_use"]
    # Closed run is a finished bubble, not a streaming one.
    assert snap[0].detail["streaming"] is False


def test_open_buffer_is_clipped_keeping_the_tail() -> None:
    f = LiveTranscriptFolder(
        wakeup_id="w1", persona="worker", task_id="t1",
        started_at="2026-06-11T00:00:00.000Z", max_open_chars=50,
    )
    f.ingest([{"type": "thinking_delta", "text": "x" * 40}])
    f.ingest([{"type": "thinking_delta", "text": "TAIL-" + "y" * 40}])
    snap = f.snapshot()
    text = snap[0].detail["text"]
    assert len(text) <= 52  # cap + the "…" marker
    assert text.endswith("y" * 10)  # the tail (being typed) is what's kept
    assert text.startswith("…")


@pytest.mark.asyncio
async def test_build_live_cards_falls_back_to_file_tail(
    tmp_path: Path,
) -> None:
    """Without broadcaster folders (first page render after startup /
    standalone embedding), cards come from a bounded file tail at the
    path DERIVED from the wakeup id — transcript_uri is NULL for active
    wakeups, the exact gap that blinded the old dashboard."""
    conn = await init_db(tmp_path / "lyre.db")
    repos = SqliteRepositories(conn)
    try:
        await repos.personas.upsert(
            Persona(name="worker", role_description="w", system_prompt="w")
        )
        tid = await repos.tasks.create(
            TaskSpec(persona_name="worker", goal="g", acceptance="a")
        )
        wid = await repos.wakeups.start(tid, "worker")
        root = tmp_path / "objstore"
        writer = TranscriptWriter(root, wid)
        writer.write_delta("streaming text")
        writer.write_tool_use("t1", "shell_exec", {"argv": ["ls"]})

        cards = await build_live_cards(repos, root, live_folders=None)
        assert len(cards) == 1
        assert cards[0].wakeup_id == wid
        kinds = [e.kind for e in cards[0].events]
        assert "assistant_text" in kinds and "tool_use" in kinds
        writer.close()
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_build_live_cards_prefers_folder_and_scopes_to_agent(
    tmp_path: Path,
) -> None:
    conn = await init_db(tmp_path / "lyre.db")
    repos = SqliteRepositories(conn)
    try:
        await repos.personas.upsert(
            Persona(name="worker", role_description="w", system_prompt="w")
        )
        await repos.agents.create(agent_id="worker-a", persona_name="worker")
        await repos.agents.create(agent_id="worker-b", persona_name="worker")
        tid_a = await repos.tasks.create(
            TaskSpec(agent_id="worker-a", goal="g", acceptance="a")
        )
        tid_b = await repos.tasks.create(
            TaskSpec(agent_id="worker-b", goal="g", acceptance="a")
        )
        wid_a = await repos.wakeups.start(tid_a, "worker", agent_id="worker-a")
        await repos.wakeups.start(tid_b, "worker", agent_id="worker-b")

        folder = LiveTranscriptFolder(
            wakeup_id=wid_a, persona="worker", task_id=tid_a,
            started_at="2026-06-11T00:00:00.000Z",
        )
        folder.ingest([{"type": "content_delta", "text": "from folder"}])

        # Agent scoping: only worker-a's wakeup; folder content wins
        # over (absent) file tail with no file I/O needed.
        cards = await build_live_cards(
            repos, tmp_path / "objstore", live_folders={wid_a: folder},
            agent_id="worker-a",
        )
        assert [c.wakeup_id for c in cards] == [wid_a]
        assert cards[0].events[0].detail["text"] == "from folder"
    finally:
        await conn.close()
