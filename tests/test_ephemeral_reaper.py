"""PR4: ephemeral-agent reclamation (the Erlang-style reaper).

An agent created with supervision.ephemeral=true is auto-archived by the
scheduler's Phase 0.8 reaper once it has run at least one task and has none in
flight — so spawned workers (fan-in panel members etc.) don't accumulate.
Done/failure signals already ride PR3's task_terminated mail, so reclaim is pure
GC (no mail). See docs/design/WORKFLOW_ORCHESTRATION.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lyre.config import Config
from lyre.persistence.models import Persona, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.tools import ToolContext, ToolError
from lyre.runtime.tools.introspect import _create_agent
from lyre.scheduler.scheduler import Scheduler

from .fake_adapter import FakeAdapter
from .helpers import fake_entry, fake_registry

_EPHEMERAL = {"supervision": {"ephemeral": True}}


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


async def _ephemeral_with_task(
    repos: SqliteRepositories, agent_id: str, *, status: str, parent: str | None = "coordinator-1"
) -> str:
    """Create an ephemeral agent + one task in `status`. Returns agent_id."""
    await repos.agents.create(
        agent_id, "reviewer", parent_agent_id=parent, metadata=dict(_EPHEMERAL)
    )
    tid = await repos.tasks.create(TaskSpec(agent_id=agent_id, goal="g", acceptance="a"))
    if status != "pending":
        await repos.tasks.update_status(tid, status)
    return agent_id


async def _is_archived(repos: SqliteRepositories, agent_id: str) -> bool:
    a = await repos.agents.get(agent_id)
    return a is not None and a.status == "archived"


@pytest.mark.asyncio
async def test_ephemeral_with_done_task_is_reaped(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    await _ephemeral_with_task(repos, "reviewer-1", status="completed")
    await _scheduler(repos, tmp_path)._reap_ephemeral_agents()
    assert await _is_archived(repos, "reviewer-1")


@pytest.mark.asyncio
async def test_ephemeral_with_inflight_task_is_not_reaped(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    for st in ("pending", "in_progress", "needs_input"):
        aid = f"reviewer-{st}"
        await _ephemeral_with_task(repos, aid, status=st)
        await _scheduler(repos, tmp_path)._reap_ephemeral_agents()
        assert not await _is_archived(repos, aid), st


@pytest.mark.asyncio
async def test_ephemeral_with_no_tasks_is_not_reaped(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    # The create -> first-dispatch race guard: a freshly spawned agent that
    # hasn't been given a task yet must NOT be reaped out from under the
    # coordinator that's about to dispatch to it.
    await repos.agents.create(
        "reviewer-fresh", "reviewer", parent_agent_id="coordinator-1", metadata=dict(_EPHEMERAL)
    )
    await _scheduler(repos, tmp_path)._reap_ephemeral_agents()
    assert not await _is_archived(repos, "reviewer-fresh")


@pytest.mark.asyncio
async def test_non_ephemeral_agent_is_never_reaped(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    await repos.agents.create("worker-1", "worker", parent_agent_id="coordinator-1")
    tid = await repos.tasks.create(TaskSpec(agent_id="worker-1", goal="g", acceptance="a"))
    await repos.tasks.update_status(tid, "completed")
    await _scheduler(repos, tmp_path)._reap_ephemeral_agents()
    assert not await _is_archived(repos, "worker-1")


@pytest.mark.asyncio
async def test_bootstrap_ephemeral_with_null_parent_is_not_reaped(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    # parent_agent_id IS NULL marks a bootstrap singleton — never reapable even
    # if (pathologically) flagged ephemeral.
    await repos.agents.create(
        "leader", "dispatcher", parent_agent_id=None, metadata=dict(_EPHEMERAL)
    )
    tid = await repos.tasks.create(TaskSpec(agent_id="leader", goal="g", acceptance="a"))
    await repos.tasks.update_status(tid, "completed")
    await _scheduler(repos, tmp_path)._reap_ephemeral_agents()
    assert not await _is_archived(repos, "leader")


@pytest.mark.asyncio
async def test_orphan_wakeup_on_terminal_task_does_not_block_reap(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    aid = await _ephemeral_with_task(repos, "reviewer-1", status="completed")
    # Simulate a crashed wakeup that never wrote ended_at, on the already-
    # terminal task. Liveness keys on in-flight TASK status, not raw wakeup
    # rows, so this orphan must not mask the agent as live.
    await repos.wakeups.start(
        (await repos.tasks.search(persona_name="reviewer"))[0].id, aid, agent_id=aid
    )
    await _scheduler(repos, tmp_path)._reap_ephemeral_agents()
    assert await _is_archived(repos, aid)


@pytest.mark.asyncio
async def test_reap_is_idempotent(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    await _ephemeral_with_task(repos, "reviewer-1", status="completed")
    sched = _scheduler(repos, tmp_path)
    await sched._reap_ephemeral_agents()
    await sched._reap_ephemeral_agents()  # no-op second pass
    assert await _is_archived(repos, "reviewer-1")
    # Reaped agents drop out of the candidate set (status='archived').
    assert await repos.agents.find_reapable_ephemerals() == []


@pytest.mark.asyncio
async def test_reaped_via_tick(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    await _ephemeral_with_task(repos, "reviewer-1", status="completed")
    await _scheduler(repos, tmp_path)._tick()
    assert await _is_archived(repos, "reviewer-1")


@pytest.mark.asyncio
async def test_create_agent_stores_and_validates_supervision(
    repos: SqliteRepositories,
) -> None:
    await repos.personas.upsert(
        Persona(name="reviewer", role_description="r", system_prompt="r")
    )
    ctx = ToolContext(
        repos=repos, task_id="t", wakeup_id="w",
        persona_name="dispatcher", agent_id="coordinator-1",
    )
    out = await _create_agent(
        ctx, {"persona": "reviewer", "name": "panel-1", "supervision": {"ephemeral": True}}
    )
    agent = await repos.agents.get(out["agent_id"])
    assert agent is not None
    assert (agent.metadata or {})["supervision"]["ephemeral"] is True
    assert agent.parent_agent_id == "coordinator-1"

    with pytest.raises(ToolError, match="ephemeral must be a boolean"):
        await _create_agent(
            ctx, {"persona": "reviewer", "name": "panel-2", "supervision": {"ephemeral": "yes"}}
        )
