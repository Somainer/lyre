"""C4: DB maintenance prunes terminal/delivered rows past the retention window,
keeps recent ones, and NEVER touches mailbox_messages (铁律五)."""

from __future__ import annotations

import pytest

from lyre.persistence.maintenance import run_maintenance
from lyre.persistence.models import MailboxMessage, Persona, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories

_OLD = "2000-01-01T00:00:00.000Z"
_NEW = "2999-01-01T00:00:00.000Z"


async def _count(conn, table: str) -> int:
    async with conn.execute(f"SELECT COUNT(*) AS n FROM {table}") as cur:
        row = await cur.fetchone()
    return int(row["n"])


@pytest.mark.asyncio
async def test_maintenance_prunes_old_keeps_recent_spares_mailbox(
    repos: SqliteRepositories,
) -> None:
    conn = repos.conn
    await repos.personas.upsert(
        Persona(name="w", role_description="w", system_prompt="w")
    )
    await repos.agents.create("agent-1", "w")
    tid = await repos.tasks.create(
        TaskSpec(persona_name="w", goal="g", acceptance="a")
    )

    # wakeups: one old ended, one recent ended.
    for wid, ts in (("wk-old", _OLD), ("wk-new", _NEW)):
        await conn.execute(
            "INSERT INTO wakeups (id, task_id, persona_name, agent_id, "
            "started_at, ended_at, end_status) VALUES (?,?,?,?,?,?,?)",
            (wid, tid, "w", "agent-1", ts, ts, "completed"),
        )
    # outbox: old delivered, recent delivered.
    for ext, ts in (("ob-old", _OLD), ("ob-new", _NEW)):
        await conn.execute(
            "INSERT INTO outbox (task_id, kind, payload, external_id, "
            "created_at, dispatched_at) VALUES (?,?,?,?,?,?)",
            (tid, "mailbox_send", "{}", ext, ts, ts),
        )
    # scheduled_mail: old completed (prunable), recent pending (kept).
    await conn.execute(
        "INSERT INTO scheduled_mail (recipient, sender, urgency, body, "
        "scheduled_for, created_at, status) VALUES (?,?,?,?,?,?,?)",
        ("owner", "w", "normal", "x", _OLD, _OLD, "completed"),
    )
    await conn.execute(
        "INSERT INTO scheduled_mail (recipient, sender, urgency, body, "
        "scheduled_for, created_at, status) VALUES (?,?,?,?,?,?,?)",
        ("owner", "w", "normal", "x", _NEW, _NEW, "pending"),
    )
    # fan_in: old resolved group + member (prunable).
    await conn.execute(
        "INSERT INTO fan_in_groups (id, coordinator_agent_id, expect_replies, "
        "quorum, result_schema, deadline, status, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("fg-old", "agent-1", 1, 1, "{}", _OLD, "resolved", _OLD),
    )
    await conn.execute(
        "INSERT INTO fan_in_members (group_id, leg_key, child_task_id, "
        "child_agent_id) VALUES (?,?,?,?)",
        ("fg-old", 0, tid, "agent-1"),
    )
    # An OLD owner mail — must NEVER be pruned.
    await repos.mailbox.insert_message(
        MailboxMessage(recipient="owner", external_id="keepme", sender="w",
                       urgency="normal", body="keep me forever")
    )
    await conn.commit()

    counts = await run_maintenance(
        conn, retention_days=1, vacuum=True, keep_wakeups_per_agent=0
    )

    assert counts["wakeups"] == 1
    assert counts["outbox"] == 1
    assert counts["scheduled_mail"] == 1
    assert counts["fan_in_groups"] == 1
    # Recent rows survive.
    assert await _count(conn, "wakeups") == 1
    assert await _count(conn, "outbox") == 1
    assert await _count(conn, "scheduled_mail") == 1
    assert await _count(conn, "fan_in_groups") == 0
    assert await _count(conn, "fan_in_members") == 0
    # Mailbox is sacrosanct.
    assert await _count(conn, "mailbox_messages") == 1


@pytest.mark.asyncio
async def test_maintenance_keeps_recent_wakeups_per_agent(
    repos: SqliteRepositories,
) -> None:
    """Even past the window, the most-recent K wakeups per agent are kept."""
    conn = repos.conn
    await repos.personas.upsert(
        Persona(name="w", role_description="w", system_prompt="w")
    )
    await repos.agents.create("agent-1", "w")
    tid = await repos.tasks.create(
        TaskSpec(persona_name="w", goal="g", acceptance="a")
    )
    # 3 OLD ended wakeups for the same agent.
    for i in range(3):
        await conn.execute(
            "INSERT INTO wakeups (id, task_id, persona_name, agent_id, "
            "started_at, ended_at, end_status) VALUES (?,?,?,?,?,?,?)",
            (f"wk-{i}", tid, "w", "agent-1", f"2000-01-0{i+1}T00:00:00.000Z",
             _OLD, "completed"),
        )
    await conn.commit()

    counts = await run_maintenance(
        conn, retention_days=1, vacuum=False, keep_wakeups_per_agent=2
    )
    # 3 old, keep 2 most-recent → 1 pruned.
    assert counts["wakeups"] == 1
    assert await _count(conn, "wakeups") == 2


async def _insert_old_completed_sched(repos: SqliteRepositories) -> None:
    await repos.conn.execute(
        "INSERT INTO scheduled_mail (recipient, sender, urgency, body, "
        "scheduled_for, created_at, status) VALUES (?,?,?,?,?,?,?)",
        ("owner", "w", "normal", "x", _OLD, _OLD, "completed"),
    )
    await repos.conn.commit()


@pytest.mark.asyncio
async def test_maintenance_phase_runs_then_throttles(
    repos: SqliteRepositories,
) -> None:
    """The scheduler maintenance phase runs once, then is throttled within
    maintenance_interval_s — verifies the wiring + the once-per-interval gate."""
    from types import SimpleNamespace

    from lyre.scheduler.scheduler import Scheduler

    await _insert_old_completed_sched(repos)
    fake = SimpleNamespace(
        config=SimpleNamespace(retention_days=1, maintenance_interval_s=3600),
        repos=repos,
        _last_maintenance=None,
    )

    await Scheduler._maybe_run_maintenance(fake)
    assert fake._last_maintenance is not None
    assert await _count(repos.conn, "scheduled_mail") == 0  # pruned

    # A second old row + an immediate second call → throttled, not pruned.
    await _insert_old_completed_sched(repos)
    await Scheduler._maybe_run_maintenance(fake)
    assert await _count(repos.conn, "scheduled_mail") == 1


@pytest.mark.asyncio
async def test_maintenance_phase_skips_when_disabled(
    repos: SqliteRepositories,
) -> None:
    from types import SimpleNamespace

    from lyre.scheduler.scheduler import Scheduler

    await _insert_old_completed_sched(repos)
    fake = SimpleNamespace(
        config=SimpleNamespace(retention_days=0, maintenance_interval_s=3600),
        repos=repos,
        _last_maintenance=None,
    )
    await Scheduler._maybe_run_maintenance(fake)
    assert fake._last_maintenance is None  # never ran
    assert await _count(repos.conn, "scheduled_mail") == 1


@pytest.mark.asyncio
async def test_maintenance_disabled_when_retention_zero(
    repos: SqliteRepositories,
) -> None:
    conn = repos.conn
    await repos.personas.upsert(
        Persona(name="w", role_description="w", system_prompt="w")
    )
    await repos.agents.create("agent-1", "w")
    tid = await repos.tasks.create(
        TaskSpec(persona_name="w", goal="g", acceptance="a")
    )
    await conn.execute(
        "INSERT INTO wakeups (id, task_id, persona_name, agent_id, "
        "started_at, ended_at, end_status) VALUES (?,?,?,?,?,?,?)",
        ("wk-old", tid, "w", "agent-1", _OLD, _OLD, "completed"),
    )
    await conn.commit()

    counts = await run_maintenance(conn, retention_days=0)
    assert counts == {"outbox": 0, "wakeups": 0, "scheduled_mail": 0, "fan_in_groups": 0}
    assert await _count(conn, "wakeups") == 1
