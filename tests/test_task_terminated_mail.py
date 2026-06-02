"""PR3: task_terminated mail — the OTP monitor / DOWN analogue.

When a task reaches a terminal state the scheduler enqueues a structured
``mailbox_send`` to the supervising agent (the parent task's agent, else owner),
so supervisors react to child terminations through the mailbox instead of
polling. Deliberately SUPPRESSED for fan-in members (PR2's barrier owns them)
and auto-dispatched inbox tasks, and for top-level tasks only fires on FAILURE
(success already replies to the owner via the agent's own mail).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lyre.adapter.llm_adapter import ContentDelta, TurnComplete
from lyre.config import Config
from lyre.outbox.dispatcher import OutboxDispatcher
from lyre.persistence.models import Persona, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.scheduler.scheduler import Scheduler

from .fake_adapter import FakeAdapter
from .helpers import fake_entry, fake_registry


def _scheduler(repos: SqliteRepositories, tmp_path: Path, *, auto_wake: bool = False) -> Scheduler:
    mem = tmp_path / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    cfg = Config(
        db_path=tmp_path / "x.db",
        object_store_path=tmp_path / "obj",
        memory_path=mem,
        anthropic_api_key="fake",
        anthropic_base_url=None,
        default_model="m",
    )
    cfg.object_store_path.mkdir(parents=True, exist_ok=True)
    return Scheduler(
        repos,
        cfg,
        registry=fake_registry(fake_entry(id="fake.workhorse", tier="workhorse")),
        adapter_for_test=lambda e: FakeAdapter(),
        auto_wake_on_mail=auto_wake,
    )


async def _team(repos: SqliteRepositories) -> None:
    await repos.agents.create("owner", "owner", parent_agent_id=None)
    await repos.agents.create("dispatcher-1", "dispatcher", parent_agent_id="owner")
    await repos.agents.create("worker-1", "worker", parent_agent_id="dispatcher-1")


async def _child(repos: SqliteRepositories, *, parent_task: str | None, metadata: dict | None = None):
    """A worker-1 task (child of `parent_task` if given) + a real wakeup row
    (so the outbox wakeup_id FK is satisfied). Returns (task, wakeup_id)."""
    tid = await repos.tasks.create(
        TaskSpec(agent_id="worker-1", goal="count files", acceptance="a",
                 parent_task_id=parent_task, metadata=metadata)
    )
    wk = await repos.wakeups.start(tid, "worker-1", agent_id="worker-1")
    task = await repos.tasks.get(tid)
    return task, wk


async def _terminated_mail(repos: SqliteRepositories, recipient: str) -> list:
    return [
        m for m in await repos.mailbox.read_messages(recipient)
        if (m.metadata or {}).get("kind") == "task_terminated"
    ]


@pytest.mark.asyncio
async def test_child_completion_notifies_parent_agent(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    await _team(repos)
    t_p = await repos.tasks.create(TaskSpec(agent_id="dispatcher-1", goal="p", acceptance="a"))
    child, wk = await _child(repos, parent_task=t_p)
    sched = _scheduler(repos, tmp_path)

    await sched._emit_task_terminated_mail(
        child, wk, "completed", summary="61 files", failure_reason=None, transcript_uri="obj://x"
    )
    await OutboxDispatcher(repos).tick()

    tt = await _terminated_mail(repos, "dispatcher-1")
    assert len(tt) == 1
    m = tt[0]
    assert m.sender == "system:supervisor"
    assert m.urgency == "normal"
    assert m.metadata["outcome"] == "completed"
    assert m.metadata["task_id"] == child.id
    assert m.metadata["transcript_uri"] == "obj://x"


@pytest.mark.asyncio
async def test_child_failure_is_high_urgency_with_reason(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    await _team(repos)
    t_p = await repos.tasks.create(TaskSpec(agent_id="dispatcher-1", goal="p", acceptance="a"))
    child, wk = await _child(repos, parent_task=t_p)
    sched = _scheduler(repos, tmp_path)

    await sched._emit_task_terminated_mail(
        child, wk, "failed", summary="boom", failure_reason="RuntimeError", transcript_uri=None
    )
    await OutboxDispatcher(repos).tick()

    tt = await _terminated_mail(repos, "dispatcher-1")
    assert len(tt) == 1
    assert tt[0].urgency == "high"
    assert tt[0].metadata["failure_reason"] == "RuntimeError"


@pytest.mark.asyncio
async def test_top_level_failure_notifies_owner(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    await _team(repos)
    child, wk = await _child(repos, parent_task=None)  # top-level
    sched = _scheduler(repos, tmp_path)

    await sched._emit_task_terminated_mail(
        child, wk, "failed", summary="x", failure_reason="E", transcript_uri=None
    )
    await OutboxDispatcher(repos).tick()
    assert len(await _terminated_mail(repos, "owner")) == 1


@pytest.mark.asyncio
async def test_top_level_completion_is_suppressed(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    await _team(repos)
    child, wk = await _child(repos, parent_task=None)
    sched = _scheduler(repos, tmp_path)
    await sched._emit_task_terminated_mail(
        child, wk, "completed", summary="x", failure_reason=None, transcript_uri=None
    )
    # No noise for a successful top-level task — the agent's own reply covers it.
    assert await repos.outbox.dequeue_batch(limit=10) == []


@pytest.mark.asyncio
async def test_fan_in_member_is_suppressed(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    await _team(repos)
    t_p = await repos.tasks.create(TaskSpec(agent_id="dispatcher-1", goal="p", acceptance="a"))
    child, wk = await _child(
        repos, parent_task=t_p, metadata={"fan_in_group": "fanin-x", "leg_key": 0}
    )
    sched = _scheduler(repos, tmp_path)
    await sched._emit_task_terminated_mail(
        child, wk, "completed", summary="x", failure_reason=None, transcript_uri=None
    )
    # The fan-in barrier (PR2) owns this child; a notice here would prematurely
    # auto-wake the coordinator on the first leg.
    assert await repos.outbox.dequeue_batch(limit=10) == []


@pytest.mark.asyncio
async def test_auto_dispatched_task_is_suppressed(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    await _team(repos)
    child, wk = await _child(
        repos, parent_task=None, metadata={"auto_dispatched": True}
    )
    sched = _scheduler(repos, tmp_path)
    # Even on failure: an auto inbox-check task is internal bookkeeping.
    await sched._emit_task_terminated_mail(
        child, wk, "failed", summary="x", failure_reason="E", transcript_uri=None
    )
    assert await repos.outbox.dequeue_batch(limit=10) == []


@pytest.mark.asyncio
async def test_archived_parent_falls_back_to_owner(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    await _team(repos)
    t_p = await repos.tasks.create(TaskSpec(agent_id="dispatcher-1", goal="p", acceptance="a"))
    child, wk = await _child(repos, parent_task=t_p)
    await repos.agents.archive("dispatcher-1")  # supervisor died
    sched = _scheduler(repos, tmp_path)

    await sched._emit_task_terminated_mail(
        child, wk, "completed", summary="x", failure_reason=None, transcript_uri=None
    )
    await OutboxDispatcher(repos).tick()
    # Signal isn't lost — it routes to the owner instead of the dead supervisor.
    assert len(await _terminated_mail(repos, "owner")) == 1
    assert await _terminated_mail(repos, "dispatcher-1") == []


@pytest.mark.asyncio
async def test_external_id_deterministic_and_idempotent(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    await _team(repos)
    t_p = await repos.tasks.create(TaskSpec(agent_id="dispatcher-1", goal="p", acceptance="a"))
    child, wk = await _child(repos, parent_task=t_p)
    sched = _scheduler(repos, tmp_path)

    # Fire twice (retried tick / recovery): exactly one mail survives.
    await sched._emit_task_terminated_mail(
        child, wk, "completed", summary="x", failure_reason=None, transcript_uri=None
    )
    await sched._emit_task_terminated_mail(
        child, wk, "completed", summary="x", failure_reason=None, transcript_uri=None
    )
    await OutboxDispatcher(repos).tick()
    tt = await _terminated_mail(repos, "dispatcher-1")
    assert len(tt) == 1
    assert tt[0].external_id == f"task_terminated:{child.id}"


@pytest.mark.asyncio
async def test_non_terminal_and_none_emit_nothing(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    await _team(repos)
    t_p = await repos.tasks.create(TaskSpec(agent_id="dispatcher-1", goal="p", acceptance="a"))
    child, wk = await _child(repos, parent_task=t_p)
    sched = _scheduler(repos, tmp_path)

    await sched._emit_task_terminated_mail(
        child, wk, "needs_input", summary=None, failure_reason=None, transcript_uri=None
    )
    await sched._emit_task_terminated_mail(
        None, wk, "failed", summary=None, failure_reason=None, transcript_uri=None
    )
    assert await repos.outbox.dequeue_batch(limit=10) == []


@pytest.mark.asyncio
async def test_completed_child_wakeup_emits_via_tick(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    """Integration: a real child wakeup that completes fires the mail from the
    end-of-wakeup call site (pins the wiring, not just the helper)."""
    await repos.personas.upsert(
        Persona(
            name="worker",
            role_description="w",
            system_prompt="w",
            allowed_lyre_tools=["mailbox_send"],
            model_preference={"tier": "workhorse", "requires": ["tool_use"], "prefer": []},
        )
    )
    await _team(repos)
    t_p = await repos.tasks.create(TaskSpec(agent_id="dispatcher-1", goal="p", acceptance="a"))
    await repos.tasks.update_status(t_p, "in_progress")  # keep _tick from running it
    t_c = await repos.tasks.create(
        TaskSpec(agent_id="worker-1", goal="do work", acceptance="a", parent_task_id=t_p)
    )

    fake = FakeAdapter()
    fake.push_turn([ContentDelta(text="done"), TurnComplete(stop_reason="end_turn")])
    mem = tmp_path / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    cfg = Config(
        db_path=tmp_path / "x.db",
        object_store_path=tmp_path / "obj",
        memory_path=mem,
        anthropic_api_key="fake",
        anthropic_base_url=None,
        default_model="m",
    )
    cfg.object_store_path.mkdir(parents=True, exist_ok=True)
    sched = Scheduler(
        repos,
        cfg,
        registry=fake_registry(fake_entry(id="fake.workhorse", tier="workhorse")),
        adapter_for_test=lambda e: fake,
        auto_wake_on_mail=False,
    )

    await sched._tick()
    assert (await repos.tasks.get(t_c)).status == "completed"

    await OutboxDispatcher(repos).tick()
    tt = await _terminated_mail(repos, "dispatcher-1")
    assert len(tt) == 1 and tt[0].metadata["outcome"] == "completed"
