"""Orchestration robustness — fan-in failure visibility cluster (O1/O2).

O2a (here): the outbox DAO that detects a leg's typed result still enqueued but
not yet dispatched, so O2's completion-time validation doesn't false-downgrade a
leg whose result is one tick from delivery. The full 3-leg acceptance mock lands
with O1a/O2b.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from lyre.persistence.models import (
    FanInGroup,
    FanInMember,
    MailboxMessage,
    OutboxRow,
    Persona,
    TaskSpec,
)
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.tools import ToolContext
from lyre.runtime.tools.fan_in import _fan_in_results
from lyre.scheduler.scheduler import Scheduler


@pytest.mark.asyncio
async def test_has_pending_fan_in_result_scoped_to_task_group_leg(
    repos: SqliteRepositories,
) -> None:
    await repos.personas.upsert(
        Persona(name="w", role_description="w", system_prompt="w")
    )
    await repos.agents.create("agent-1", "w")
    tid = await repos.tasks.create(
        TaskSpec(persona_name="w", goal="g", acceptance="a")
    )

    await repos.outbox.enqueue([
        OutboxRow(
            task_id=tid,
            wakeup_id=None,
            kind="mailbox_send",
            external_id="r-leg1",
            payload={
                "recipient": "coordinator-1",
                "metadata": {
                    "fan_in": {"group_id": "grp", "leg_key": 1, "result": {"ok": True}}
                },
            },
        )
    ])

    assert await repos.outbox.has_pending_fan_in_result(tid, "grp", 1) is True
    # Scoped: wrong leg / wrong task / wrong group are not matched.
    assert await repos.outbox.has_pending_fan_in_result(tid, "grp", 2) is False
    assert await repos.outbox.has_pending_fan_in_result("other-task", "grp", 1) is False
    assert await repos.outbox.has_pending_fan_in_result(tid, "other-grp", 1) is False

    # Once dispatched, it is no longer "pending".
    await repos.conn.execute(
        "UPDATE outbox SET dispatched_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
        "WHERE task_id = ?",
        (tid,),
    )
    await repos.conn.commit()
    assert await repos.outbox.has_pending_fan_in_result(tid, "grp", 1) is False


async def _leg_task(repos: SqliteRepositories, meta: dict | None) -> object:
    tid = await repos.tasks.create(
        TaskSpec(persona_name="w", goal="g", acceptance="a", metadata=meta)
    )
    return await repos.tasks.get(tid)


@pytest.mark.asyncio
async def test_fan_in_leg_submitted_result_validation(
    repos: SqliteRepositories,
) -> None:
    """O2b helper: a fan-in leg counts as having submitted iff its typed result
    is pending in the outbox OR already delivered to the coordinator; a
    non-fan-in task returns None (nothing to validate)."""
    await repos.personas.upsert(
        Persona(name="w", role_description="w", system_prompt="w")
    )
    await repos.agents.create("coordinator-1", "w")
    await repos.agents.create("leg-agent", "w")
    await repos.fan_in.create_group(
        FanInGroup(
            id="grp", coordinator_agent_id="coordinator-1", expect_replies=3,
            quorum=3, result_schema={}, deadline=datetime(2999, 1, 1, tzinfo=UTC),
        )
    )
    fake = SimpleNamespace(repos=repos)

    # Non-fan-in task → None.
    t_plain = await _leg_task(repos, None)
    assert await Scheduler._fan_in_leg_submitted_result(fake, t_plain, t_plain.id) is None

    # Fan-in leg with NO result anywhere → False (the silent dead leg).
    t_none = await _leg_task(repos, {"fan_in_group": "grp", "leg_key": 1})
    assert await Scheduler._fan_in_leg_submitted_result(fake, t_none, t_none.id) is False

    # Fan-in leg with a PENDING outbox result → True.
    t_pending = await _leg_task(repos, {"fan_in_group": "grp", "leg_key": 2})
    await repos.outbox.enqueue([
        OutboxRow(task_id=t_pending.id, kind="mailbox_send", external_id="r2",
                  payload={"metadata": {"fan_in": {"group_id": "grp", "leg_key": 2,
                                                    "result": {"ok": 1}}}})
    ])
    assert await Scheduler._fan_in_leg_submitted_result(fake, t_pending, t_pending.id) is True

    # Fan-in leg whose result is already DELIVERED to the coordinator → True.
    t_delivered = await _leg_task(repos, {"fan_in_group": "grp", "leg_key": 3})
    await repos.mailbox.insert_message(
        MailboxMessage(recipient="coordinator-1", external_id="d3", sender="leg-agent",
                       urgency="low", body="result",
                       metadata={"fan_in": {"group_id": "grp", "leg_key": 3,
                                            "result": {"ok": 1}}})
    )
    assert await Scheduler._fan_in_leg_submitted_result(fake, t_delivered, t_delivered.id) is True


def _fake_sched(repos: SqliteRepositories) -> SimpleNamespace:
    fake = SimpleNamespace(
        repos=repos,
        config=SimpleNamespace(fanin_max_age_s=0),
        # Poison-group counter the real __init__ owns.
        _fanin_failures={},
    )
    # _resolve_fan_in_barriers calls these on self; bind the real methods
    # so the lightweight fake-self exercises the real composition.
    fake._reconcile_fan_in_failed_legs = (
        lambda g: Scheduler._reconcile_fan_in_failed_legs(fake, g)
    )
    fake._resolve_one_fan_in_group = (
        lambda g, now, max_age: Scheduler._resolve_one_fan_in_group(
            fake, g, now, max_age
        )
    )
    fake._force_expire_poison_fan_in = (
        lambda g, exc: Scheduler._force_expire_poison_fan_in(fake, g, exc)
    )
    return fake


@pytest.mark.asyncio
async def test_fanin_dispatcher_sees_failed_leg_no_result_and_continuation_failure(
    repos: SqliteRepositories,
) -> None:
    """Acceptance mock: a 3-leg fanout where leg0 delivers a typed result, leg1
    completed without a typed result (O2-downgraded to failed), and leg2
    exhausted turns (needs_continuation→failed). The coordinator must see the
    barrier resolve at QUORUM (not the deadline), with failed_legs=[1,2] and no
    still-missing legs — instead of the group hanging silently."""
    await repos.personas.upsert(
        Persona(name="w", role_description="w", system_prompt="w")
    )
    await repos.agents.create("coordinator-1", "w")
    for a in ("leg0", "leg1", "leg2"):
        await repos.agents.create(a, "w")
    await repos.fan_in.create_group(
        FanInGroup(
            id="g", coordinator_agent_id="coordinator-1", expect_replies=3,
            quorum=3, result_schema={}, deadline=datetime(2999, 1, 1, tzinfo=UTC),
        )
    )
    tids: dict[int, str] = {}
    for leg, agent in enumerate(("leg0", "leg1", "leg2")):
        tid = await repos.tasks.create(
            TaskSpec(persona_name="w", goal="g", acceptance="a")
        )
        tids[leg] = tid
        await repos.fan_in.add_member(
            FanInMember(group_id="g", leg_key=leg, child_task_id=tid, child_agent_id=agent)
        )

    # leg0: a real typed result delivered to the coordinator.
    await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="coordinator-1", external_id="leg0-result", sender="leg0",
            urgency="low", body="finding",
            metadata={"fan_in": {"group_id": "g", "leg_key": 0, "result": {"finding": "x"}}},
        )
    )
    # leg1 (no-result completed → O2 downgrade) and leg2 (turn-exhaust) both failed.
    await repos.tasks.update_status(tids[1], "failed")
    await repos.tasks.update_status(tids[2], "failed")

    fake = _fake_sched(repos)

    # Reconciliation is idempotent: running it twice yields 2 sentinels, not 4.
    g = await repos.fan_in.get("g")
    await Scheduler._reconcile_fan_in_failed_legs(fake, g)
    await Scheduler._reconcile_fan_in_failed_legs(fake, g)
    assert await repos.mailbox.count_fan_in_results("coordinator-1", "g") == 3

    # Phase 0.5 resolves at QUORUM (1 real + 2 failed-sentinels == 3), not deadline.
    await Scheduler._resolve_fan_in_barriers(fake)
    g = await repos.fan_in.get("g")
    assert g.status == "quorum_met"

    async with repos.conn.execute(
        "SELECT json_extract(metadata,'$.trigger') AS trig FROM mailbox_messages "
        "WHERE recipient='coordinator-1' AND external_id='fanin:g:resolved'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None and row["trig"] == "quorum"

    # The fan_in_results tool separates real results from failed legs.
    ctx = ToolContext(
        repos=repos, task_id="t", wakeup_id="wk", persona_name="w",
        agent_id="coordinator-1",
    )
    out = await _fan_in_results(ctx, {"group_id": "g"})
    assert out["delivered"] == 1
    assert [r["leg_key"] for r in out["results"]] == [0]
    assert {f["leg_key"] for f in out["failed_legs"]} == {1, 2}
    assert all(f["reason"] == "failed" for f in out["failed_legs"])
    assert out["missing_legs"] == []
