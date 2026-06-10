"""H1 dead-loop guard: a wakeup that spins on the SAME (tool, args) call turn
after turn gets one nudge, then is cooperatively stopped (needs_continuation)
via the S0 seam — instead of burning every remaining turn and (per A2) risking
a phantom 'completed'."""

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
async def test_repeated_identical_tool_call_nudges_then_bails(
    object_store: Path,
) -> None:
    adapter = FakeAdapter()
    # Same tool, same args, every turn — the degenerate dead loop.
    for _ in range(8):
        adapter.push_turn(
            [
                ToolUseComplete(id="t", name="list_tasks", input={"status": "x"}),
                TurnComplete(stop_reason="tool_use"),
            ]
        )
    transcript = TranscriptWriter(object_store, "wakeup-deadloop")
    loop = build_single_candidate_loop(
        adapter, transcript, max_turns=20, loop_repeat_threshold=3
    )

    result = await loop.run(system_prompt="", initial_messages=[_user("go")])
    transcript.close()

    # count hits 3 on turn 3 → nudge; turn 4 repeats → bail. So 4 turns ran,
    # and the wakeup is needs_continuation (re-dispatchable), NOT completed.
    assert result.turns == 4
    assert result.status == "needs_continuation"
    assert "loop_repeat_nudge_injected" in transcript.path.read_text()
    assert "loop_repeat_bail" in transcript.path.read_text()


@pytest.mark.asyncio
async def test_jittering_truncated_args_retries_still_trip_guard(
    object_store: Path,
) -> None:
    """The 2026-06 field incident: a model whose tool args are cut by the
    output budget regenerates the payload every retry, so the ``_raw``
    bytes jitter by a few chars and exact-identity fingerprinting never
    matches — 13 consecutive ~10KB truncated writes sailed past the
    guard to max_turns. The fingerprint must collapse the ``_raw``
    sentinel to (tool, <truncated-args>) so this class still trips."""
    adapter = FakeAdapter()
    for i in range(8):
        adapter.push_turn(
            [
                ToolUseComplete(
                    id="t",
                    name="python_exec",
                    # Same failing call, jittering bytes — like regenerated
                    # code cut at a slightly different offset each turn.
                    input={"_raw": '{"code": "' + "spec text " * 3 + "x" * i},
                ),
                TurnComplete(stop_reason="max_tokens"),
            ]
        )
    transcript = TranscriptWriter(object_store, "wakeup-raw-jitter")
    loop = build_single_candidate_loop(
        adapter, transcript, max_turns=20, loop_repeat_threshold=3
    )

    result = await loop.run(system_prompt="", initial_messages=[_user("go")])
    transcript.close()

    assert result.turns == 4
    assert result.status == "needs_continuation"
    assert "loop_repeat_nudge_injected" in transcript.path.read_text()
    assert "loop_repeat_bail" in transcript.path.read_text()


@pytest.mark.asyncio
async def test_distinct_args_each_turn_do_not_trigger_guard(
    object_store: Path,
) -> None:
    adapter = FakeAdapter()
    # Same tool but DIFFERENT args each turn (e.g. paging through results) —
    # not a dead loop; the fingerprint changes so the guard never fires.
    # mailbox_send is user-facing, so the clean no-tool finish is 'completed'
    # rather than the (orthogonal) silent-close path.
    for i in range(5):
        adapter.push_turn(
            [
                ToolUseComplete(id="t", name="mailbox_send", input={"to": "owner", "n": i}),
                TurnComplete(stop_reason="tool_use"),
            ]
        )
    adapter.push_turn([ContentDelta(text="done"), TurnComplete(stop_reason="end_turn")])
    transcript = TranscriptWriter(object_store, "wakeup-distinct")
    loop = build_single_candidate_loop(
        adapter, transcript, max_turns=20, loop_repeat_threshold=3
    )

    result = await loop.run(system_prompt="", initial_messages=[_user("go")])
    transcript.close()

    assert result.status == "completed"
    assert "loop_repeat_bail" not in transcript.path.read_text()


@pytest.mark.asyncio
async def test_guard_disabled_when_threshold_zero(object_store: Path) -> None:
    adapter = FakeAdapter()
    for _ in range(4):
        adapter.push_turn(
            [
                ToolUseComplete(id="t", name="mailbox_send", input={"to": "owner"}),
                TurnComplete(stop_reason="tool_use"),
            ]
        )
    adapter.push_turn([ContentDelta(text="done"), TurnComplete(stop_reason="end_turn")])
    transcript = TranscriptWriter(object_store, "wakeup-guard-off")
    loop = build_single_candidate_loop(
        adapter, transcript, max_turns=20, loop_repeat_threshold=0
    )

    result = await loop.run(system_prompt="", initial_messages=[_user("go")])
    transcript.close()

    # With the guard off, identical calls run until the model itself stops.
    assert result.status == "completed"
    assert result.turns == 5
    assert "loop_repeat_bail" not in transcript.path.read_text()
