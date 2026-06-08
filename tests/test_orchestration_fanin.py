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
    MailboxMessage,
    OutboxRow,
    Persona,
    TaskSpec,
)
from lyre.persistence.sqlite_impl import SqliteRepositories
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
