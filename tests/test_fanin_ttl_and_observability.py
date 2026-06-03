"""PR6: global fan-in TTL backstop + archival observability.

(1) TTL: a global LYRE_FANIN_MAX_AGE force-expires any `open` fan_in_group older
    than the cap, independent of the group's own (coordinator-set) deadline. 0
    disables it (the per-group deadline remains the always-on liveness).
(2) Observability: every archival records WHY — reaped / storm_halted /
    idle_reclaimed / manual — written atomically with the archive, surfaced by
    list_agents (and the dashboard). See docs/design/WORKFLOW_ORCHESTRATION.md.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from lyre.config import Config
from lyre.persistence.models import FanInGroup, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.future_mail import now_utc
from lyre.runtime.tools import ToolContext
from lyre.runtime.tools.introspect import _archive_agent, _list_agents
from lyre.scheduler.scheduler import Scheduler

from .fake_adapter import FakeAdapter
from .helpers import fake_entry, fake_registry

_EPHEMERAL = {"supervision": {"ephemeral": True}}


def _scheduler(
    repos: SqliteRepositories, tmp_path: Path, *, fanin_max_age_s: int = 0
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
        fanin_max_age_s=fanin_max_age_s,
    )
    cfg.object_store_path.mkdir(parents=True, exist_ok=True)
    return Scheduler(
        repos, cfg, registry=fake_registry(fake_entry()),
        adapter_for_test=lambda e: FakeAdapter(), auto_wake_on_mail=False,
    )


async def _open_group(repos: SqliteRepositories, *, age_hours: float, deadline_hours: float) -> str:
    """A fan-in group whose created_at is backdated `age_hours` and whose own
    deadline is `deadline_hours` in the future."""
    await repos.agents.create("coordinator-1", "dispatcher", parent_agent_id="owner")
    gid = await repos.fan_in.create_group(
        FanInGroup(
            id="g1",
            coordinator_agent_id="coordinator-1",
            parent_task_id=None,
            expect_replies=2,
            quorum=2,
            result_schema={"type": "object"},
            deadline=now_utc() + timedelta(hours=deadline_hours),
        )
    )
    backdated = (now_utc() - timedelta(hours=age_hours)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    await repos.fan_in.conn.execute(
        "UPDATE fan_in_groups SET created_at = ? WHERE id = ?", (backdated, gid)
    )
    await repos.fan_in.conn.commit()
    return gid


async def _make_group(
    repos: SqliteRepositories,
    gid: str,
    coordinator: str,
    *,
    age_hours: float,
    deadline_hours: float,
) -> None:
    """One open group under an existing `coordinator`, created `age_hours` ago
    with its own deadline `deadline_hours` out — for building a multi-group
    backlog (negative deadline_hours = past)."""
    await repos.fan_in.create_group(
        FanInGroup(
            id=gid,
            coordinator_agent_id=coordinator,
            parent_task_id=None,
            expect_replies=2,
            quorum=2,
            result_schema={"type": "object"},
            deadline=now_utc() + timedelta(hours=deadline_hours),
        )
    )
    backdated = (now_utc() - timedelta(hours=age_hours)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    await repos.fan_in.conn.execute(
        "UPDATE fan_in_groups SET created_at = ? WHERE id = ?", (backdated, gid)
    )
    await repos.fan_in.conn.commit()


# --------------------------------------------------------------------------
# (1) global TTL
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_global_ttl_expires_old_open_group(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    # 2h old, deadline still 1h in the FUTURE → only the global TTL can close it.
    gid = await _open_group(repos, age_hours=2, deadline_hours=1)
    sched = _scheduler(repos, tmp_path, fanin_max_age_s=3600)  # 1h cap
    await sched._resolve_fan_in_barriers()

    grp = await repos.fan_in.get(gid)
    assert grp is not None and grp.status == "expired"
    msgs = await repos.mailbox.read_unread("coordinator-1", min_urgency="low", limit=10)
    ready = [m for m in msgs if (m.metadata or {}).get("fan_in_resolved") == gid]
    assert len(ready) == 1
    assert ready[0].metadata.get("trigger") == "ttl"


@pytest.mark.asyncio
async def test_ttl_disabled_by_default_keeps_group_open(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    gid = await _open_group(repos, age_hours=99, deadline_hours=1)
    sched = _scheduler(repos, tmp_path, fanin_max_age_s=0)  # disabled
    await sched._resolve_fan_in_barriers()
    grp = await repos.fan_in.get(gid)
    assert grp is not None and grp.status == "open"  # deadline (future) governs


@pytest.mark.asyncio
async def test_ttl_resolution_is_idempotent(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    gid = await _open_group(repos, age_hours=2, deadline_hours=1)
    sched = _scheduler(repos, tmp_path, fanin_max_age_s=3600)
    await sched._resolve_fan_in_barriers()
    await sched._resolve_fan_in_barriers()  # second pass: group already expired
    msgs = await repos.mailbox.read_all_by_recipient("coordinator-1")
    assert sum(1 for m in msgs if (m.metadata or {}).get("fan_in_resolved") == gid) == 1


@pytest.mark.asyncio
async def test_deadline_still_closes_when_ttl_disabled(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    # deadline in the PAST, TTL disabled → existing deadline path closes it.
    gid = await _open_group(repos, age_hours=1, deadline_hours=-1)
    sched = _scheduler(repos, tmp_path, fanin_max_age_s=0)
    await sched._resolve_fan_in_barriers()
    grp = await repos.fan_in.get(gid)
    assert grp is not None and grp.status == "expired"
    msgs = await repos.mailbox.read_unread("coordinator-1", min_urgency="low", limit=10)
    ready = [m for m in msgs if (m.metadata or {}).get("fan_in_resolved") == gid]
    assert ready[0].metadata.get("trigger") == "deadline"


@pytest.mark.asyncio
async def test_global_ttl_expires_age_expired_group_buried_behind_full_deadline_page(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    """Under load (>20 open groups) the global ceiling must STILL be enforced.

    Regression: find_open(limit=20) is deadline-ordered, so an age-expired group
    whose own deadline is far in the future sorts to position 21+ and never
    reached the TTL check — the global cap silently stopped applying under load.
    """
    await repos.agents.create("coordinator-1", "dispatcher", parent_agent_id="owner")
    # The straggler: 2h old (> 1h cap), but deadline 10h out → sorts dead last.
    await _make_group(repos, "old", "coordinator-1", age_hours=2, deadline_hours=10)
    # 25 fresh groups with EARLIER deadlines fill the 20-row deadline page and
    # bury "old" at position 26 — none are age-expired, quorum-met, or overdue.
    for i in range(25):
        await _make_group(
            repos, f"young-{i}", "coordinator-1", age_hours=0, deadline_hours=1 + i * 0.1
        )
    sched = _scheduler(repos, tmp_path, fanin_max_age_s=3600)  # 1h cap
    await sched._resolve_fan_in_barriers()

    old = await repos.fan_in.get("old")
    assert old is not None and old.status == "expired"
    msgs = await repos.mailbox.read_unread("coordinator-1", min_urgency="low", limit=50)
    ready = [m for m in msgs if (m.metadata or {}).get("fan_in_resolved") == "old"]
    assert len(ready) == 1 and ready[0].metadata.get("trigger") == "ttl"
    # The fresh, in-deadline groups are untouched (not past their own deadline).
    for i in range(25):
        g = await repos.fan_in.get(f"young-{i}")
        assert g is not None and g.status == "open"


# --------------------------------------------------------------------------
# (2) archive_reason observability
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_archive_records_and_unarchive_clears_reason(
    repos: SqliteRepositories,
) -> None:
    await repos.agents.create("reviewer/r1", "reviewer", parent_agent_id="coordinator-1")
    await repos.agents.archive("reviewer/r1", reason="reaped")
    a = await repos.agents.get("reviewer/r1")
    assert a is not None and a.status == "archived" and a.archive_reason == "reaped"
    await repos.agents.unarchive("reviewer/r1")
    a = await repos.agents.get("reviewer/r1")
    assert a is not None and a.archive_reason is None


@pytest.mark.asyncio
async def test_reaper_marks_reaped(repos: SqliteRepositories, tmp_path: Path) -> None:
    await repos.agents.create("coordinator-1", "dispatcher", parent_agent_id="owner")
    await repos.agents.create(
        "reviewer/r1", "reviewer", parent_agent_id="coordinator-1", metadata=dict(_EPHEMERAL)
    )
    tid = await repos.tasks.create(TaskSpec(agent_id="reviewer/r1", goal="g", acceptance="a"))
    await repos.tasks.update_status(tid, "completed")
    sched = _scheduler(repos, tmp_path)
    await sched._reap_ephemeral_agents()
    a = await repos.agents.get("reviewer/r1")
    assert a is not None and a.status == "archived" and a.archive_reason == "reaped"


@pytest.mark.asyncio
async def test_phase2_crash_loop_marks_storm_halted(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    await repos.agents.create("coordinator-1", "dispatcher", parent_agent_id="owner")
    await repos.agents.create(
        "reviewer/r1", "reviewer", parent_agent_id="coordinator-1",
        metadata={"supervision": {"ephemeral": True, "restart": "transient",
                                   "max_restarts": 1, "max_seconds": 60}},
    )
    tid = await repos.tasks.create(TaskSpec(agent_id="reviewer/r1", goal="g", acceptance="a"))
    task = await repos.tasks.get(tid)
    assert task is not None
    sched = _scheduler(repos, tmp_path)
    assert await sched._ephemeral_recovery_exceeded(task) is False  # within budget
    assert await sched._ephemeral_recovery_exceeded(task) is True   # exceeded → halt
    a = await repos.agents.get("reviewer/r1")
    assert a is not None and a.status == "archived" and a.archive_reason == "storm_halted"


def _dispatcher_ctx(repos: SqliteRepositories) -> ToolContext:
    return ToolContext(
        repos=repos, task_id="t", wakeup_id="w",
        persona_name="dispatcher", agent_id="dispatcher",
    )


@pytest.mark.asyncio
async def test_archive_agent_tool_records_reason(repos: SqliteRepositories) -> None:
    await repos.agents.create("coordinator-1", "dispatcher", parent_agent_id="owner")
    await repos.agents.create("reviewer/r1", "reviewer", parent_agent_id="coordinator-1")
    await repos.agents.create("reviewer/r2", "reviewer", parent_agent_id="coordinator-1")
    ctx = _dispatcher_ctx(repos)
    await _archive_agent(ctx, {"agent_id": "reviewer/r1", "reason": "idle_reclaimed"})
    await _archive_agent(ctx, {"agent_id": "reviewer/r2"})  # default
    assert (await repos.agents.get("reviewer/r1")).archive_reason == "idle_reclaimed"
    assert (await repos.agents.get("reviewer/r2")).archive_reason == "manual"


@pytest.mark.asyncio
async def test_list_agents_surfaces_archive_reason(repos: SqliteRepositories) -> None:
    await repos.agents.create("coordinator-1", "dispatcher", parent_agent_id="owner")
    await repos.agents.create("reviewer/r1", "reviewer", parent_agent_id="coordinator-1")
    await repos.agents.archive("reviewer/r1", reason="storm_halted")
    out = await _list_agents(_dispatcher_ctx(repos), {"include_archived": True})
    row = next(a for a in out["agents"] if a["id"] == "reviewer/r1")
    assert row["archive_reason"] == "storm_halted"
    assert row["archived_at"] is not None
