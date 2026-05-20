"""Tests for the Subagent + parent-wake mechanism.

Three concerns:
  - DAO (find_children, find_parents_ready_to_wake, wake_parent)
  - Tool (await_subagents)
  - End-to-end via Scheduler: dispatcher dispatches worker, awaits, worker
    completes, dispatcher auto-wakes and sees the result.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lyre.adapter.llm_adapter import (
    ContentDelta,
    ToolUseComplete,
    TurnComplete,
)
from lyre.config import Config
from lyre.persistence.models import Persona, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.context import assemble_initial_user_message
from lyre.runtime.tools import ToolContext, ToolError
from lyre.runtime.tools.tasks import AWAIT_SUBAGENTS
from lyre.scheduler.scheduler import Scheduler

from .fake_adapter import FakeAdapter
from .helpers import fake_entry, fake_registry

# ---------------------------------------------------------------------------
# DAO
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_children_returns_only_direct_children(
    repos: SqliteRepositories,
) -> None:
    await repos.personas.upsert(
        Persona(name="p", role_description="p", system_prompt="p")
    )
    parent = await repos.tasks.create(
        TaskSpec(persona_name="p", goal="parent", acceptance="a")
    )
    child1 = await repos.tasks.create(
        TaskSpec(persona_name="p", goal="c1", acceptance="a", parent_task_id=parent)
    )
    child2 = await repos.tasks.create(
        TaskSpec(persona_name="p", goal="c2", acceptance="a", parent_task_id=parent)
    )
    unrelated = await repos.tasks.create(
        TaskSpec(persona_name="p", goal="u", acceptance="a")
    )

    kids = await repos.tasks.find_children(parent)
    assert {c.id for c in kids} == {child1, child2}
    assert unrelated not in {c.id for c in kids}
    assert await repos.tasks.find_children(unrelated) == []


@pytest.mark.asyncio
async def test_find_parents_ready_to_wake_only_when_all_terminal(
    repos: SqliteRepositories,
) -> None:
    await repos.personas.upsert(
        Persona(name="p", role_description="p", system_prompt="p")
    )
    parent = await repos.tasks.create(
        TaskSpec(persona_name="p", goal="parent", acceptance="a")
    )
    # Parent must be in needs_input to be wake-able.
    await repos.tasks.update_status(parent, "needs_input")

    # Spawn 2 children.
    c1 = await repos.tasks.create(
        TaskSpec(persona_name="p", goal="c1", acceptance="a", parent_task_id=parent)
    )
    c2 = await repos.tasks.create(
        TaskSpec(persona_name="p", goal="c2", acceptance="a", parent_task_id=parent)
    )

    # No children terminal yet → not ready.
    ready = await repos.tasks.find_parents_ready_to_wake()
    assert ready == []

    # One terminal, one still pending → still not ready.
    await repos.tasks.update_status(c1, "completed")
    assert await repos.tasks.find_parents_ready_to_wake() == []

    # Both terminal → ready.
    await repos.tasks.update_status(c2, "failed")
    ready = await repos.tasks.find_parents_ready_to_wake()
    assert [p.id for p in ready] == [parent]


@pytest.mark.asyncio
async def test_needs_input_without_children_does_not_wake(
    repos: SqliteRepositories,
) -> None:
    """needs_input is also used for owner-blocker scenarios; those have no
    children and must NOT be auto-woken by the subagent mechanism."""
    await repos.personas.upsert(
        Persona(name="p", role_description="p", system_prompt="p")
    )
    task = await repos.tasks.create(
        TaskSpec(persona_name="p", goal="g", acceptance="a")
    )
    await repos.tasks.update_status(task, "needs_input")
    assert await repos.tasks.find_parents_ready_to_wake() == []


@pytest.mark.asyncio
async def test_wake_parent_transitions_to_pending(
    repos: SqliteRepositories,
) -> None:
    await repos.personas.upsert(
        Persona(name="p", role_description="p", system_prompt="p")
    )
    parent = await repos.tasks.create(
        TaskSpec(persona_name="p", goal="parent", acceptance="a")
    )
    child = await repos.tasks.create(
        TaskSpec(persona_name="p", goal="c", acceptance="a", parent_task_id=parent)
    )
    await repos.tasks.update_status(parent, "needs_input")
    await repos.tasks.update_status(child, "completed")

    assert await repos.tasks.wake_parent(parent) is True
    t = await repos.tasks.get(parent)
    assert t is not None
    assert t.status == "pending"
    assert t.lease_holder is None


@pytest.mark.asyncio
async def test_wake_parent_refuses_when_child_still_in_progress(
    repos: SqliteRepositories,
) -> None:
    await repos.personas.upsert(
        Persona(name="p", role_description="p", system_prompt="p")
    )
    parent = await repos.tasks.create(
        TaskSpec(persona_name="p", goal="parent", acceptance="a")
    )
    await repos.tasks.create(
        TaskSpec(persona_name="p", goal="c", acceptance="a", parent_task_id=parent)
    )
    await repos.tasks.update_status(parent, "needs_input")
    # child is still pending → must not wake.
    assert await repos.tasks.wake_parent(parent) is False
    t = await repos.tasks.get(parent)
    assert t is not None and t.status == "needs_input"


# ---------------------------------------------------------------------------
# Tool: await_subagents
# ---------------------------------------------------------------------------


@pytest.fixture
async def parent_ctx(repos: SqliteRepositories) -> ToolContext:
    await repos.personas.upsert(
        Persona(name="dispatcher", role_description="l", system_prompt="l")
    )
    await repos.personas.upsert(
        Persona(name="worker", role_description="w", system_prompt="w")
    )
    parent = await repos.tasks.create(
        TaskSpec(persona_name="dispatcher", goal="g", acceptance="a")
    )
    wakeup = await repos.wakeups.start(parent, "dispatcher")
    await repos.tasks.claim_lease(parent, wakeup, duration_sec=600)
    return ToolContext(
        repos=repos, task_id=parent, wakeup_id=wakeup, persona_name="dispatcher",
    )


@pytest.mark.asyncio
async def test_await_subagents_errors_without_children(
    parent_ctx: ToolContext,
) -> None:
    with pytest.raises(ToolError, match="no subagent children"):
        await AWAIT_SUBAGENTS.handler(parent_ctx, {})


@pytest.mark.asyncio
async def test_await_subagents_marks_needs_input_when_pending(
    parent_ctx: ToolContext,
) -> None:
    child = await parent_ctx.repos.tasks.create(
        TaskSpec(
            persona_name="worker", goal="c", acceptance="a",
            parent_task_id=parent_ctx.task_id,
        )
    )
    res = await AWAIT_SUBAGENTS.handler(parent_ctx, {})
    assert res["status"] == "awaiting"
    waiting_ids = [c["id"] for c in res["waiting_for"]]
    assert waiting_ids == [child]

    parent = await parent_ctx.repos.tasks.get(parent_ctx.task_id)
    assert parent is not None
    assert parent.status == "needs_input"
    assert parent.checkpoint is not None
    assert parent.checkpoint["awaiting_children"] == [child]


@pytest.mark.asyncio
async def test_await_subagents_returns_all_done_when_already_terminal(
    parent_ctx: ToolContext,
) -> None:
    """If a child finishes before await_subagents is called, the tool
    returns immediately without yielding."""
    child = await parent_ctx.repos.tasks.create(
        TaskSpec(
            persona_name="worker", goal="c", acceptance="a",
            parent_task_id=parent_ctx.task_id,
        )
    )
    await parent_ctx.repos.tasks.update_status(child, "completed")

    res = await AWAIT_SUBAGENTS.handler(parent_ctx, {})
    assert res["status"] == "all_done"

    parent = await parent_ctx.repos.tasks.get(parent_ctx.task_id)
    assert parent is not None
    assert parent.status == "in_progress"  # untouched


# ---------------------------------------------------------------------------
# assemble_initial_user_message injects children info
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_message_includes_children_status(
    repos: SqliteRepositories,
) -> None:
    await repos.personas.upsert(
        Persona(name="dispatcher", role_description="l", system_prompt="l")
    )
    await repos.personas.upsert(
        Persona(name="worker", role_description="w", system_prompt="w")
    )
    parent_id = await repos.tasks.create(
        TaskSpec(persona_name="dispatcher", goal="parent goal", acceptance="parent ok")
    )
    child_id = await repos.tasks.create(
        TaskSpec(
            persona_name="worker", goal="c", acceptance="a",
            parent_task_id=parent_id,
        )
    )
    await repos.tasks.update_status(child_id, "completed")

    parent = await repos.tasks.get(parent_id)
    assert parent is not None
    msg = await assemble_initial_user_message(parent, tasks_repo=repos.tasks)
    text = msg.content[0].text or ""
    assert "parent goal" in text
    assert child_id in text
    assert "status=completed" in text


@pytest.mark.asyncio
async def test_user_message_without_repo_omits_children_section(
    repos: SqliteRepositories,
) -> None:
    await repos.personas.upsert(
        Persona(name="dispatcher", role_description="l", system_prompt="l")
    )
    parent_id = await repos.tasks.create(
        TaskSpec(persona_name="dispatcher", goal="g", acceptance="a")
    )
    parent = await repos.tasks.get(parent_id)
    assert parent is not None
    msg = await assemble_initial_user_message(parent)
    text = msg.content[0].text or ""
    assert "subagent" not in text


# ---------------------------------------------------------------------------
# End-to-end through Scheduler
# ---------------------------------------------------------------------------


def _config(tmp_path: Path) -> Config:
    mem = tmp_path / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    return Config(
        db_path=tmp_path / "x.db",
        object_store_path=tmp_path / "objstore",
        memory_path=mem,
        anthropic_api_key="fake",
        anthropic_base_url=None,
        default_model="m",
        model_override=None,
    )


@pytest.mark.asyncio
async def test_dispatcher_dispatches_worker_awaits_and_wakes(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    """Full control-chain: dispatcher dispatch_task → await_subagents → end_turn
    → worker runs and completes → scheduler auto-wakes dispatcher → dispatcher sees
    child status in its new wakeup."""
    cfg = _config(tmp_path)
    cfg.object_store_path.mkdir(parents=True, exist_ok=True)

    await repos.personas.upsert(
        Persona(
            name="dispatcher", role_description="dispatcher", system_prompt="lead",
            allowed_lyre_tools=["dispatch_task", "await_subagents", "mailbox_send"],
            model_preference={
                "tier": "flagship", "requires": ["tool_use"], "prefer": [],
            },
            needs_worktree=False,
        )
    )
    await repos.personas.upsert(
        Persona(
            name="worker", role_description="worker", system_prompt="work",
            allowed_lyre_tools=["mailbox_send"],
            model_preference={
                "tier": "workhorse", "requires": ["tool_use"], "prefer": [],
            },
            needs_worktree=False,
        )
    )
    # Post-A3: dispatch_task / mailbox routing needs real agents. Seed an
    # agent for each persona (id == persona name keeps the scripted
    # FakeAdapter inputs unchanged).
    await repos.agents.create(agent_id="dispatcher", persona_name="dispatcher")
    await repos.agents.create(agent_id="worker", persona_name="worker")
    await repos.agents.create(agent_id="owner", persona_name="dispatcher")
    await repos.mailbox.ensure_mailbox("owner")

    parent_task = await repos.tasks.create(
        TaskSpec(
            agent_id="dispatcher", goal="get worker to ping owner",
            acceptance="owner mailbox has the ping",
        )
    )

    # ONE FakeAdapter per persona — it dispenses queued turns across
    # multiple stream_turn calls. Leader's queue has its 2 wakeups
    # concatenated (the queue persists across wakeups since we keep the same
    # adapter instance).
    dispatcher_adapter = FakeAdapter()
    # Wakeup 1: dispatch + await + end_turn
    dispatcher_adapter.push_turn([
        ToolUseComplete(
            id="t1", name="dispatch_task",
            input={
                "persona": "worker",
                "goal": "send mailbox ping to owner",
                "acceptance": "ping sent",
            },
        ),
        TurnComplete(stop_reason="tool_use"),
    ])
    dispatcher_adapter.push_turn([
        ToolUseComplete(id="t2", name="await_subagents", input={}),
        TurnComplete(stop_reason="tool_use"),
    ])
    dispatcher_adapter.push_turn([
        ContentDelta(text="dispatcher yields"),
        TurnComplete(stop_reason="end_turn"),
    ])
    # Wakeup 2 (resume): report + end_turn
    dispatcher_adapter.push_turn([
        ToolUseComplete(
            id="r1", name="mailbox_send",
            input={"to": "owner", "body": "all tasks complete"},
        ),
        TurnComplete(stop_reason="tool_use"),
    ])
    dispatcher_adapter.push_turn([
        ContentDelta(text="reported"),
        TurnComplete(stop_reason="end_turn"),
    ])

    worker_adapter = FakeAdapter()
    worker_adapter.push_turn([
        ToolUseComplete(
            id="w1", name="mailbox_send",
            input={"to": "owner", "body": "ping from worker"},
        ),
        TurnComplete(stop_reason="tool_use"),
    ])
    worker_adapter.push_turn([
        ContentDelta(text="done"),
        TurnComplete(stop_reason="end_turn"),
    ])

    # Route by ModelEntry.tier: dispatcher prefers flagship, worker workhorse.
    def adapter_for(entry):
        if entry.tier == "flagship":
            return dispatcher_adapter
        return worker_adapter

    scheduler = Scheduler(
        repos, cfg, poll_interval_s=0.05,
        registry=fake_registry(
            fake_entry(id="m-flagship", tier="flagship"),
            fake_entry(id="m-workhorse", tier="workhorse"),
        ),
        adapter_for_test=adapter_for,
    )

    # Tick 1: scheduler picks up pending parent (dispatcher). Leader dispatches
    # worker, awaits, end_turn → dispatcher is needs_input.
    await scheduler._tick()
    p = await repos.tasks.get(parent_task)
    assert p is not None
    assert p.status == "needs_input"
    assert p.checkpoint is not None
    assert "awaiting_children" in p.checkpoint

    # Tick 2: scheduler picks up pending worker (the child).
    await scheduler._tick()
    children = await repos.tasks.find_children(parent_task)
    assert len(children) == 1
    worker_task = children[0]
    assert worker_task.status == "completed"

    # Tick 3: scheduler sees parent ready_to_wake, transitions to pending.
    await scheduler._tick()
    p = await repos.tasks.get(parent_task)
    assert p is not None
    # After phase 1 (wake) + phase 3 (run) we should be either pending OR
    # completed depending on whether the same tick also ran it. The wake
    # phase only transitions; the run phase picks up pending. In this
    # scheduler design they happen within the same tick.
    assert p.status in ("pending", "completed"), (
        f"expected pending/completed, got {p.status}"
    )

    # Tick 4 (or already done): dispatcher's resume wakeup runs and reports.
    if p.status == "pending":
        await scheduler._tick()
        p = await repos.tasks.get(parent_task)

    assert p is not None
    assert p.status == "completed"

    # Owner mailbox got BOTH messages (worker's ping + dispatcher's report — both
    # via outbox, but we haven't run the dispatcher; they should be in the
    # outbox rows).
    rows = await repos.outbox.dequeue_batch(limit=10)
    bodies = {r.payload["body"] for r in rows if r.kind == "mailbox_send"}
    assert "ping from worker" in bodies
    assert "all tasks complete" in bodies

    # Wake-resumed dispatcher wakeup's user message must include the child status.
    # dispatcher_adapter.calls accumulates messages from each stream_turn; the
    # first call of the SECOND wakeup is the one we want — after the initial
    # 3 turns of wakeup 1.
    assert len(dispatcher_adapter.calls) >= 4, (
        f"expected ≥4 dispatcher stream_turn calls, got {len(dispatcher_adapter.calls)}"
    )
    resume_call = dispatcher_adapter.calls[3]  # first turn of wakeup 2
    text_blocks = [
        blk.text or ""
        for m in resume_call["messages"]
        for blk in m.content
        if blk.type == "text"
    ]
    assert any(worker_task.id in t for t in text_blocks), (
        f"child task_id missing from resume context: {text_blocks!r}"
    )
    assert any("status=completed" in t for t in text_blocks)
