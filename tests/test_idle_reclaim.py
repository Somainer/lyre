"""Idle-reclaim: surface idle non-ephemeral spawned agents to the Dispatcher.

A spawned, NON-ephemeral agent that is done (no in-flight task) is never
auto-archived by the ephemeral reaper — it lingers in `idle` forever. This
feature surfaces such agents via `list_agents`' `idle_seconds` + `stale` fields
once they've been idle past `LYRE_IDLE_RECLAIM_AGE`, so the Dispatcher can
`archive_agent` them itself (a PULL hint — the runtime never auto-archives on
it). Ephemeral agents (reaper's job), in-flight agents, open fan-in legs, and
bootstrap singletons are never flagged; the whole flag is off when the threshold
is 0 (the default). See docs/design/WORKFLOW_ORCHESTRATION.md.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from lyre.persistence.models import FanInGroup, FanInMember, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.future_mail import now_utc
from lyre.runtime.tools import ToolContext
from lyre.runtime.tools.introspect import _list_agents

_HOUR = 3600


async def _spawned(
    repos: SqliteRepositories,
    agent_id: str,
    *,
    parent: str | None = "dispatcher",
    ephemeral: bool = False,
) -> str:
    meta = {"supervision": {"ephemeral": True}} if ephemeral else None
    await repos.agents.create(agent_id, "reviewer", parent_agent_id=parent, metadata=meta)
    return agent_id


# ---------------------------------------------------------------------------
# idle_report (repository primitive)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_idle_nonephemeral_spawned_is_stale_past_threshold(
    repos: SqliteRepositories,
) -> None:
    await _spawned(repos, "reviewer/r1")
    # last_active falls back to created_at (≈ real now); look from +1h with a
    # 60s threshold → ~3600s idle ≫ 60 → stale.
    rep = await repos.agents.idle_report(now_utc() + timedelta(hours=1), 60)
    assert rep["reviewer/r1"].stale is True
    assert rep["reviewer/r1"].idle_seconds >= _HOUR - 5


@pytest.mark.asyncio
async def test_within_threshold_not_stale(repos: SqliteRepositories) -> None:
    await _spawned(repos, "reviewer/r1")
    rep = await repos.agents.idle_report(now_utc(), _HOUR)  # idle ≈ 0 < 3600
    assert rep["reviewer/r1"].stale is False
    assert rep["reviewer/r1"].idle_seconds < _HOUR


@pytest.mark.asyncio
async def test_ephemeral_never_stale(repos: SqliteRepositories) -> None:
    # Reaper's job, not the Dispatcher's — never flagged here even when idle.
    await _spawned(repos, "reviewer/eph", ephemeral=True)
    rep = await repos.agents.idle_report(now_utc() + timedelta(hours=1), 60)
    assert rep["reviewer/eph"].stale is False
    assert rep["reviewer/eph"].idle_seconds >= _HOUR - 5  # still reported


@pytest.mark.asyncio
async def test_in_flight_task_not_stale(repos: SqliteRepositories) -> None:
    await _spawned(repos, "reviewer/r1")
    await repos.tasks.create(TaskSpec(agent_id="reviewer/r1", goal="g", acceptance="a"))
    # pending == in flight → not reclaimable however idle it looks.
    rep = await repos.agents.idle_report(now_utc() + timedelta(hours=1), 60)
    assert rep["reviewer/r1"].stale is False


@pytest.mark.asyncio
async def test_completed_task_does_not_block_stale(repos: SqliteRepositories) -> None:
    await _spawned(repos, "reviewer/r1")
    tid = await repos.tasks.create(
        TaskSpec(agent_id="reviewer/r1", goal="g", acceptance="a")
    )
    await repos.tasks.update_status(tid, "completed")  # terminal → not in flight
    rep = await repos.agents.idle_report(now_utc() + timedelta(hours=1), 60)
    assert rep["reviewer/r1"].stale is True


@pytest.mark.asyncio
async def test_open_fan_in_leg_not_stale_then_stale_when_resolved(
    repos: SqliteRepositories,
) -> None:
    await repos.agents.create("coordinator-1", "dispatcher", parent_agent_id="owner")
    await _spawned(repos, "reviewer/leg", parent="coordinator-1")
    ptid = await repos.tasks.create(
        TaskSpec(agent_id="coordinator-1", goal="coord", acceptance="a")
    )
    ctid = await repos.tasks.create(
        TaskSpec(agent_id="reviewer/leg", goal="leg", acceptance="a")
    )
    await repos.tasks.update_status(ctid, "completed")  # leg done, no in-flight task
    gid = await repos.fan_in.create_group(
        FanInGroup(
            id="g1",
            coordinator_agent_id="coordinator-1",
            parent_task_id=ptid,
            expect_replies=1,
            quorum=1,
            result_schema={"type": "object"},
            deadline=now_utc() + timedelta(hours=1),
        )
    )
    await repos.fan_in.add_member(
        FanInMember(group_id=gid, leg_key=0, child_task_id=ctid, child_agent_id="reviewer/leg")
    )

    far = now_utc() + timedelta(hours=1)
    # Group OPEN → the coordinator may still re-dispatch / is awaiting it → hold off.
    assert (await repos.agents.idle_report(far, 60))["reviewer/leg"].stale is False
    # Group resolved → the barrier is closed → safe to reclaim.
    await repos.fan_in.set_status(gid, "resolved")
    assert (await repos.agents.idle_report(far, 60))["reviewer/leg"].stale is True


@pytest.mark.asyncio
async def test_bootstrap_singleton_never_stale(repos: SqliteRepositories) -> None:
    # parent_agent_id NULL → owner-facing singleton → protected, like the reaper.
    await repos.agents.create("dispatcher", "dispatcher", parent_agent_id=None)
    rep = await repos.agents.idle_report(now_utc() + timedelta(hours=1), 60)
    assert rep["dispatcher"].stale is False


@pytest.mark.asyncio
async def test_owner_created_agent_never_stale(repos: SqliteRepositories) -> None:
    # A human creates agents via CLI/dashboard with parent_agent_id='owner'
    # (a literal string, not NULL). Those are owner-curated, meant to persist —
    # the Dispatcher must not reclaim them, only the children agents spawn.
    await _spawned(repos, "reviewer/curated", parent="owner")
    rep = await repos.agents.idle_report(now_utc() + timedelta(hours=1), 60)
    assert rep["reviewer/curated"].stale is False
    assert rep["reviewer/curated"].idle_seconds >= _HOUR - 5  # still reported


@pytest.mark.asyncio
async def test_threshold_zero_disables_stale(repos: SqliteRepositories) -> None:
    await _spawned(repos, "reviewer/r1")
    rep = await repos.agents.idle_report(now_utc() + timedelta(hours=1), 0)
    assert rep["reviewer/r1"].stale is False
    assert rep["reviewer/r1"].idle_seconds >= _HOUR - 5  # idle still reported


@pytest.mark.asyncio
async def test_archived_agent_excluded_from_report(repos: SqliteRepositories) -> None:
    await _spawned(repos, "reviewer/r1")
    await repos.agents.archive("reviewer/r1")
    rep = await repos.agents.idle_report(now_utc() + timedelta(hours=1), 60)
    assert "reviewer/r1" not in rep


@pytest.mark.asyncio
async def test_idle_seconds_clamped_nonnegative(repos: SqliteRepositories) -> None:
    await _spawned(repos, "reviewer/r1")
    # `now` BEFORE created_at (clock skew) must not yield a negative idle.
    rep = await repos.agents.idle_report(now_utc() - timedelta(hours=1), 60)
    assert rep["reviewer/r1"].idle_seconds == 0


# ---------------------------------------------------------------------------
# list_agents integration (the surface the Dispatcher reads)
# ---------------------------------------------------------------------------
def _ctx(repos: SqliteRepositories, *, threshold: int) -> ToolContext:
    return ToolContext(
        repos=repos,
        task_id="t",
        wakeup_id="w",
        persona_name="dispatcher",
        agent_id="dispatcher",
        extras={"idle_reclaim_age_s": threshold},
    )


@pytest.mark.asyncio
async def test_list_agents_surfaces_idle_and_stale(
    repos: SqliteRepositories, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    await _spawned(repos, "reviewer/r1")
    # list_agents reads now via now_utc() internally — push it an hour ahead so
    # the fresh agent reads as long-idle.
    future = now_utc() + timedelta(hours=1)
    monkeypatch.setattr("lyre.runtime.tools.introspect.now_utc", lambda: future)

    out = await _list_agents(_ctx(repos, threshold=60), {})
    row = next(a for a in out["agents"] if a["id"] == "reviewer/r1")
    assert row["stale"] is True
    assert row["idle_seconds"] >= _HOUR - 5
    assert "archive_agent" in out["note"]  # housekeeping hint appended


@pytest.mark.asyncio
async def test_list_agents_no_stale_when_disabled(
    repos: SqliteRepositories, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _spawned(repos, "reviewer/r1")
    future = now_utc() + timedelta(hours=1)
    monkeypatch.setattr("lyre.runtime.tools.introspect.now_utc", lambda: future)

    out = await _list_agents(_ctx(repos, threshold=0), {})
    row = next(a for a in out["agents"] if a["id"] == "reviewer/r1")
    assert row["stale"] is False
    assert row["idle_seconds"] >= _HOUR - 5  # idle still surfaced
    assert "archive_agent" not in out["note"]
