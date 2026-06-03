"""T1: the wakeup PUSHES the agent's own state in (scratchpad + recent sends).

Wakeups are stateless; the RCA (019e8d7d) showed a model can't be trusted to
pull its own scratchpad / sent mail. assemble_initial_user_message now injects
them directly — bounded, and only when the sources are supplied (back-compat).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lyre.persistence.models import MailboxMessage, Task
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.context import assemble_initial_user_message


def _task() -> Task:
    return Task(
        id="t1", persona_name="dispatcher", goal="do the thing",
        acceptance="it is done", status="in_progress",
    )


def _text(msg) -> str:  # noqa: ANN001
    return msg.content[0].text


@pytest.mark.asyncio
async def test_scratchpad_content_is_pushed_in(tmp_path: Path) -> None:
    memory_root = tmp_path / "memory"
    (memory_root / "scratchpad").mkdir(parents=True)
    (memory_root / "scratchpad" / "dispatcher.md").write_text(
        "- promised owner: ship X by 18:00\n", encoding="utf-8"
    )
    msg = await assemble_initial_user_message(
        _task(), agent_id="dispatcher", memory_root=memory_root
    )
    text = _text(msg)
    assert "promised owner: ship X by 18:00" in text  # content, not a pointer
    assert "scratchpad" in text


@pytest.mark.asyncio
async def test_scratchpad_flat_id_for_slashed_agent(tmp_path: Path) -> None:
    memory_root = tmp_path / "memory"
    (memory_root / "scratchpad").mkdir(parents=True)
    # `analyst/auth` → `analyst-auth.md`, matching the preamble convention.
    (memory_root / "scratchpad" / "analyst-auth.md").write_text(
        "MARKER-123\n", encoding="utf-8"
    )
    msg = await assemble_initial_user_message(
        _task(), agent_id="analyst/auth", memory_root=memory_root
    )
    assert "MARKER-123" in _text(msg)


@pytest.mark.asyncio
async def test_scratchpad_is_capped(tmp_path: Path) -> None:
    memory_root = tmp_path / "memory"
    (memory_root / "scratchpad").mkdir(parents=True)
    (memory_root / "scratchpad" / "dispatcher.md").write_text(
        "x" * 10_000, encoding="utf-8"
    )
    msg = await assemble_initial_user_message(
        _task(), agent_id="dispatcher", memory_root=memory_root
    )
    text = _text(msg)
    assert "truncated" in text
    assert text.count("x") < 6000  # bounded well under the 10k written


@pytest.mark.asyncio
async def test_recent_sent_mail_is_pushed_in(repos: SqliteRepositories) -> None:
    await repos.mailbox.ensure_mailbox("owner")
    await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="owner", external_id="x1", sender="dispatcher",
            urgency="normal", body="...", title="status: X dispatched",
        )
    )
    msg = await assemble_initial_user_message(
        _task(), mailbox_repo=repos.mailbox, agent_id="dispatcher"
    )
    text = _text(msg)
    assert "status: X dispatched" in text  # so it won't re-send / forget
    assert "→ owner" in text


@pytest.mark.asyncio
async def test_no_push_when_sources_absent() -> None:
    # Back-compat: with no memory_root / mailbox_repo, neither section appears.
    msg = await assemble_initial_user_message(_task())
    text = _text(msg)
    assert "你的 scratchpad" not in text
    assert "最近发出的信" not in text
    assert "do the thing" in text  # goal still there
