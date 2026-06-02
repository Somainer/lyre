"""PR4c: supervisor hardening — reserved singleton slot + Phase-2 crash-loop bound.

(1) Reserved slot: under subprocess saturation by spawned ephemeral children,
    a bootstrap singleton (owner-facing dispatcher, parent_agent_id NULL) is
    dispatched first and a slot is held in reserve so children can't occupy
    every slot.
(2) Phase-2 bound: an ephemeral child in a raw-SIGKILL crash-loop (recovered by
    lease expiry, so it never reaches a terminal status the reaper can see) is
    counted against restart intensity; on exceed it's failed + escalated +
    reclaimed instead of re-run forever.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lyre.config import Config
from lyre.persistence.models import TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.scheduler.scheduler import Scheduler

from .fake_adapter import FakeAdapter
from .helpers import fake_entry, fake_registry


def _scheduler(
    repos: SqliteRepositories,
    tmp_path: Path,
    *,
    subprocess: bool = False,
    max_concurrent: int = 4,
) -> Scheduler:
    mem = tmp_path / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    cfg = Config(
        db_path=tmp_path / "x.db",
        object_store_path=tmp_path / "obj",
        memory_path=mem,
        anthropic_api_key="fake",
        anthropic_base_url=None,
        default_model="m",
        max_concurrent_tasks=max_concurrent,
    )
    cfg.object_store_path.mkdir(parents=True, exist_ok=True)
    return Scheduler(
        repos,
        cfg,
        registry=fake_registry(fake_entry()),
        adapter_for_test=lambda e: FakeAdapter(),
        auto_wake_on_mail=False,
        spawn_subprocess=subprocess,
    )


def _record_dispatches(sched: Scheduler) -> list[str]:
    """Replace _run_task with a recorder so we test the dispatch DECISION
    (which tasks, in what order) without spawning real subprocesses."""
    dispatched: list[str] = []

    async def _rec(task_id: str) -> None:
        dispatched.append(task_id)

    sched._run_task = _rec  # type: ignore[method-assign]
    return dispatched


# --------------------------------------------------------------------------
# (1) Reserved singleton slot
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_nonsingletons_capped_at_max_minus_one(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    # 3 ephemeral-ish children, no singleton pending, max_concurrent=3,
    # subprocess mode → one slot is reserved, so only 2 children dispatch.
    for i in (1, 2, 3):
        await repos.agents.create(f"worker-{i}", "worker", parent_agent_id="coordinator-1")
        await repos.tasks.create(TaskSpec(agent_id=f"worker-{i}", goal="g", acceptance="a"))
    sched = _scheduler(repos, tmp_path, subprocess=True, max_concurrent=3)
    dispatched = _record_dispatches(sched)
    await sched._tick()
    assert len(dispatched) == 2  # max_concurrent - 1; one slot reserved


@pytest.mark.asyncio
async def test_singleton_prioritised_under_saturation(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    await repos.agents.create("dispatcher", "dispatcher", parent_agent_id=None)  # singleton
    await repos.agents.create("worker-1", "worker", parent_agent_id="dispatcher")
    child = await repos.tasks.create(TaskSpec(agent_id="worker-1", goal="g", acceptance="a"))
    boss = await repos.tasks.create(TaskSpec(agent_id="dispatcher", goal="owner", acceptance="a"))

    sched = _scheduler(repos, tmp_path, subprocess=True, max_concurrent=2)
    # Simulate one slot already busy with a running child subprocess.
    sched._active_subprocesses["busy-child"] = (None, None)  # type: ignore[assignment]
    dispatched = _record_dispatches(sched)
    await sched._tick()
    # Only one free slot, and it goes to the bootstrap singleton — not the child.
    assert dispatched == [boss]
    assert child not in dispatched


@pytest.mark.asyncio
async def test_inline_mode_dispatches_without_reserve(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    # Inline mode is serial (no subprocess reserve); a lone child still runs.
    await repos.agents.create("worker-1", "worker", parent_agent_id="coordinator-1")
    child = await repos.tasks.create(TaskSpec(agent_id="worker-1", goal="g", acceptance="a"))
    sched = _scheduler(repos, tmp_path, subprocess=False)
    dispatched = _record_dispatches(sched)
    await sched._tick()
    assert dispatched == [child]


# --------------------------------------------------------------------------
# (2) Phase-2 crash-loop intensity bound
# --------------------------------------------------------------------------
async def _ephemeral_task(
    repos: SqliteRepositories, *, max_restarts: int, parent: str = "coordinator-1"
) -> str:
    await repos.agents.create(
        "reviewer-1",
        "reviewer",
        parent_agent_id=parent,
        metadata={
            "supervision": {
                "ephemeral": True,
                "restart": "transient",
                "max_restarts": max_restarts,
                "max_seconds": 60,
            }
        },
    )
    return await repos.tasks.create(
        TaskSpec(agent_id="reviewer-1", goal="g", acceptance="a")
    )


@pytest.mark.asyncio
async def test_phase2_recovery_within_budget_allows_rerun(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    await repos.agents.create("coordinator-1", "dispatcher", parent_agent_id="owner")
    tid = await _ephemeral_task(repos, max_restarts=2)
    task = await repos.tasks.get(tid)
    sched = _scheduler(repos, tmp_path)

    assert await sched._ephemeral_recovery_exceeded(task) is False  # within → re-run
    state = await repos.supervision.get("reviewer-1")
    assert state is not None and state.restart_count == 1


@pytest.mark.asyncio
async def test_phase2_recovery_exceeded_fails_escalates_reclaims(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    await repos.agents.create("coordinator-1", "dispatcher", parent_agent_id="owner")
    tid = await _ephemeral_task(repos, max_restarts=1)
    task = await repos.tasks.get(tid)
    sched = _scheduler(repos, tmp_path)

    assert await sched._ephemeral_recovery_exceeded(task) is False  # 1st: within
    assert await sched._ephemeral_recovery_exceeded(task) is True   # 2nd: exceeded

    t = await repos.tasks.get(tid)
    assert t is not None and t.status == "failed"          # terminal → not re-recovered
    agent = await repos.agents.get("reviewer-1")
    assert agent is not None and agent.status == "archived"  # reaper won't restart it
    state = await repos.supervision.get("reviewer-1")
    assert state is not None and state.escalated_at is not None
    esc = [
        m for m in await repos.mailbox.read_messages("coordinator-1")
        if (m.metadata or {}).get("kind") == "supervision_escalation"
    ]
    assert len(esc) == 1 and esc[0].urgency == "high"


@pytest.mark.asyncio
async def test_phase2_non_ephemeral_task_is_not_bounded(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    await repos.agents.create("worker-1", "worker", parent_agent_id="coordinator-1")
    tid = await repos.tasks.create(TaskSpec(agent_id="worker-1", goal="g", acceptance="a"))
    task = await repos.tasks.get(tid)
    sched = _scheduler(repos, tmp_path)

    assert await sched._ephemeral_recovery_exceeded(task) is False  # ordinary recovery
    assert await repos.supervision.get("worker-1") is None          # never bumped
