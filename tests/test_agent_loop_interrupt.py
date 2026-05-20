"""Tests for mid-stream + turn-boundary blocker interrupt in AgentLoop."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from lyre.adapter.llm_adapter import (
    ContentDelta,
    LyreContentBlock,
    LyreMessage,
    LyreToolSpec,
    StreamEvent,
    TurnComplete,
)
from lyre.persistence.models import MailboxMessage, Persona
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.agent_loop import AgentLoop
from lyre.runtime.blocker_watcher import BlockerWatcher
from lyre.runtime.transcript import TranscriptWriter

from .fake_adapter import FakeAdapter
from .helpers import fake_entry


class _SlowAdapter:
    """Yield each event with an asyncio.sleep gap so the test has time to
    raise an interrupt mid-stream."""

    def __init__(self, events_per_turn: list[list[StreamEvent]], gap_s: float = 0.05):
        self._turns = list(events_per_turn)
        self.gap_s = gap_s
        self.calls = 0

    async def stream_turn(
        self,
        messages: list[LyreMessage],
        tools: list[LyreToolSpec],
        model: str,
        max_tokens: int = 4096,
        temperature: float | None = None,
        system: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        events = self._turns.pop(0) if self._turns else [TurnComplete(stop_reason="end_turn")]
        for evt in events:
            await asyncio.sleep(self.gap_s)
            yield evt


def _user(text: str) -> LyreMessage:
    return LyreMessage(role="user", content=[LyreContentBlock(type="text", text=text)])


@pytest.mark.asyncio
async def test_turn_boundary_interrupt_injects_notice(
    repos: SqliteRepositories, object_store: Path
) -> None:
    """Blocker arrives between turns. The pre-turn check should inject a user
    notice before the next stream call."""
    await repos.personas.upsert(
        Persona(name="leader", role_description="l", system_prompt="l")
    )
    await repos.mailbox.ensure_mailbox("leader")
    watcher = BlockerWatcher(
        repos=repos, recipient="leader", baseline_msg_id=0, poll_interval_s=0.02
    )
    await watcher.start()

    adapter = FakeAdapter()
    # Turn 1: produces text, ends. Then we inject a blocker. Turn 2 must see
    # the notice in its message history.
    adapter.push_turn([ContentDelta(text="hi"), TurnComplete(stop_reason="end_turn")])
    # Provide a 2nd turn in case the loop continues (shouldn't normally — but
    # interrupt forces a continue).
    adapter.push_turn([ContentDelta(text="ok handled"), TurnComplete(stop_reason="end_turn")])

    transcript = TranscriptWriter(object_store, "wakeup-tb")
    loop = AgentLoop(
        candidates=[fake_entry(id="m")],
        adapter_for=lambda e: adapter,
        model_name_for=lambda e: e.id,
        transcript=transcript,
        blocker_watcher=watcher,
        max_turns=4,
    )

    # Insert the blocker BEFORE running so the very first turn-boundary check
    # picks it up (we do this AFTER seeding the persona/mailbox above).
    await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="leader", external_id="b1", sender="owner",
            urgency="blocker", body="STOP, check X",
        )
    )
    # Give the watcher one poll cycle to set the signal.
    for _ in range(50):
        if watcher.signal.is_set():
            break
        await asyncio.sleep(0.02)
    assert watcher.signal.is_set()

    result = await loop.run("be brief", [_user("go")])
    transcript.close()
    await watcher.stop()

    # The interrupt fired before turn 1.
    assert len(result.interrupt_events) >= 1
    assert result.interrupt_events[0]["where"] == "pre_turn"
    assert result.interrupt_events[0]["count"] == 1

    # The 1st call to the adapter must have already contained the notice
    # (before the original "go" — wait, notice gets APPENDED, so it's the 2nd
    # message in the call).
    call1_msgs = adapter.calls[0]["messages"]
    notice_texts = [
        blk.text or ""
        for m in call1_msgs
        for blk in m.content
        if blk.type == "text"
    ]
    assert any("INTERRUPT" in t for t in notice_texts)


@pytest.mark.asyncio
async def test_mid_stream_interrupt_breaks_and_continues(
    repos: SqliteRepositories, object_store: Path
) -> None:
    """Blocker arrives WHILE the model is streaming. The loop breaks the
    current stream, persists the partial assistant turn, injects the notice,
    and runs another turn."""
    await repos.personas.upsert(
        Persona(name="leader", role_description="l", system_prompt="l")
    )
    await repos.mailbox.ensure_mailbox("leader")
    watcher = BlockerWatcher(
        repos=repos, recipient="leader", baseline_msg_id=0, poll_interval_s=0.02
    )
    await watcher.start()

    # Slow adapter: first turn has 6 small deltas spaced by 0.05s = 0.3s total
    # window. We'll insert a blocker mid-way.
    slow = _SlowAdapter(
        events_per_turn=[
            [
                ContentDelta(text="part-1 "),
                ContentDelta(text="part-2 "),
                ContentDelta(text="part-3 "),
                ContentDelta(text="part-4 "),
                ContentDelta(text="part-5 "),
                TurnComplete(stop_reason="end_turn"),
            ],
            # After the interrupt, the loop will call stream_turn again. The
            # 2nd call says "ok done".
            [ContentDelta(text="ok done"), TurnComplete(stop_reason="end_turn")],
        ],
        gap_s=0.05,
    )

    transcript = TranscriptWriter(object_store, "wakeup-mid")
    loop = AgentLoop(
        candidates=[fake_entry(id="m")],
        adapter_for=lambda e: slow,
        model_name_for=lambda e: e.id,
        transcript=transcript,
        blocker_watcher=watcher,
        max_turns=4,
    )

    async def inject_blocker_soon() -> None:
        await asyncio.sleep(0.12)  # ~2-3 deltas in
        await repos.mailbox.insert_message(
            MailboxMessage(
                recipient="leader", external_id="b-mid", sender="owner",
                urgency="blocker", body="STOP NOW",
            )
        )

    inject_task = asyncio.create_task(inject_blocker_soon())
    result = await loop.run("be brief", [_user("go")])
    await inject_task
    transcript.close()
    await watcher.stop()

    # The mid-stream interrupt fired.
    assert any(
        ev["where"] == "mid_stream" for ev in result.interrupt_events
    ), f"expected mid_stream interrupt, got {result.interrupt_events}"

    # The 2nd LLM call must have received the partial assistant message + the
    # interrupt notice as the most recent user message.
    assert slow.calls >= 2
    # The final text comes from the post-interrupt turn.
    assert "ok done" in result.text


@pytest.mark.asyncio
async def test_no_interrupt_when_no_blockers(
    repos: SqliteRepositories, object_store: Path
) -> None:
    """Sanity: with no blockers in the mailbox, the watcher never fires and
    the agent loop runs normally."""
    await repos.personas.upsert(
        Persona(name="leader", role_description="l", system_prompt="l")
    )
    await repos.mailbox.ensure_mailbox("leader")
    watcher = BlockerWatcher(
        repos=repos, recipient="leader", baseline_msg_id=0, poll_interval_s=0.02
    )
    await watcher.start()
    try:
        adapter = FakeAdapter()
        adapter.push_turn([ContentDelta(text="all clear"), TurnComplete(stop_reason="end_turn")])
        transcript = TranscriptWriter(object_store, "wakeup-clear")
        loop = AgentLoop(
            candidates=[fake_entry(id="m")],
            adapter_for=lambda e: adapter,
            model_name_for=lambda e: e.id,
            transcript=transcript,
            blocker_watcher=watcher,
        )
        # Let the watcher poll a few times before launching.
        await asyncio.sleep(0.1)
        result = await loop.run("", [_user("go")])
        transcript.close()
        assert result.interrupt_events == []
        assert result.text == "all clear"
    finally:
        await watcher.stop()


# ---------------------------------------------------------------------------
# High-urgency mail: boundary injection only, NO mid-stream break
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_high_urgency_mail_injects_at_boundary_not_midstream(
    repos: SqliteRepositories, object_store: Path,
) -> None:
    """urgency=high mail must surface to a running agent — but only at the
    TURN BOUNDARY, never mid-stream. Mid-stream interrupts are reserved
    for urgency=blocker ('system is waiting')."""
    await repos.personas.upsert(
        Persona(name="leader", role_description="l", system_prompt="l")
    )
    await repos.mailbox.ensure_mailbox("leader")
    watcher = BlockerWatcher(
        repos=repos, recipient="leader", baseline_msg_id=0,
        min_urgency="high", poll_interval_s=0.02,
    )
    await watcher.start()

    slow = _SlowAdapter(
        events_per_turn=[
            [
                ContentDelta(text="part-1 "),
                ContentDelta(text="part-2 "),
                ContentDelta(text="part-3 "),
                ContentDelta(text="part-4 "),
                ContentDelta(text="part-5 "),
                TurnComplete(stop_reason="end_turn"),
            ],
            [ContentDelta(text="next turn"), TurnComplete(stop_reason="end_turn")],
        ],
        gap_s=0.05,
    )

    transcript = TranscriptWriter(object_store, "wakeup-high-mid")
    loop = AgentLoop(
        candidates=[fake_entry(id="m")],
        adapter_for=lambda e: slow,
        model_name_for=lambda e: e.id,
        transcript=transcript,
        blocker_watcher=watcher,
        max_turns=4,
    )

    async def inject_high_mail_mid_stream() -> None:
        # Insert HIGH (not blocker) midway through turn 1's slow stream
        await asyncio.sleep(0.12)
        await repos.mailbox.insert_message(
            MailboxMessage(
                recipient="leader", external_id="h-mid", sender="owner",
                urgency="high", body="please reply on this thread",
            )
        )

    inject_task = asyncio.create_task(inject_high_mail_mid_stream())
    result = await loop.run("be brief", [_user("go")])
    await inject_task
    transcript.close()
    await watcher.stop()

    # No mid_stream interrupt event — high alone never breaks the stream
    assert all(
        ev["where"] != "mid_stream" for ev in result.interrupt_events
    ), f"high should NOT cause mid_stream; got {result.interrupt_events}"
    # The boundary injection fires — at either pre_turn (next iteration)
    # or post_turn_before_break (the same iteration that would have exited).
    assert any(
        ev["where"] in ("pre_turn", "post_turn_before_break")
        for ev in result.interrupt_events
    ), f"expected boundary injection; got {result.interrupt_events}"
    # Adapter was called at least twice — turn 2 ran because of the notice
    assert slow.calls >= 2


@pytest.mark.asyncio
async def test_blocker_still_does_midstream_when_high_also_present(
    repos: SqliteRepositories, object_store: Path,
) -> None:
    """If both high AND blocker pending, blocker semantics win →
    mid-stream interrupt fires. Verifies has_blocker_pending guards
    the mid-stream path."""
    await repos.personas.upsert(
        Persona(name="leader", role_description="l", system_prompt="l")
    )
    await repos.mailbox.ensure_mailbox("leader")
    watcher = BlockerWatcher(
        repos=repos, recipient="leader", baseline_msg_id=0,
        min_urgency="high", poll_interval_s=0.02,
    )
    await watcher.start()

    slow = _SlowAdapter(
        events_per_turn=[
            [
                ContentDelta(text="part-1 "),
                ContentDelta(text="part-2 "),
                ContentDelta(text="part-3 "),
                ContentDelta(text="part-4 "),
                ContentDelta(text="part-5 "),
                TurnComplete(stop_reason="end_turn"),
            ],
            [ContentDelta(text="handled"), TurnComplete(stop_reason="end_turn")],
        ],
        gap_s=0.05,
    )
    transcript = TranscriptWriter(object_store, "wakeup-mixed")
    loop = AgentLoop(
        candidates=[fake_entry(id="m")],
        adapter_for=lambda e: slow,
        model_name_for=lambda e: e.id,
        transcript=transcript,
        blocker_watcher=watcher,
        max_turns=4,
    )

    async def inject_mixed() -> None:
        await asyncio.sleep(0.10)
        await repos.mailbox.insert_message(
            MailboxMessage(
                recipient="leader", external_id="h-mix", sender="owner",
                urgency="high", body="optional follow up",
            )
        )
        # A blocker arrives just after, which SHOULD trigger mid-stream
        await asyncio.sleep(0.02)
        await repos.mailbox.insert_message(
            MailboxMessage(
                recipient="leader", external_id="b-mix", sender="owner",
                urgency="blocker", body="STOP, change of plan",
            )
        )

    inject_task = asyncio.create_task(inject_mixed())
    result = await loop.run("be brief", [_user("go")])
    await inject_task
    transcript.close()
    await watcher.stop()

    assert any(
        ev["where"] == "mid_stream" for ev in result.interrupt_events
    ), f"expected mid_stream interrupt because a blocker landed; got {result.interrupt_events}"
