"""Tests for the wakeup end contract.

See ``docs/design/WAKEUP_END_CONTRACT.md``. Every wakeup must
terminate via an explicit ``end_wakeup(...)`` declaration; the
runtime maps the declaration onto ``tasks.status`` /
``wakeups.end_status`` deterministically and nudges once when the
agent forgets to declare.
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

# ---------------------------------------------------------------------------
# Test harness — minimal ToolContext + loop builder
# ---------------------------------------------------------------------------


async def _build_ctx(repos: SqliteRepositories) -> ToolContext:
    await repos.personas.upsert(
        Persona(name="worker", role_description="w", system_prompt="w")
    )
    await repos.agents.create(agent_id="worker", persona_name="worker")
    await repos.personas.upsert(
        Persona(name="owner", role_description="o", system_prompt="o")
    )
    await repos.agents.create(agent_id="owner", persona_name="owner")
    task_id = await repos.tasks.create(
        TaskSpec(agent_id="worker", goal="g", acceptance="a")
    )
    wakeup_id = await repos.wakeups.start(task_id, "worker")
    await repos.tasks.claim_lease(task_id, wakeup_id, duration_sec=600)
    return ToolContext(
        repos=repos, task_id=task_id, wakeup_id=wakeup_id,
        persona_name="worker", agent_id="worker",
    )


def _build_loop(
    adapter: FakeAdapter,
    transcript: TranscriptWriter,
    ctx: ToolContext,
    allowed_tools: list[str] | None = None,
) -> AgentLoop:
    return AgentLoop(
        candidates=[fake_entry(id="m")],
        adapter_for=lambda e: adapter,
        model_name_for=lambda e: e.id,
        transcript=transcript,
        tool_registry=build_default_registry(),
        tool_context=ctx,
        allowed_tools=allowed_tools or ["mailbox_send", "mailbox_read"],
    )


async def _run(loop: AgentLoop):
    return await loop.run(
        system_prompt="",
        initial_messages=[
            LyreMessage(role="user", content=[
                LyreContentBlock(type="text", text="go")
            ])
        ],
    )


# ---------------------------------------------------------------------------
# Status mapping: each declared status lands on the right columns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_done_declaration_maps_to_completed(
    repos: SqliteRepositories, object_store: Path,
) -> None:
    ctx = await _build_ctx(repos)
    transcript = TranscriptWriter(object_store, ctx.wakeup_id)
    adapter = FakeAdapter()
    adapter.push_done(summary="finished the task")

    loop = _build_loop(adapter, transcript, ctx)
    result = await _run(loop)
    transcript.close()

    assert result.declared_status == "done"
    assert result.declared_summary == "finished the task"
    assert result.status == "completed"
    # Failure / awaiting fields stay None for a clean done.
    assert result.declared_failure_reason is None
    assert result.declared_awaiting_on is None


@pytest.mark.asyncio
async def test_in_progress_declaration_maps_to_yielded(
    repos: SqliteRepositories, object_store: Path,
) -> None:
    ctx = await _build_ctx(repos)
    transcript = TranscriptWriter(object_store, ctx.wakeup_id)
    adapter = FakeAdapter()
    adapter.push_turn([
        ToolUseComplete(
            id="ew",
            name="end_wakeup",
            input={"status": "in_progress", "summary": "yielding mid-task"},
        ),
        Usage(input_tokens=10, output_tokens=5),
        TurnComplete(stop_reason="end_turn"),
    ])

    loop = _build_loop(adapter, transcript, ctx)
    result = await _run(loop)
    transcript.close()

    assert result.declared_status == "in_progress"
    assert result.status == "yielded"


@pytest.mark.asyncio
async def test_awaiting_subtask_carries_ref_through(
    repos: SqliteRepositories, object_store: Path,
) -> None:
    """Awaiting a subtask: the awaiting_ref pins which child task the
    next wakeup should be gated on. Both fields land on the result."""
    ctx = await _build_ctx(repos)
    transcript = TranscriptWriter(object_store, ctx.wakeup_id)
    adapter = FakeAdapter()
    adapter.push_awaiting(
        awaiting_on="subtask",
        summary="dispatched worker, waiting on child",
        awaiting_ref="child-task-123",
    )

    loop = _build_loop(adapter, transcript, ctx)
    result = await _run(loop)
    transcript.close()

    assert result.declared_status == "awaiting"
    assert result.declared_awaiting_on == "subtask"
    assert result.declared_awaiting_ref == "child-task-123"
    assert result.status == "awaiting"


@pytest.mark.asyncio
async def test_failed_with_reason_and_recoverable_flag(
    repos: SqliteRepositories, object_store: Path,
) -> None:
    ctx = await _build_ctx(repos)
    transcript = TranscriptWriter(object_store, ctx.wakeup_id)
    adapter = FakeAdapter()
    adapter.push_turn([
        ToolUseComplete(
            id="ew",
            name="end_wakeup",
            input={
                "status": "failed",
                "summary": "GitHub returned 500",
                "failure_reason": "provider_error",
                "recoverable": True,
            },
        ),
        Usage(input_tokens=10, output_tokens=5),
        TurnComplete(stop_reason="end_turn"),
    ])

    loop = _build_loop(adapter, transcript, ctx)
    result = await _run(loop)
    transcript.close()

    assert result.declared_status == "failed"
    assert result.declared_failure_reason == "provider_error"
    assert result.declared_recoverable is True
    assert result.status == "failed"


# ---------------------------------------------------------------------------
# Nudge: agent forgot to declare, runtime asks once, agent recovers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nudge_recovers_when_agent_forgets_end_wakeup(
    repos: SqliteRepositories, object_store: Path,
) -> None:
    """Turn 1: agent ends with text only and no end_wakeup. Runtime
    injects a nudge user message. Turn 2: agent calls end_wakeup,
    declaration is captured. Wakeup ends cleanly as `done`."""
    ctx = await _build_ctx(repos)
    transcript = TranscriptWriter(object_store, ctx.wakeup_id)
    adapter = FakeAdapter()
    # Turn 1: model forgot end_wakeup.
    adapter.push_turn([
        ContentDelta(text="all done!"),
        Usage(input_tokens=10, output_tokens=3),
        TurnComplete(stop_reason="end_turn"),
    ])
    # Turn 2: after the nudge, model declares.
    adapter.push_done(summary="declared after nudge")

    loop = _build_loop(adapter, transcript, ctx)
    result = await _run(loop)
    transcript.close()

    assert result.declared_status == "done"
    assert result.status == "completed"
    # Two turns: original + post-nudge.
    assert result.turns == 2


@pytest.mark.asyncio
async def test_hard_fallback_forces_silent_close_when_nudge_fails(
    repos: SqliteRepositories, object_store: Path,
) -> None:
    """Turn 1: no end_wakeup. Nudge. Turn 2: still no end_wakeup
    (empty turn). Runtime force-records failed/silent_close."""
    ctx = await _build_ctx(repos)
    transcript = TranscriptWriter(object_store, ctx.wakeup_id)
    adapter = FakeAdapter()
    # Both turns end without end_wakeup.
    adapter.push_turn([
        ContentDelta(text="meh"),
        TurnComplete(stop_reason="end_turn"),
    ])
    adapter.push_turn([
        ContentDelta(text="still nothing"),
        TurnComplete(stop_reason="end_turn"),
    ])

    loop = _build_loop(adapter, transcript, ctx)
    result = await _run(loop)
    transcript.close()

    assert result.declared_status == "failed"
    assert result.declared_failure_reason == "silent_close"
    assert result.declared_recoverable is False
    assert result.status == "silent_close"


# ---------------------------------------------------------------------------
# Trailing tool calls after end_wakeup are dropped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trailing_tool_calls_after_end_wakeup_dropped(
    repos: SqliteRepositories, object_store: Path,
) -> None:
    """end_wakeup is terminal. Any tool_use blocks emitted in the
    same turn AFTER end_wakeup must be dropped — no dispatch, no
    side effect — and surfaced as is_error tool_results to keep the
    conversation history consistent."""
    ctx = await _build_ctx(repos)
    transcript = TranscriptWriter(object_store, ctx.wakeup_id)
    adapter = FakeAdapter()
    adapter.push_turn([
        # The agent fires end_wakeup THEN tries another mailbox_send.
        ToolUseComplete(
            id="ew",
            name="end_wakeup",
            input={"status": "done", "summary": "done"},
        ),
        ToolUseComplete(
            id="should_be_dropped",
            name="mailbox_send",
            input={"to": "owner", "body": "this should not be sent"},
        ),
        TurnComplete(stop_reason="end_turn"),
    ])

    loop = _build_loop(adapter, transcript, ctx)
    result = await _run(loop)
    transcript.close()

    # Both tool_uses are recorded as "called" — but the dropped one's
    # outcome is is_error.
    assert result.declared_status == "done"
    # No mail in the outbox: the trailing mailbox_send was dropped.
    batch = await ctx.repos.outbox.dequeue_batch(limit=10)
    assert batch == [], (
        f"trailing tool calls must not produce real side effects; "
        f"found outbox rows: {batch}"
    )


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_awaiting_without_awaiting_on_errors(
    repos: SqliteRepositories, object_store: Path,
) -> None:
    """The handler validates: status='awaiting' requires awaiting_on
    (otherwise the scheduler has nothing to gate resume on)."""
    ctx = await _build_ctx(repos)
    transcript = TranscriptWriter(object_store, ctx.wakeup_id)
    adapter = FakeAdapter()
    # Turn 1: bad end_wakeup call — schema violation. Handler raises
    # ToolError, surfaces as is_error tool_result, the wakeup does NOT
    # terminate (declaration was never captured).
    adapter.push_turn([
        ToolUseComplete(
            id="bad",
            name="end_wakeup",
            input={"status": "awaiting", "summary": "want to wait"},
        ),
        TurnComplete(stop_reason="tool_use"),
    ])
    # Turn 2: model fixes the call.
    adapter.push_awaiting(awaiting_on="mail", summary="waiting for owner")

    loop = _build_loop(adapter, transcript, ctx)
    result = await _run(loop)
    transcript.close()

    assert result.declared_status == "awaiting"
    assert result.declared_awaiting_on == "mail"


@pytest.mark.asyncio
async def test_failed_without_reason_errors(
    repos: SqliteRepositories, object_store: Path,
) -> None:
    """Symmetric to awaiting: status='failed' requires failure_reason."""
    ctx = await _build_ctx(repos)
    transcript = TranscriptWriter(object_store, ctx.wakeup_id)
    adapter = FakeAdapter()
    adapter.push_turn([
        ToolUseComplete(
            id="bad",
            name="end_wakeup",
            input={"status": "failed", "summary": "broke"},
        ),
        TurnComplete(stop_reason="tool_use"),
    ])
    adapter.push_turn([
        ToolUseComplete(
            id="ok",
            name="end_wakeup",
            input={
                "status": "failed",
                "summary": "broke",
                "failure_reason": "tool_error",
                "recoverable": False,
            },
        ),
        TurnComplete(stop_reason="end_turn"),
    ])

    loop = _build_loop(adapter, transcript, ctx)
    result = await _run(loop)
    transcript.close()

    assert result.declared_status == "failed"
    assert result.declared_failure_reason == "tool_error"


@pytest.mark.asyncio
async def test_unknown_status_errors(
    repos: SqliteRepositories, object_store: Path,
) -> None:
    """status must be one of the four enum values."""
    ctx = await _build_ctx(repos)
    transcript = TranscriptWriter(object_store, ctx.wakeup_id)
    adapter = FakeAdapter()
    adapter.push_turn([
        ToolUseComplete(
            id="bad",
            name="end_wakeup",
            input={"status": "maybe?", "summary": "vague"},
        ),
        TurnComplete(stop_reason="tool_use"),
    ])
    adapter.push_done(summary="recovered")

    loop = _build_loop(adapter, transcript, ctx)
    result = await _run(loop)
    transcript.close()

    assert result.declared_status == "done"


# ---------------------------------------------------------------------------
# end_wakeup is auto-injected; persona allowlist doesn't gate it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_end_wakeup_callable_even_when_not_in_allowlist(
    repos: SqliteRepositories, object_store: Path,
) -> None:
    """end_wakeup is part of the runtime contract — every persona has
    access to it regardless of allowed_lyre_tools. Tests that build a
    minimal allowlist still get clean termination."""
    ctx = await _build_ctx(repos)
    transcript = TranscriptWriter(object_store, ctx.wakeup_id)
    adapter = FakeAdapter()
    adapter.push_done(summary="ok")

    # Empty allowlist — only end_wakeup auto-injected.
    loop = _build_loop(adapter, transcript, ctx, allowed_tools=[])
    result = await _run(loop)
    transcript.close()

    assert result.declared_status == "done"


# ---------------------------------------------------------------------------
# Identity preamble carries the contract paragraph
# ---------------------------------------------------------------------------


def test_identity_preamble_documents_end_wakeup_contract() -> None:
    """Regression: future edits to the identity preamble must not
    drop the end_wakeup contract section. Without it, the model
    doesn't know to call the tool and every wakeup hits the nudge."""
    from lyre.persistence.models import Persona
    from lyre.runtime.context import assemble_system_prompt

    persona = Persona(
        name="worker", role_description="w", system_prompt="w",
    )
    prompt = assemble_system_prompt(persona, agent_id="worker")
    # Key tokens from §6a of WAKEUP_END_CONTRACT.md
    assert "end_wakeup" in prompt
    assert "MUST terminate" in prompt or "REQUIRED" in prompt
    # The four statuses are documented.
    for status in ("done", "in_progress", "awaiting", "failed"):
        assert status in prompt
    # The ack-and-stop antipattern is called out so models don't fall
    # into "I'll look into it" → stop.
    assert "ack-and-stop" in prompt or "silent_close" in prompt
