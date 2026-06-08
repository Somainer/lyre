"""B2 operator cooperative cancel: the loop observes a durable cancel request
at the turn boundary (via cancel_check), finishes the current turn, and stops
with status 'cancelled' — surgically, without killing the whole process."""

from __future__ import annotations

from pathlib import Path

import pytest

from lyre.adapter.llm_adapter import (
    ContentDelta,
    LyreContentBlock,
    LyreMessage,
    ToolUseComplete,
    TurnComplete,
)
from lyre.runtime.transcript import TranscriptWriter

from .fake_adapter import FakeAdapter
from .helpers import build_single_candidate_loop


def _user(text: str) -> LyreMessage:
    return LyreMessage(role="user", content=[LyreContentBlock(type="text", text=text)])


@pytest.mark.asyncio
async def test_cancel_requested_stops_with_cancelled_status(object_store: Path) -> None:
    adapter = FakeAdapter()
    # The model would keep working, but cancel fires at the first boundary.
    for _ in range(5):
        adapter.push_turn(
            [
                ToolUseComplete(id="t", name="mailbox_send", input={"to": "owner"}),
                TurnComplete(stop_reason="tool_use"),
            ]
        )

    async def _always_cancel() -> str:
        return "operator stop"

    transcript = TranscriptWriter(object_store, "wakeup-cancel")
    loop = build_single_candidate_loop(
        adapter, transcript, cancel_check=_always_cancel
    )
    result = await loop.run(system_prompt="", initial_messages=[_user("go")])
    transcript.close()

    assert result.status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_mid_wakeup_honored_at_next_boundary(object_store: Path) -> None:
    adapter = FakeAdapter()
    for _ in range(6):
        adapter.push_turn(
            [
                ToolUseComplete(id="t", name="mailbox_send", input={"to": "owner"}),
                TurnComplete(stop_reason="tool_use"),
            ]
        )

    calls = {"n": 0}

    async def _cancel_after_two() -> str | None:
        calls["n"] += 1
        # None for the first two boundaries (turns 1, 2), then request cancel.
        return None if calls["n"] <= 2 else "stop now"

    transcript = TranscriptWriter(object_store, "wakeup-cancel-mid")
    loop = build_single_candidate_loop(
        adapter, transcript, cancel_check=_cancel_after_two
    )
    result = await loop.run(system_prompt="", initial_messages=[_user("go")])
    transcript.close()

    # Two turns ran, the third boundary observed the cancel.
    assert result.status == "cancelled"
    assert result.turns == 3


@pytest.mark.asyncio
async def test_no_cancel_completes_normally(object_store: Path) -> None:
    adapter = FakeAdapter()
    adapter.push_turn(
        [
            ToolUseComplete(id="t", name="mailbox_send", input={"to": "owner"}),
            TurnComplete(stop_reason="tool_use"),
        ]
    )
    adapter.push_turn([ContentDelta(text="done"), TurnComplete(stop_reason="end_turn")])

    async def _never_cancel() -> str | None:
        return None

    transcript = TranscriptWriter(object_store, "wakeup-no-cancel")
    loop = build_single_candidate_loop(
        adapter, transcript, cancel_check=_never_cancel
    )
    result = await loop.run(system_prompt="", initial_messages=[_user("go")])
    transcript.close()

    assert result.status == "completed"
