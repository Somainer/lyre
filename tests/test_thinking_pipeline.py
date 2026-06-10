"""End-to-end-ish test for the thinking pipeline.

The user reported: a DeepSeek-V4-pro wakeup emitted ZERO content_delta
events in the transcript (every turn_end had text_len=0) — and yet the
model clearly *did* reason between turns. Diagnosis: DeepSeek's
Anthropic-compat layer streams reasoning as `thinking_delta` blocks,
which our adapter was silently dropping.

This test pins the fix:
  1. Anthropic adapter emits a `ThinkingDelta` for each `thinking_delta`
     stream event.
  2. AgentLoop forwards each `ThinkingDelta` to
     `TranscriptWriter.write_thinking_delta` (separate from
     `write_delta` so the audit log keeps the two voices distinct).
  3. The dashboard activity feed aggregates consecutive thinking_delta
     transcript rows into a single `kind="thinking"` event for display.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lyre.adapter.anthropic import AnthropicAdapter
from lyre.adapter.llm_adapter import (
    LyreContentBlock,
    LyreMessage,
    ThinkingBlockComplete,
    ThinkingDelta,
    ToolUseComplete,
    TurnComplete,
    Usage,
)
from lyre.dashboard.activity import _tail_transcript_events
from lyre.persistence.models import Persona, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.agent_loop import AgentLoop
from lyre.runtime.tools import ToolContext
from lyre.runtime.tools.builtin import build_default_registry
from lyre.runtime.transcript import TranscriptWriter

from .fake_adapter import FakeAdapter
from .helpers import fake_entry

# --------------------------------------------------------------------
# Adapter unit: thinking_delta → ThinkingDelta
# --------------------------------------------------------------------


def test_anthropic_adapter_emits_thinking_delta() -> None:
    """A ContentBlockDeltaEvent whose delta.type == 'thinking_delta'
    must surface as a ThinkingDelta StreamEvent. Previously dropped on
    the floor — see screenshot bug."""
    adapter = AnthropicAdapter(api_key="x", base_url=None)
    # Mock the SDK event shape: ContentBlockDeltaEvent with a
    # ThinkingDelta sub-object.
    evt = MagicMock()
    # isinstance-check uses anthropic.types.ContentBlockDeltaEvent;
    # patch the import to make our mock pass that check.
    from anthropic.types import ContentBlockDeltaEvent  # noqa: PLC0415
    evt.__class__ = ContentBlockDeltaEvent
    evt.index = 0
    evt.delta = MagicMock()
    evt.delta.type = "thinking_delta"
    evt.delta.thinking = "Let me reason about this step by step."

    out = adapter._anthropic_to_lyre(evt, tool_use_buffers={})
    assert isinstance(out, ThinkingDelta)
    assert out.text == "Let me reason about this step by step."


# --------------------------------------------------------------------
# AgentLoop integration: ThinkingDelta → transcript thinking_delta line
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_loop_writes_thinking_delta_to_transcript(
    repos: SqliteRepositories, object_store: Path
) -> None:
    """When the model streams thinking, the transcript jsonl gets one
    line per chunk with type='thinking_delta' so the dashboard can
    surface the reasoning voice separately from out-loud text."""
    for name in ("worker", "owner"):
        await repos.personas.upsert(
            Persona(name=name, role_description=name, system_prompt=name)
        )
        await repos.agents.create(agent_id=name, persona_name=name)
    task_id = await repos.tasks.create(
        TaskSpec(agent_id="worker", goal="g", acceptance="a")
    )
    wakeup_id = await repos.wakeups.start(task_id, "worker")
    await repos.tasks.claim_lease(task_id, wakeup_id, duration_sec=600)
    ctx = ToolContext(
        repos=repos,
        task_id=task_id,
        wakeup_id=wakeup_id,
        persona_name="worker",
        agent_id="worker",
    )
    transcript = TranscriptWriter(object_store, wakeup_id)
    adapter = FakeAdapter()
    adapter.push_turn(
        [
            ThinkingDelta(text="First, let me think. "),
            ThinkingDelta(text="The user asked X, so I should Y."),
            Usage(input_tokens=10, output_tokens=2),
            TurnComplete(stop_reason="end_turn"),
        ]
    )

    loop = AgentLoop(
        candidates=[fake_entry(id="m")],
        adapter_for=lambda e: adapter,
        model_name_for=lambda e: e.id,
        transcript=transcript,
        tool_registry=build_default_registry(),
        tool_context=ctx,
        allowed_tools=["mailbox_send"],
    )
    await loop.run(
        system_prompt="",
        initial_messages=[
            LyreMessage(role="user", content=[LyreContentBlock(type="text", text="go")])
        ],
    )
    transcript.close()

    # Two thinking_delta rows in the transcript, no content_delta rows.
    path = Path(transcript.uri.removeprefix("file://"))
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    thinking = [r for r in rows if r.get("type") == "thinking_delta"]
    text = [r for r in rows if r.get("type") == "content_delta"]
    assert [r["text"] for r in thinking] == [
        "First, let me think. ",
        "The user asked X, so I should Y.",
    ]
    assert text == []


# --------------------------------------------------------------------
# Activity feed: thinking_delta rows → one 'thinking' event per run
# --------------------------------------------------------------------


def test_activity_aggregates_thinking_delta_into_one_event(
    tmp_path: Path,
) -> None:
    """Consecutive `thinking_delta` rows should fold into a single
    activity event with kind='thinking' so the dashboard shows one
    brain bubble per reasoning burst, not one per token."""
    wakeup_dir = tmp_path / "wakeups" / "test-wk"
    wakeup_dir.mkdir(parents=True)
    path = wakeup_dir / "transcript.jsonl"
    rows = [
        {"type": "thinking_delta", "text": "First ", "ts": 1700000000000},
        {"type": "thinking_delta", "text": "second ", "ts": 1700000000100},
        {"type": "thinking_delta", "text": "third.", "ts": 1700000000200},
        {"type": "tool_use", "name": "mailbox_send", "id": "t1",
         "input": {"to": "owner", "body": "x"}, "ts": 1700000000300},
        # Mix in another thinking burst AFTER the tool — operator
        # should see two distinct bubbles, not one merged.
        {"type": "thinking_delta", "text": "Now reflect.", "ts": 1700000000400},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    events = _tail_transcript_events(
        path=path,
        wakeup_id="test-wk",
        persona="worker",
        task_id="task-1",
        started_at="2026-05-20T00:00:00.000Z",
    )
    thinking_events = [e for e in events if e.kind == "thinking"]
    assert len(thinking_events) == 2
    assert thinking_events[0].detail["text"] == "First second third."
    assert thinking_events[1].detail["text"] == "Now reflect."
    # The tool_use survives in between
    assert any(e.kind == "tool_use" for e in events)


@pytest.mark.asyncio
async def test_thinking_block_is_echoed_in_replayed_assistant_message(
    repos: SqliteRepositories, object_store: Path
) -> None:
    """Regression for 400 'content[].thinking must be passed back'.

    DeepSeek-V4-pro (and Anthropic with extended thinking) refuse the
    next API call if the prior assistant turn's thinking block isn't
    echoed back verbatim. The bug: AgentLoop captured ThinkingDelta
    chunks but never assembled them into the assistant message it
    appended to `messages`. After turn 1 emitted a thinking block,
    turn 2's request was missing the prior thinking and the API
    returned BadRequestError.

    This test simulates two-turn flow with a thinking block in turn 1
    and verifies turn 2's call to the model includes the thinking
    block in the prior assistant message — in the FIRST position
    (Anthropic ordering requirement: thinking before text/tool_use).
    """
    for name in ("worker", "owner"):
        await repos.personas.upsert(
            Persona(name=name, role_description=name, system_prompt=name)
        )
        await repos.agents.create(agent_id=name, persona_name=name)
    task_id = await repos.tasks.create(
        TaskSpec(agent_id="worker", goal="g", acceptance="a")
    )
    wakeup_id = await repos.wakeups.start(task_id, "worker")
    await repos.tasks.claim_lease(task_id, wakeup_id, duration_sec=600)
    ctx = ToolContext(
        repos=repos, task_id=task_id, wakeup_id=wakeup_id,
        persona_name="worker", agent_id="worker",
    )
    transcript = TranscriptWriter(object_store, wakeup_id)
    adapter = FakeAdapter()
    # Turn 1: model emits thinking + tool_use. (Streams in real life:
    # thinking_delta chunks then signature_delta then stop; tool_use_start
    # + input_json_delta then stop. We use the post-aggregation
    # ThinkingBlockComplete event here for brevity.)
    adapter.push_turn(
        [
            ThinkingDelta(text="Let me reason. "),
            ThinkingBlockComplete(
                text="Let me reason. The answer is X.",
                signature="sig-abc",
            ),
            ToolUseComplete(
                id="t1", name="mailbox_send",
                input={"to": "owner", "body": "hi"},
            ),
            Usage(input_tokens=10, output_tokens=5),
            TurnComplete(stop_reason="end_turn"),
        ]
    )
    # Turn 2: model says done (no tools) → natural exit.
    adapter.push_turn([TurnComplete(stop_reason="end_turn")])

    loop = AgentLoop(
        candidates=[fake_entry(id="m")],
        adapter_for=lambda e: adapter,
        model_name_for=lambda e: e.id,
        transcript=transcript,
        tool_registry=build_default_registry(),
        tool_context=ctx,
        allowed_tools=["mailbox_send"],
    )
    await loop.run(
        system_prompt="",
        initial_messages=[
            LyreMessage(role="user", content=[LyreContentBlock(type="text", text="go")])
        ],
    )
    transcript.close()

    # The second API call's messages must include an assistant message
    # whose FIRST block is the thinking block with full text +
    # signature preserved. Without this, the provider rejects the call.
    assert len(adapter.calls) >= 2
    call2_msgs = adapter.calls[1]["messages"]
    assistant_msgs = [m for m in call2_msgs if m.role == "assistant"]
    assert assistant_msgs, "turn 2 must have an assistant message in history"
    blocks = assistant_msgs[0].content
    assert blocks[0].type == "thinking", (
        f"thinking must be the FIRST block, got {blocks[0].type}; "
        f"order is {[b.type for b in blocks]}"
    )
    assert blocks[0].text == "Let me reason. The answer is X."
    assert blocks[0].signature == "sig-abc"
    # Tool_use must still be present and AFTER the thinking block.
    tool_blocks = [b for b in blocks if b.type == "tool_use"]
    assert len(tool_blocks) == 1
    assert tool_blocks[0].tool_name == "mailbox_send"


def test_anthropic_adapter_serializes_thinking_block_back_to_api() -> None:
    """The `_lyre_to_anthropic_messages` converter must round-trip a
    LyreContentBlock(type='thinking', ...) into the Anthropic API's
    `{type: 'thinking', thinking: ..., signature: ...}` shape. With
    signature omitted when empty (DeepSeek compat layer doesn't sign;
    Anthropic-proper sets it)."""
    msgs = [
        LyreMessage(
            role="assistant",
            content=[
                LyreContentBlock(
                    type="thinking",
                    text="reasoning here",
                    signature="sig-xyz",
                ),
                LyreContentBlock(type="text", text="final answer"),
            ],
        )
    ]
    out = AnthropicAdapter._lyre_to_anthropic_messages(msgs)
    assert len(out) == 1
    blocks = out[0]["content"]
    assert blocks[0] == {
        "type": "thinking",
        "thinking": "reasoning here",
        "signature": "sig-xyz",
    }
    assert blocks[1] == {"type": "text", "text": "final answer"}

    # Empty signature: include the thinking block but omit the
    # signature key (DeepSeek doesn't sign).
    msgs[0].content[0].signature = None
    out2 = AnthropicAdapter._lyre_to_anthropic_messages(msgs)
    assert out2[0]["content"][0] == {
        "type": "thinking",
        "thinking": "reasoning here",
    }


def test_activity_distinguishes_thinking_from_assistant_text(
    tmp_path: Path,
) -> None:
    """thinking_delta and content_delta are SEPARATE voices — must not
    be merged into a single event. The brain badge in the UI relies on
    this distinction."""
    wakeup_dir = tmp_path / "wakeups" / "test-wk2"
    wakeup_dir.mkdir(parents=True)
    path = wakeup_dir / "transcript.jsonl"
    rows = [
        {"type": "thinking_delta", "text": "Reasoning… ", "ts": 1700000000000},
        {"type": "content_delta", "text": "Hi there.", "ts": 1700000000100},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    events = _tail_transcript_events(
        path=path,
        wakeup_id="test-wk2",
        persona="worker",
        task_id="task-1",
        started_at="2026-05-20T00:00:00.000Z",
    )
    kinds = [e.kind for e in events]
    assert "thinking" in kinds
    assert "assistant_text" in kinds
    think = next(e for e in events if e.kind == "thinking")
    text = next(e for e in events if e.kind == "assistant_text")
    assert "Reasoning" in think.detail["text"]
    assert "Hi there" in text.detail["text"]
