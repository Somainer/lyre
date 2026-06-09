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


# --------------------------------------------------------------------------
# (C) Phase-2 recovery bound for BOOTSTRAP SINGLETONS (dispatcher etc.,
# parent_agent_id NULL). A deterministic setup failure would otherwise re-run
# forever, silently. On exceed: fail + escalate to OWNER, but NEVER archive —
# the singleton must stay reachable for the next mail to revive it (Phase 0).
# --------------------------------------------------------------------------
async def _singleton_task(repos: SqliteRepositories) -> str:
    # owner must exist so the no-parent fallback can route the escalation.
    await repos.agents.create("owner", "owner", parent_agent_id=None)
    await repos.agents.create("dispatcher-1", "dispatcher", parent_agent_id=None)
    tid = await repos.tasks.create(
        TaskSpec(agent_id="dispatcher-1", goal="g", acceptance="a")
    )
    # Mimic the dangling-lease recovery state: a started wakeup holds the lease.
    wk = await repos.wakeups.start(tid, "dispatcher", agent_id="dispatcher-1")
    await repos.tasks.claim_lease(tid, wk, duration_sec=1)
    return tid


@pytest.mark.asyncio
async def test_singleton_recovery_within_budget_allows_rerun(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    tid = await _singleton_task(repos)
    task = await repos.tasks.get(tid)
    sched = _scheduler(repos, tmp_path)  # default singleton_recovery_max=3

    for _ in range(3):
        assert await sched._singleton_recovery_exceeded(task) is False
    t = await repos.tasks.get(tid)
    assert t is not None and t.status == "in_progress"  # not failed within budget
    assert (t.metadata or {}).get("_recovery_attempts") == 3
    assert (await repos.agents.get("dispatcher-1")).status != "archived"


@pytest.mark.asyncio
async def test_singleton_recovery_exceeded_escalates_to_owner_no_archive(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    tid = await _singleton_task(repos)
    task = await repos.tasks.get(tid)
    sched = _scheduler(repos, tmp_path)

    results = [await sched._singleton_recovery_exceeded(task) for _ in range(4)]
    assert results == [False, False, False, True]  # cap=3 → escalate on the 4th

    t = await repos.tasks.get(tid)
    assert t is not None and t.status == "failed"  # terminal → no more recovery
    # CRITICAL: the dispatcher is NOT archived — unlike the ephemeral path. It
    # must stay reachable so the owner's next mail re-wakes it via Phase 0.
    agent = await repos.agents.get("dispatcher-1")
    assert agent is not None and agent.status != "archived"
    # The failure escalates to the owner (no-parent fallback) via task_terminated.
    rows = await repos.outbox.dequeue_batch(limit=10)
    esc = [
        r for r in rows
        if (r.payload.get("metadata") or {}).get("kind") == "task_terminated"
    ]
    assert len(esc) == 1
    assert esc[0].payload["recipient"] == "owner"
    assert (
        esc[0].payload["metadata"]["failure_reason"] == "singleton_recovery_exceeded"
    )


@pytest.mark.asyncio
async def test_singleton_recovery_skips_non_bootstrap_agent(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    # parent_agent_id set → spawned child, NOT a bootstrap singleton → unbounded
    # here (ordinary chaos recovery, exactly like a worker).
    await repos.agents.create(
        "worker-1", "worker", parent_agent_id="dispatcher-1"
    )
    tid = await repos.tasks.create(
        TaskSpec(agent_id="worker-1", goal="g", acceptance="a")
    )
    task = await repos.tasks.get(tid)
    sched = _scheduler(repos, tmp_path)
    assert await sched._singleton_recovery_exceeded(task) is False
    t = await repos.tasks.get(tid)
    assert (t.metadata or {}).get("_recovery_attempts") is None  # never bumped
