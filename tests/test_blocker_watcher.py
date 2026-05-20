"""Unit tests for BlockerWatcher."""

from __future__ import annotations

import asyncio

import pytest

from lyre.persistence.models import MailboxMessage, Persona
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.blocker_watcher import (
    BlockerWatcher,
    format_interrupt_notice,
)


async def _seed_persona(repos: SqliteRepositories, name: str) -> None:
    await repos.personas.upsert(
        Persona(name=name, role_description="p", system_prompt="p")
    )
    await repos.mailbox.ensure_mailbox(name)


def _msg(recipient: str, body: str, urgency: str = "blocker", ext: str = "") -> MailboxMessage:
    return MailboxMessage(
        recipient=recipient,
        external_id=ext or f"e-{recipient}-{body[:8]}",
        sender="owner",
        urgency=urgency,  # type: ignore[arg-type]
        body=body,
    )


@pytest.mark.asyncio
async def test_watcher_raises_signal_on_new_blocker(
    repos: SqliteRepositories,
) -> None:
    await _seed_persona(repos, "dispatcher")
    watcher = BlockerWatcher(repos=repos, recipient="dispatcher", baseline_msg_id=0, poll_interval_s=0.05)
    await watcher.start()
    try:
        await repos.mailbox.insert_message(_msg("dispatcher", "STOP", ext="b1"))
        # Wait up to 1s for the watcher to notice.
        for _ in range(40):
            if watcher.signal.is_set():
                break
            await asyncio.sleep(0.05)
        assert watcher.signal.is_set()
        assert len(watcher.pending) == 1
        assert watcher.pending[0].body == "STOP"
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_watcher_ignores_messages_below_baseline(
    repos: SqliteRepositories,
) -> None:
    await _seed_persona(repos, "dispatcher")
    # Pre-insert a blocker BEFORE we start the watcher; set baseline to it.
    msg_id = await repos.mailbox.insert_message(_msg("dispatcher", "old", ext="b0"))

    watcher = BlockerWatcher(
        repos=repos, recipient="dispatcher",
        baseline_msg_id=msg_id, poll_interval_s=0.05,
    )
    await watcher.start()
    try:
        await asyncio.sleep(0.2)  # give the watcher time to poll
        assert not watcher.signal.is_set()
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_watcher_ignores_below_threshold_urgency(
    repos: SqliteRepositories,
) -> None:
    """With default min_urgency='high', normal/low don't fire. high+ does.
    (Renamed from the old "ignores_non_blocker" since the watcher now
    widened to high; only normal/low stay silent.)"""
    await _seed_persona(repos, "dispatcher")
    watcher = BlockerWatcher(repos=repos, recipient="dispatcher", baseline_msg_id=0, poll_interval_s=0.05)
    await watcher.start()
    try:
        await repos.mailbox.insert_message(
            _msg("dispatcher", "FYI", urgency="normal", ext="n1")
        )
        await repos.mailbox.insert_message(
            _msg("dispatcher", "archive", urgency="low", ext="l1")
        )
        await asyncio.sleep(0.2)
        assert not watcher.signal.is_set()
        # Now insert a high — MUST fire (no mid-stream privilege, but the
        # watcher signal still goes up so agent_loop's turn boundary picks it up)
        await repos.mailbox.insert_message(
            _msg("dispatcher", "please reply", urgency="high", ext="h1")
        )
        for _ in range(40):
            if watcher.signal.is_set():
                break
            await asyncio.sleep(0.05)
        assert watcher.signal.is_set()
        # The pending list contains the high message, NO blocker present.
        assert any(m.urgency == "high" for m in watcher.pending)
        assert not watcher.has_blocker_pending
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_acknowledge_clears_signal_and_drains_pending(
    repos: SqliteRepositories,
) -> None:
    await _seed_persona(repos, "dispatcher")
    watcher = BlockerWatcher(repos=repos, recipient="dispatcher", baseline_msg_id=0, poll_interval_s=0.05)
    await watcher.start()
    try:
        await repos.mailbox.insert_message(_msg("dispatcher", "STOP", ext="b1"))
        for _ in range(40):
            if watcher.signal.is_set():
                break
            await asyncio.sleep(0.05)
        msgs = watcher.acknowledge()
        assert len(msgs) == 1
        assert watcher.pending == []
        assert not watcher.signal.is_set()
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_signal_re_raises_only_for_NEW_blockers_after_ack(
    repos: SqliteRepositories,
) -> None:
    """After acknowledge(), the watcher's in-memory baseline advances so the
    same blocker isn't shown again. A second blocker with higher id MUST
    still fire."""
    await _seed_persona(repos, "dispatcher")
    watcher = BlockerWatcher(repos=repos, recipient="dispatcher", baseline_msg_id=0, poll_interval_s=0.05)
    await watcher.start()
    try:
        await repos.mailbox.insert_message(_msg("dispatcher", "first", ext="b1"))
        for _ in range(40):
            if watcher.signal.is_set():
                break
            await asyncio.sleep(0.05)
        watcher.acknowledge()
        # Quiet period: no NEW blockers above the advanced baseline.
        await asyncio.sleep(0.2)
        assert not watcher.signal.is_set()
        assert watcher.pending == []

        # 2nd blocker (higher id) MUST raise the signal again.
        await repos.mailbox.insert_message(_msg("dispatcher", "second", ext="b2"))
        for _ in range(40):
            if watcher.signal.is_set():
                break
            await asyncio.sleep(0.05)
        assert watcher.signal.is_set()
        assert {m.body for m in watcher.pending} == {"second"}
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_stop_is_idempotent_and_kills_task(
    repos: SqliteRepositories,
) -> None:
    await _seed_persona(repos, "dispatcher")
    watcher = BlockerWatcher(repos=repos, recipient="dispatcher", baseline_msg_id=0, poll_interval_s=0.05)
    await watcher.start()
    await watcher.stop()
    await watcher.stop()  # second stop is a no-op
    assert watcher._task is None


def test_format_interrupt_notice_renders_preview() -> None:
    msgs = [
        MailboxMessage(
            id=42, recipient="r", external_id="e", sender="owner",
            urgency="blocker", body="STOP and reconsider X",
        ),
        MailboxMessage(
            id=43, recipient="r", external_id="e2", sender="owner",
            urgency="blocker", body="also Y",
        ),
    ]
    notice = format_interrupt_notice(msgs)
    assert "INTERRUPT" in notice
    assert "Message count: 2" in notice  # renamed from "Blocker count" in MailWatcher rewrite
    assert "[id=42] urgency=blocker from owner: STOP and reconsider X" in notice
    assert "also Y" in notice


def test_format_interrupt_notice_caps_preview_at_five() -> None:
    msgs = [
        MailboxMessage(
            id=i, recipient="r", external_id=f"e{i}", sender="owner",
            urgency="blocker", body=f"msg {i}",
        )
        for i in range(8)
    ]
    notice = format_interrupt_notice(msgs)
    assert "and 3 more" in notice
