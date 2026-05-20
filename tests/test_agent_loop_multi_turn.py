"""Multi-turn AgentLoop tests with tool dispatch.

Uses FakeAdapter (scripted streams) + real ToolRegistry + real SQLite.
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
from lyre.persistence.models import Persona, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.agent_loop import AgentLoop
from lyre.runtime.tools import ToolContext
from lyre.runtime.tools.builtin import build_default_registry
from lyre.runtime.transcript import TranscriptWriter

from .fake_adapter import FakeAdapter
from .helpers import fake_entry


@pytest.fixture
async def loop_setup(
    repos: SqliteRepositories, object_store: Path
) -> tuple[FakeAdapter, ToolContext, TranscriptWriter]:
    await repos.personas.upsert(
        Persona(name="worker", role_description="w", system_prompt="w")
    )
    await repos.personas.upsert(
        Persona(name="owner", role_description="o", system_prompt="o")
    )
    task_id = await repos.tasks.create(
        TaskSpec(persona_name="worker", goal="g", acceptance="a")
    )
    wakeup_id = await repos.wakeups.start(task_id, "worker")
    await repos.tasks.claim_lease(task_id, wakeup_id, duration_sec=600)
    return (
        FakeAdapter(),
        ToolContext(
            repos=repos,
            task_id=task_id,
            wakeup_id=wakeup_id,
            persona_name="worker",
        ),
        TranscriptWriter(object_store, wakeup_id),
    )


@pytest.mark.asyncio
async def test_run_multi_turn_with_one_tool_call(loop_setup) -> None:
    adapter, ctx, transcript = loop_setup

    # Turn 1: model calls mailbox_send
    adapter.push_turn(
        [
            ContentDelta(text="Sending update..."),
            ToolUseComplete(
                id="tu_1",
                name="mailbox_send",
                input={"to": "owner", "body": "PR is open"},
            ),
            Usage(input_tokens=100, output_tokens=20),
            TurnComplete(stop_reason="tool_use"),
        ]
    )
    # Turn 2: model sees the tool_result, decides done.
    adapter.push_turn(
        [
            ContentDelta(text="All done."),
            Usage(input_tokens=120, output_tokens=4),
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
    result = await loop.run(
        system_prompt="be brief",
        initial_messages=[
            LyreMessage(role="user", content=[LyreContentBlock(type="text", text="go")])
        ],
    )
    transcript.close()

    assert result.status == "completed"
    assert result.turns == 2
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["name"] == "mailbox_send"
    assert result.text == "All done."
    # Usage was accumulated across turns.
    assert result.usage["input_tokens"] == 220
    assert result.usage["output_tokens"] == 24

    # The outbox actually got the message.
    batch = await ctx.repos.outbox.dequeue_batch(limit=10)
    assert len(batch) == 1
    assert batch[0].kind == "mailbox_send"

    # Adapter saw 2 calls; the 2nd one's messages should include the
    # tool_result block from turn 1.
    assert len(adapter.calls) == 2
    call2_msgs = adapter.calls[1]["messages"]
    # call2 should have: original user, assistant w/ tool_use, user w/ tool_result
    assert call2_msgs[0].role == "user"
    assert call2_msgs[1].role == "assistant"
    assert any(blk.type == "tool_use" for blk in call2_msgs[1].content)
    assert call2_msgs[2].role == "user"
    assert any(blk.type == "tool_result" for blk in call2_msgs[2].content)


@pytest.mark.asyncio
async def test_run_blocks_disallowed_tool(loop_setup) -> None:
    adapter, ctx, transcript = loop_setup
    # Worker tries a tool that's NOT in its allowlist.
    adapter.push_turn(
        [
            ToolUseComplete(
                id="tu_x",
                name="report_side_effect",
                input={"kind": "opened_pr"},
            ),
            TurnComplete(stop_reason="tool_use"),
        ]
    )
    adapter.push_turn([ContentDelta(text="ok ignored"), TurnComplete(stop_reason="end_turn")])

    loop = AgentLoop(
        candidates=[fake_entry(id="m")],
        adapter_for=lambda e: adapter,
        model_name_for=lambda e: e.id,
        transcript=transcript,
        tool_registry=build_default_registry(),
        tool_context=ctx,
        allowed_tools=["mailbox_send"],  # no report_side_effect
    )
    result = await loop.run(
        system_prompt="",
        initial_messages=[
            LyreMessage(role="user", content=[LyreContentBlock(type="text", text="hi")])
        ],
    )
    transcript.close()

    assert result.status == "completed"
    # Tool result for the rejected call should be marked is_error.
    call2_msgs = adapter.calls[1]["messages"]
    tool_result_blocks = [
        blk for m in call2_msgs for blk in m.content if blk.type == "tool_result"
    ]
    assert len(tool_result_blocks) == 1
    assert tool_result_blocks[0].is_error
    assert "allowlist" in str(tool_result_blocks[0].tool_result)


@pytest.mark.asyncio
async def test_run_handles_tool_error_gracefully(loop_setup) -> None:
    adapter, ctx, transcript = loop_setup
    # mailbox_send is in allowlist but the model passes bad args → ToolError.
    adapter.push_turn(
        [
            ToolUseComplete(id="tu_bad", name="mailbox_send", input={"to": "owner"}),
            TurnComplete(stop_reason="tool_use"),
        ]
    )
    adapter.push_turn([ContentDelta(text="retry handled"), TurnComplete(stop_reason="end_turn")])

    loop = AgentLoop(
        candidates=[fake_entry(id="m")],
        adapter_for=lambda e: adapter,
        model_name_for=lambda e: e.id,
        transcript=transcript,
        tool_registry=build_default_registry(),
        tool_context=ctx,
        allowed_tools=["mailbox_send"],
    )
    result = await loop.run(
        system_prompt="",
        initial_messages=[
            LyreMessage(role="user", content=[LyreContentBlock(type="text", text="hi")])
        ],
    )
    transcript.close()

    assert result.status == "completed"
    tr_blocks = [
        blk
        for m in adapter.calls[1]["messages"]
        for blk in m.content
        if blk.type == "tool_result"
    ]
    assert tr_blocks[0].is_error
    assert "body" in str(tr_blocks[0].tool_result)


@pytest.mark.asyncio
async def test_run_respects_max_turns(loop_setup) -> None:
    adapter, ctx, transcript = loop_setup
    # Forever-tool-using model.
    for i in range(10):
        adapter.push_turn(
            [
                ToolUseComplete(
                    id=f"t{i}", name="mailbox_send",
                    input={"to": "owner", "body": f"ping {i}"},
                ),
                TurnComplete(stop_reason="tool_use"),
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
        max_turns=3,
    )
    result = await loop.run(
        system_prompt="",
        initial_messages=[
            LyreMessage(role="user", content=[LyreContentBlock(type="text", text="hi")])
        ],
    )
    transcript.close()
    assert result.turns == 3
    assert result.status == "needs_continuation"


@pytest.mark.asyncio
async def test_run_with_no_tools_single_turn(loop_setup) -> None:
    adapter, ctx, transcript = loop_setup
    adapter.push_turn([ContentDelta(text="hi"), TurnComplete(stop_reason="end_turn")])
    loop = AgentLoop(
        candidates=[fake_entry(id="m")],
        adapter_for=lambda e: adapter,
        model_name_for=lambda e: e.id,
        transcript=transcript,
        tool_registry=None, tool_context=None, allowed_tools=[],
    )
    result = await loop.run(
        system_prompt="",
        initial_messages=[
            LyreMessage(role="user", content=[LyreContentBlock(type="text", text="hi")])
        ],
    )
    transcript.close()
    assert result.turns == 1
    assert result.status == "completed"
    assert result.text == "hi"
