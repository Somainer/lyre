"""PR4b: the supervisor — one_for_one restart + restart-intensity + escalation.

The reaper (Phase 0.8) now applies an OTP restart policy to a reapable
ephemeral agent's latest outcome: restart the leg one-for-one (bounded by
max_restarts within max_seconds), escalate to the supervisor when that bound is
exceeded, or reclaim. PR3's task_terminated is suppressed for ephemeral agents
(the reaper owns their lifecycle). Reaper mails are inserted directly (the
scheduler isn't in a wakeup), so no OutboxDispatcher is needed in these tests.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lyre.config import Config
from lyre.persistence.models import TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.scheduler.scheduler import Scheduler

from .fake_adapter import FakeAdapter
from .helpers import fake_entry, fake_registry


def _scheduler(repos: SqliteRepositories, tmp_path: Path) -> Scheduler:
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
        registry=fake_registry(fake_entry()),
        adapter_for_test=lambda e: FakeAdapter(),
        auto_wake_on_mail=False,
    )


async def _ephemeral(
    repos: SqliteRepositories,
    agent_id: str,
    *,
    restart: str,
    max_restarts: int = 3,
    max_seconds: int = 60,
    parent: str = "coordinator-1",
) -> None:
    await repos.agents.create(
        agent_id,
        "reviewer",
        parent_agent_id=parent,
        metadata={
            "supervision": {
                "ephemeral": True,
                "restart": restart,
                "max_restarts": max_restarts,
                "max_seconds": max_seconds,
            }
        },
    )


async def _task(repos: SqliteRepositories, agent_id: str, *, status: str, metadata=None) -> str:
    tid = await repos.tasks.create(
        TaskSpec(agent_id=agent_id, goal="review", acceptance="a", metadata=metadata)
    )
    if status != "pending":
        await repos.tasks.update_status(tid, status)
    return tid


async def _archived(repos: SqliteRepositories, agent_id: str) -> bool:
    a = await repos.agents.get(agent_id)
    return a is not None and a.status == "archived"


async def _supervision_mail(repos: SqliteRepositories, recipient: str, kind: str) -> list:
    return [
        m for m in await repos.mailbox.read_messages(recipient)
        if (m.metadata or {}).get("kind") == kind
    ]


@pytest.mark.asyncio
async def test_transient_restarts_failed_child(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    await repos.agents.create("coordinator-1", "dispatcher", parent_agent_id="owner")
    await _ephemeral(repos, "reviewer-1", restart="transient")
    failed = await _task(repos, "reviewer-1", status="failed")

    await _scheduler(repos, tmp_path)._reap_ephemeral_agents()

    assert not await _archived(repos, "reviewer-1")  # restarted, not reclaimed
    latest = await repos.tasks.find_latest_task_for_agent("reviewer-1")
    assert latest is not None and latest.id != failed and latest.status == "pending"
    state = await repos.supervision.get("reviewer-1")
    assert state is not None and state.restart_count == 1


@pytest.mark.asyncio
async def test_transient_does_not_restart_completed_child(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    await repos.agents.create("coordinator-1", "dispatcher", parent_agent_id="owner")
    await _ephemeral(repos, "reviewer-1", restart="transient")
    await _task(repos, "reviewer-1", status="completed")

    await _scheduler(repos, tmp_path)._reap_ephemeral_agents()
    assert await _archived(repos, "reviewer-1")  # clean completion → reclaimed
    assert await repos.supervision.get("reviewer-1") is None  # never bumped


@pytest.mark.asyncio
async def test_permanent_restarts_completed_child(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    await repos.agents.create("coordinator-1", "dispatcher", parent_agent_id="owner")
    await _ephemeral(repos, "reviewer-1", restart="permanent")
    await _task(repos, "reviewer-1", status="completed")

    await _scheduler(repos, tmp_path)._reap_ephemeral_agents()
    assert not await _archived(repos, "reviewer-1")
    latest = await repos.tasks.find_latest_task_for_agent("reviewer-1")
    assert latest is not None and latest.status == "pending"


@pytest.mark.asyncio
async def test_temporary_failed_child_emits_failure_notice_and_reclaims(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    await repos.agents.create("coordinator-1", "dispatcher", parent_agent_id="owner")
    await _ephemeral(repos, "reviewer-1", restart="temporary")
    await _task(repos, "reviewer-1", status="failed")

    await _scheduler(repos, tmp_path)._reap_ephemeral_agents()
    assert await _archived(repos, "reviewer-1")
    # The failure isn't lost: the supervisor gets a high-urgency notice.
    notices = await _supervision_mail(repos, "coordinator-1", "supervision_failure")
    assert len(notices) == 1 and notices[0].urgency == "high"


@pytest.mark.asyncio
async def test_restart_preserves_fan_in_metadata(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    await repos.agents.create("coordinator-1", "dispatcher", parent_agent_id="owner")
    await _ephemeral(repos, "reviewer-1", restart="transient")
    await _task(
        repos, "reviewer-1", status="failed",
        metadata={"fan_in_group": "fanin-x", "leg_key": 2},
    )
    await _scheduler(repos, tmp_path)._reap_ephemeral_agents()
    latest = await repos.tasks.find_latest_task_for_agent("reviewer-1")
    assert latest is not None and latest.status == "pending"
    assert (latest.metadata or {}).get("fan_in_group") == "fanin-x"
    assert (latest.metadata or {}).get("leg_key") == 2


@pytest.mark.asyncio
async def test_restart_intensity_exceeded_escalates_and_reclaims(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    await repos.agents.create("coordinator-1", "dispatcher", parent_agent_id="owner")
    await _ephemeral(repos, "reviewer-1", restart="transient", max_restarts=1, max_seconds=60)
    await _task(repos, "reviewer-1", status="failed")
    sched = _scheduler(repos, tmp_path)

    # 1st reap: within budget (count 1 <= 1) → restart.
    await sched._reap_ephemeral_agents()
    assert not await _archived(repos, "reviewer-1")
    restarted = await repos.tasks.find_latest_task_for_agent("reviewer-1")
    assert restarted is not None and restarted.status == "pending"

    # The restarted leg fails too; 2nd reap: count 2 > 1 → escalate + reclaim.
    await repos.tasks.update_status(restarted.id, "failed")
    await sched._reap_ephemeral_agents()
    assert await _archived(repos, "reviewer-1")
    esc = await _supervision_mail(repos, "coordinator-1", "supervision_escalation")
    assert len(esc) == 1 and esc[0].urgency == "high"
    state = await repos.supervision.get("reviewer-1")
    assert state is not None and state.escalated_at is not None


@pytest.mark.asyncio
async def test_bump_and_check_intensity_window(
    repos: SqliteRepositories,
) -> None:
    await repos.agents.create("a", "reviewer", parent_agent_id="coordinator-1")
    t0 = datetime(2026, 6, 2, 12, 0, 0, tzinfo=UTC)
    sup = repos.supervision
    # max_restarts=1 within 60s.
    assert await sup.bump_and_check_intensity("a", 1, 60, t0) is True   # count 1
    assert await sup.bump_and_check_intensity("a", 1, 60, t0 + timedelta(seconds=10)) is False  # count 2 > 1
    # Past the window → reset to count 1 → within budget again.
    assert await sup.bump_and_check_intensity("a", 1, 60, t0 + timedelta(seconds=120)) is True
    state = await sup.get("a")
    assert state is not None and state.restart_count == 1


@pytest.mark.asyncio
async def test_ephemeral_task_terminated_is_suppressed(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    # PR3's task_terminated must NOT fire for ephemeral agents (reaper owns them).
    await repos.agents.create("coordinator-1", "dispatcher", parent_agent_id="owner")
    await _ephemeral(repos, "reviewer-1", restart="transient")
    tid = await _task(repos, "reviewer-1", status="failed")
    task = await repos.tasks.get(tid)
    sched = _scheduler(repos, tmp_path)
    await sched._emit_task_terminated_mail(
        task, "wk", "failed", summary="x", failure_reason="E", transcript_uri=None
    )
    assert await repos.outbox.dequeue_batch(limit=10) == []
