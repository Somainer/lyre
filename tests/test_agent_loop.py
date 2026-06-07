"""Tests for the AgentLoop run() (single-turn cases).

Multi-turn / tool dispatch tests live in test_agent_loop_multi_turn.py.
Fallback / health-circuit tests live in test_agent_loop_fallback.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lyre.adapter.llm_adapter import (
    ContentDelta,
    LyreContentBlock,
    LyreMessage,
    ToolUseComplete,
    TurnComplete,
    Usage,
)
from lyre.runtime.transcript import TranscriptWriter

from .fake_adapter import FakeAdapter
from .helpers import build_single_candidate_loop


@pytest.mark.asyncio
async def test_run_collects_text_and_usage(object_store: Path) -> None:
    adapter = FakeAdapter()
    adapter.push_turn(
        [
            ContentDelta(text="hello "),
            ContentDelta(text="world"),
            Usage(input_tokens=10, output_tokens=2),
            TurnComplete(stop_reason="end_turn"),
        ]
    )
    transcript = TranscriptWriter(object_store, "wakeup-test")
    loop = build_single_candidate_loop(adapter, transcript)

    result = await loop.run(
        system_prompt="be helpful",
        initial_messages=[
            LyreMessage(role="user", content=[LyreContentBlock(type="text", text="hi")])
        ],
    )
    transcript.close()

    assert result.text == "hello world"
    assert result.usage == {"input_tokens": 10, "output_tokens": 2}
    assert result.status == "completed"
    assert result.tool_calls == []
    assert result.turns == 1
    assert result.model_id == "fake.test-model"

    raw = transcript.path.read_text()
    assert "hello " in raw
    assert "world" in raw


@pytest.mark.asyncio
async def test_run_with_tool_use_loops_until_end_turn(object_store: Path) -> None:
    """Without a tool registry, the loop emits an error tool_result and
    keeps going (max_turns cap kicks in)."""
    adapter = FakeAdapter()
    adapter.push_turn(
        [
            ToolUseComplete(id="t1", name="mailbox_send", input={"to": "owner"}),
            TurnComplete(stop_reason="tool_use"),
        ]
    )
    adapter.push_turn([ContentDelta(text="ok"), TurnComplete(stop_reason="end_turn")])
    transcript = TranscriptWriter(object_store, "wakeup-tool")
    loop = build_single_candidate_loop(adapter, transcript)

    result = await loop.run(
        system_prompt="",
        initial_messages=[
            LyreMessage(role="user", content=[LyreContentBlock(type="text", text="hi")])
        ],
    )
    transcript.close()

    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["name"] == "mailbox_send"
    assert result.turns == 2
    assert result.status == "completed"


@pytest.mark.asyncio
async def test_max_turns_exhaustion_on_end_turn_is_needs_continuation_not_completed(
    object_store: Path,
) -> None:
    """A2: a wakeup truncated by max_turns must NOT be reported 'completed',
    even when every turn's stop_reason is 'end_turn' alongside tool_use (the
    DeepSeek/Anthropic pattern where end_turn is metadata, not a control
    signal). The clean-finish break only fires on a no-tool turn, so this loop
    runs to exhaustion and must surface as 'needs_continuation' — observable
    and re-dispatchable — instead of silently claiming success."""
    adapter = FakeAdapter()
    # Every turn calls a tool AND reports end_turn → the no-tool clean-finish
    # break never fires; the loop runs all max_turns iterations.
    for _ in range(3):
        adapter.push_turn(
            [
                ToolUseComplete(id="t", name="mailbox_send", input={"to": "owner"}),
                TurnComplete(stop_reason="end_turn"),
            ]
        )
    transcript = TranscriptWriter(object_store, "wakeup-maxturns")
    loop = build_single_candidate_loop(adapter, transcript, max_turns=3)

    result = await loop.run(
        system_prompt="",
        initial_messages=[
            LyreMessage(role="user", content=[LyreContentBlock(type="text", text="go")])
        ],
    )
    transcript.close()

    assert result.turns == 3
    assert result.status == "needs_continuation"


@pytest.mark.asyncio
async def test_compaction_signal_falls_back_to_estimate_when_no_usage(
    object_store: Path,
) -> None:
    """D1: when the adapter emits no Usage event, the loop falls back to a
    coarse client token estimate so context_peak (and the compaction guard)
    don't silently stay at zero — which would let the wakeup sail past the
    model's real context window with auto-compaction disabled."""
    adapter = FakeAdapter()
    # A turn with NO Usage event (FakeAdapter only auto-appends TurnComplete).
    adapter.push_turn([ContentDelta(text="reply"), TurnComplete(stop_reason="end_turn")])
    transcript = TranscriptWriter(object_store, "wakeup-d1")
    loop = build_single_candidate_loop(adapter, transcript)

    result = await loop.run(
        system_prompt="sys",
        initial_messages=[
            LyreMessage(
                role="user",
                content=[LyreContentBlock(type="text", text="word " * 400)],
            )
        ],
    )
    transcript.close()

    # No Usage was emitted, yet context_peak is populated from the estimate.
    assert result.context_peak_tokens > 0


@pytest.mark.asyncio
async def test_real_usage_wins_over_estimate(object_store: Path) -> None:
    """D1: a real Usage event is never overridden by the client estimate."""
    adapter = FakeAdapter()
    adapter.push_turn(
        [
            ContentDelta(text="x" * 8000),
            Usage(input_tokens=7, output_tokens=2),
            TurnComplete(stop_reason="end_turn"),
        ]
    )
    transcript = TranscriptWriter(object_store, "wakeup-d1b")
    loop = build_single_candidate_loop(adapter, transcript)

    result = await loop.run(
        system_prompt="",
        initial_messages=[
            LyreMessage(role="user", content=[LyreContentBlock(type="text", text="hi")])
        ],
    )
    transcript.close()

    # Real Usage (7), not the ~2000-token estimate of the 8000-char turn.
    assert result.usage["input_tokens"] == 7
    assert result.context_peak_tokens == 7


@pytest.mark.asyncio
async def test_run_passes_system_and_messages_through(object_store: Path) -> None:
    adapter = FakeAdapter()
    adapter.push_turn([ContentDelta(text=""), TurnComplete(stop_reason="end_turn")])
    transcript = TranscriptWriter(object_store, "wakeup-pass")
    loop = build_single_candidate_loop(adapter, transcript, model_id="fake.my-model")

    msg = LyreMessage(role="user", content=[LyreContentBlock(type="text", text="ping")])
    await loop.run(system_prompt="be terse", initial_messages=[msg])
    transcript.close()

    call = adapter.calls[0]
    assert call["system"] == "be terse"
    # model_name_for returns entry.id in the helper
    assert call["model"] == "fake.my-model"
    assert call["messages"] == [msg]
