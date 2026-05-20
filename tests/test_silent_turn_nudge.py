"""Tests for the silent-turn nudge in AgentLoop.

The nudge guards against the observed failure mode where a model (notably
DeepSeek v4 pro on Anthropic-compat) calls info-gathering tools
(mailbox_read, list_personas, list_agents) and then end_turn's WITHOUT
ever calling a user-facing tool (mailbox_send, dispatch_task, etc.). The
wakeup looks "completed" but the sender is left waiting.

Rules:
  - Nudge fires only if at least one tool was called this wakeup AND
    none of them were user-facing.
  - Nudge fires at most ONCE per wakeup — a stubborn model can still
    close the run by end_turn'ing twice.
  - Plain text-only end_turn (no tools at all) does NOT trigger the
    nudge (legitimate chat path).
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
)
from lyre.persistence.models import Persona, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.tools import ToolContext
from lyre.runtime.tools.builtin import build_default_registry
from lyre.runtime.transcript import TranscriptWriter

from .fake_adapter import FakeAdapter
from .helpers import build_single_candidate_loop


async def _ctx(repos: SqliteRepositories) -> ToolContext:
    """Spin up a usable ToolContext so mailbox/list tools actually work."""
    await repos.personas.upsert(
        Persona(name="dispatcher", role_description="l", system_prompt="l")
    )
    await repos.personas.upsert(
        Persona(name="owner", role_description="o", system_prompt="o")
    )
    await repos.agents.create(agent_id="dispatcher", persona_name="dispatcher")
    await repos.agents.create(agent_id="owner", persona_name="owner")
    task_id = await repos.tasks.create(
        TaskSpec(agent_id="dispatcher", goal="g", acceptance="a")
    )
    wakeup_id = await repos.wakeups.start(task_id, "dispatcher")
    await repos.tasks.claim_lease(task_id, wakeup_id, duration_sec=600)
    return ToolContext(
        repos=repos, task_id=task_id, wakeup_id=wakeup_id,
        persona_name="dispatcher", agent_id="dispatcher",
    )


@pytest.mark.asyncio
async def test_nudge_fires_on_info_only_wakeup(
    repos: SqliteRepositories, object_store: Path
) -> None:
    """Model calls info-gathering tools only → nudge injected. If the
    model recovers and sends mailbox_send on the next turn, nudge cap
    isn't reached."""
    ctx = await _ctx(repos)

    adapter = FakeAdapter()
    # Turn 1: gather info via mailbox_read + list_agents, then end_turn.
    adapter.push_turn(
        [
            ToolUseComplete(id="t1", name="mailbox_read", input={}),
            ToolUseComplete(id="t2", name="list_agents", input={}),
            TurnComplete(stop_reason="end_turn"),
        ]
    )
    # Turn 2: model recovers and replies via mailbox_send (the good
    # outcome the nudge is trying to produce).
    adapter.push_turn(
        [
            ToolUseComplete(
                id="t3", name="mailbox_send",
                input={
                    "to": "owner", "body": "got it",
                    "_tool_use_id": "t3",
                },
            ),
            TurnComplete(stop_reason="end_turn"),
        ]
    )

    transcript_id = "wakeup-silent"
    transcript = TranscriptWriter(object_store, transcript_id)
    loop = build_single_candidate_loop(
        adapter, transcript,
        tool_registry=build_default_registry(),
        tool_context=ctx,
        allowed_tools=["mailbox_read", "list_agents", "mailbox_send"],
    )

    result = await loop.run(
        system_prompt="",
        initial_messages=[
            LyreMessage(
                role="user", content=[LyreContentBlock(type="text", text="hi")]
            )
        ],
    )
    transcript.close()

    # Three turns now: (1) info gathering → nudge, (2) mailbox_send →
    # tools execute, (3) model gets the tool_result back and emits a
    # no-tool response → loop breaks naturally. The third turn exists
    # because we no longer prematurely break on `stop_reason=end_turn`
    # alongside tool_use blocks — that was the bug behind every
    # "ack-then-stop" silent failure.
    assert result.turns == 3
    assert result.status == "completed"

    # Exactly one nudge fired; the second turn produced a user-facing
    # action so no further nudges were needed.
    raw = transcript.path.read_text()
    assert raw.count("silent_turn_nudge_injected") == 1


@pytest.mark.asyncio
async def test_nudge_does_not_fire_when_mailbox_send_was_called(
    repos: SqliteRepositories, object_store: Path
) -> None:
    """Model calls mailbox_send (user-facing) → no nudge."""
    ctx = await _ctx(repos)

    adapter = FakeAdapter()
    # Turn 1: read + reply, then end_turn. This is the GOOD path.
    adapter.push_turn(
        [
            ToolUseComplete(id="t1", name="mailbox_read", input={}),
            ToolUseComplete(
                id="t2", name="mailbox_send",
                input={
                    "to": "owner",
                    "body": "here's the reply",
                    "_tool_use_id": "t2",
                },
            ),
            TurnComplete(stop_reason="end_turn"),
        ]
    )

    transcript = TranscriptWriter(object_store, "wakeup-reply")
    loop = build_single_candidate_loop(
        adapter, transcript,
        tool_registry=build_default_registry(),
        tool_context=ctx,
        allowed_tools=["mailbox_read", "mailbox_send"],
    )

    result = await loop.run(
        system_prompt="",
        initial_messages=[
            LyreMessage(
                role="user", content=[LyreContentBlock(type="text", text="hi")]
            )
        ],
    )
    transcript.close()

    # Two turns: (1) read + send; (2) post-tool model response (no
    # tools) → natural exit. The second turn is required by the
    # corrected loop so the model can see the mailbox_send tool_result.
    assert result.turns == 2
    raw = transcript.path.read_text()
    assert "silent_turn_nudge_injected" not in raw


@pytest.mark.asyncio
async def test_nudge_does_not_fire_for_text_only_end_turn(
    repos: SqliteRepositories, object_store: Path
) -> None:
    """No tools at all → not the bug pattern → no nudge."""
    ctx = await _ctx(repos)
    adapter = FakeAdapter()
    adapter.push_turn(
        [
            ContentDelta(text="just chatting, all done"),
            TurnComplete(stop_reason="end_turn"),
        ]
    )
    transcript = TranscriptWriter(object_store, "wakeup-text")
    loop = build_single_candidate_loop(
        adapter, transcript,
        tool_registry=build_default_registry(),
        tool_context=ctx,
        allowed_tools=["mailbox_read"],
    )
    result = await loop.run(
        system_prompt="",
        initial_messages=[
            LyreMessage(
                role="user", content=[LyreContentBlock(type="text", text="hi")]
            )
        ],
    )
    transcript.close()
    assert result.turns == 1
    raw = transcript.path.read_text()
    assert "silent_turn_nudge_injected" not in raw


@pytest.mark.asyncio
async def test_nudge_caps_at_max_per_wakeup(
    repos: SqliteRepositories, object_store: Path
) -> None:
    """A stubborn model that ignores both nudges must not loop forever —
    after _MAX_SILENT_TURN_NUDGES nudges, the wakeup closes."""
    ctx = await _ctx(repos)
    adapter = FakeAdapter()
    # Turns 1, 2, 3: all info-only end_turn. Loop should nudge twice
    # (the cap), then close on the third silent end_turn.
    for tool_id in ("t1", "t2", "t3"):
        adapter.push_turn(
            [
                ToolUseComplete(id=tool_id, name="list_agents", input={}),
                TurnComplete(stop_reason="end_turn"),
            ]
        )
    transcript = TranscriptWriter(object_store, "wakeup-stubborn")
    loop = build_single_candidate_loop(
        adapter, transcript,
        tool_registry=build_default_registry(),
        tool_context=ctx,
        allowed_tools=["list_agents"],
    )
    result = await loop.run(
        system_prompt="",
        initial_messages=[
            LyreMessage(
                role="user", content=[LyreContentBlock(type="text", text="hi")]
            )
        ],
    )
    transcript.close()
    # 4 turns now: 3 scripted info-only turns (2 nudges fire), then a
    # 4th empty turn where FakeAdapter has no more script — the model
    # emits no-tool end_turn, hits the natural break, silent_close
    # fallback fires.
    assert result.turns == 4
    raw = transcript.path.read_text()
    # Both nudges fired during the 3 scripted turns; the 4th no-tool
    # turn is the natural exit point.
    assert raw.count("silent_turn_nudge_injected") == 2
