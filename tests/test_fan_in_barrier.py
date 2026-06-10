"""PR2: the mailbox-driven fan-in barrier (R1).

End-to-end through the real tools + outbox + scheduler phase: a coordinator
opens a barrier, dispatches children into it, children return typed result-mails
via mailbox_send(result_for=...), the OutboxDispatcher delivers them, and
Phase 0.5 resolves the group + wakes the coordinator. The barrier counts
DELIVERED result-mails, so it never trips while a result is still an
undispatched outbox row. See docs/design/WORKFLOW_ORCHESTRATION.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lyre.config import Config
from lyre.outbox.dispatcher import OutboxDispatcher
from lyre.persistence.models import TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.tools import ToolContext, ToolError
from lyre.runtime.tools.fan_in import _fan_in_cancel, _fan_in_open, _fan_in_status
from lyre.runtime.tools.mailbox import _mailbox_send
from lyre.runtime.tools.tasks import _dispatch_task
from lyre.scheduler.scheduler import Scheduler

from .fake_adapter import FakeAdapter
from .helpers import fake_entry, fake_registry

_SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string"}, "rationale": {"type": "string"}},
    "required": ["verdict"],
}


async def _setup(repos: SqliteRepositories) -> str:
    """One coordinator + two reviewer agents + a coordinator task. Returns the
    coordinator's task_id (the would-be barrier opener)."""
    await repos.agents.create("coordinator-1", "dispatcher", parent_agent_id="owner")
    await repos.agents.create("rev-1", "reviewer", parent_agent_id="coordinator-1")
    await repos.agents.create("rev-2", "reviewer", parent_agent_id="coordinator-1")
    return await repos.tasks.create(
        TaskSpec(agent_id="coordinator-1", goal="review PR", acceptance="aggregated")
    )


def _ctx(
    repos: SqliteRepositories, agent_id: str, task_id: str, wakeup: str = "wk"
) -> ToolContext:
    return ToolContext(
        repos=repos,
        task_id=task_id,
        wakeup_id=wakeup,
        persona_name=agent_id,
        agent_id=agent_id,
    )


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
    return Scheduler(
        repos,
        cfg,
        registry=fake_registry(fake_entry()),
        adapter_for_test=lambda e: FakeAdapter(),
        auto_wake_on_mail=True,
    )


async def _send_result(
    repos: SqliteRepositories, agent: str, task_id: str, group: str, leg: int, result: dict
) -> dict:
    # A real wakeup row: the outbox FK (wakeup_id REFERENCES wakeups(id)) is
    # enforced, so the sender needs an actual in-flight wakeup, like production.
    wk = await repos.wakeups.start(task_id, agent, agent_id=agent)
    return await _mailbox_send(
        _ctx(repos, agent, task_id, wk),
        {
            "to": "coordinator-1",
            "body": f"{agent} result",
            "result_for": group,
            "leg_key": leg,
            "result": result,
            "_tool_use_id": f"tu-{agent}",
        },
    )


@pytest.mark.asyncio
async def test_barrier_counts_delivered_mail_not_completed_tasks(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    coord_task = await _setup(repos)
    cctx = _ctx(repos, "coordinator-1", coord_task)
    g = (await _fan_in_open(cctx, {"expect_replies": 2, "quorum": 2, "result_schema": _SCHEMA}))[
        "group_id"
    ]
    d1 = await _dispatch_task(
        cctx, {"agent": "rev-1", "goal": "g", "acceptance": "a", "fan_in": {"group_id": g, "leg_key": 0}}
    )
    d2 = await _dispatch_task(
        cctx, {"agent": "rev-2", "goal": "g", "acceptance": "a", "fan_in": {"group_id": g, "leg_key": 1}}
    )
    await _send_result(repos, "rev-1", d1["task_id"], g, 0, {"verdict": "approve"})
    await _send_result(repos, "rev-2", d2["task_id"], g, 1, {"verdict": "reject"})

    sched = _scheduler(repos, tmp_path)
    # Results are still in the outbox (undelivered) — the barrier must NOT trip.
    await sched._resolve_fan_in_barriers()
    grp = await repos.fan_in.get(g)
    assert grp is not None and grp.status == "open"
    assert await repos.mailbox.count_fan_in_results("coordinator-1", g) == 0

    # Deliver, then it resolves.
    await OutboxDispatcher(repos).tick()
    assert await repos.mailbox.count_fan_in_results("coordinator-1", g) == 2
    await sched._resolve_fan_in_barriers()
    grp = await repos.fan_in.get(g)
    assert grp is not None and grp.status == "quorum_met"

    # A high-urgency 'ready' mail reached the coordinator (the resume trigger).
    msgs = await repos.mailbox.read_messages("coordinator-1")
    ready = [m for m in msgs if (m.metadata or {}).get("fan_in_resolved") == g]
    assert len(ready) == 1 and ready[0].urgency == "high"


@pytest.mark.asyncio
async def test_resolve_is_idempotent_single_winner(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    coord_task = await _setup(repos)
    cctx = _ctx(repos, "coordinator-1", coord_task)
    g = (await _fan_in_open(cctx, {"expect_replies": 1, "quorum": 1, "result_schema": _SCHEMA}))[
        "group_id"
    ]
    d = await _dispatch_task(
        cctx, {"agent": "rev-1", "goal": "g", "acceptance": "a", "fan_in": {"group_id": g, "leg_key": 0}}
    )
    await _send_result(repos, "rev-1", d["task_id"], g, 0, {"verdict": "approve"})
    await OutboxDispatcher(repos).tick()
    sched = _scheduler(repos, tmp_path)
    await sched._resolve_fan_in_barriers()
    await sched._resolve_fan_in_barriers()  # re-run is a no-op (status != open)
    msgs = await repos.mailbox.read_messages("coordinator-1")
    ready = [m for m in msgs if (m.metadata or {}).get("fan_in_resolved") == g]
    assert len(ready) == 1  # exactly-once ready mail


@pytest.mark.asyncio
async def test_result_mail_is_low_urgency_and_does_not_auto_wake_before_quorum(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    coord_task = await _setup(repos)
    cctx = _ctx(repos, "coordinator-1", coord_task)
    g = (await _fan_in_open(cctx, {"expect_replies": 2, "quorum": 2, "result_schema": _SCHEMA}))[
        "group_id"
    ]
    d1 = await _dispatch_task(
        cctx, {"agent": "rev-1", "goal": "g", "acceptance": "a", "fan_in": {"group_id": g, "leg_key": 0}}
    )
    await _send_result(repos, "rev-1", d1["task_id"], g, 0, {"verdict": "approve"})
    await OutboxDispatcher(repos).tick()

    delivered = [
        m for m in await repos.mailbox.read_messages("coordinator-1")
        if (m.metadata or {}).get("fan_in")
    ]
    assert len(delivered) == 1 and delivered[0].urgency == "low"

    # Coordinator's opening wakeup finished (task completed → agent idle). A
    # partial inbox of low-urgency results must NOT auto-wake it.
    await repos.tasks.update_status(coord_task, "completed")
    sched = _scheduler(repos, tmp_path)
    await sched._auto_dispatch_for_unread_mail()
    tasks = await repos.tasks.search(persona_name="dispatcher")
    assert not any((t.metadata or {}).get("auto_dispatched") for t in tasks)


@pytest.mark.asyncio
async def test_send_time_schema_validation_fails_closed(
    repos: SqliteRepositories,
) -> None:
    coord_task = await _setup(repos)
    cctx = _ctx(repos, "coordinator-1", coord_task)
    g = (await _fan_in_open(cctx, {"expect_replies": 1, "quorum": 1, "result_schema": _SCHEMA}))[
        "group_id"
    ]
    d = await _dispatch_task(
        cctx, {"agent": "rev-1", "goal": "g", "acceptance": "a", "fan_in": {"group_id": g, "leg_key": 0}}
    )
    with pytest.raises(ToolError, match="result_schema"):
        await _mailbox_send(
            _ctx(repos, "rev-1", d["task_id"], "wk-a"),
            {
                "to": "coordinator-1",
                "body": "x",
                "result_for": g,
                "leg_key": 0,
                "result": {"wrong_field": "no verdict"},
                "_tool_use_id": "tu",
            },
        )
    # Nothing entered the pipe.
    batch = await repos.outbox.dequeue_batch(limit=10)
    assert batch == []


@pytest.mark.asyncio
async def test_forged_result_rejected_by_lineage(
    repos: SqliteRepositories,
) -> None:
    coord_task = await _setup(repos)
    cctx = _ctx(repos, "coordinator-1", coord_task)
    g = (await _fan_in_open(cctx, {"expect_replies": 2, "quorum": 2, "result_schema": _SCHEMA}))[
        "group_id"
    ]
    d1 = await _dispatch_task(
        cctx, {"agent": "rev-1", "goal": "g", "acceptance": "a", "fan_in": {"group_id": g, "leg_key": 0}}
    )
    d2 = await _dispatch_task(
        cctx, {"agent": "rev-2", "goal": "g", "acceptance": "a", "fan_in": {"group_id": g, "leg_key": 1}}
    )
    # rev-2 tries to submit rev-1's leg (leg_key=0) — must be refused.
    with pytest.raises(ToolError, match="do not own"):
        await _send_result(repos, "rev-2", d2["task_id"], g, 0, {"verdict": "approve"})
    # rev-1's own leg still works.
    await _send_result(repos, "rev-1", d1["task_id"], g, 0, {"verdict": "approve"})


@pytest.mark.asyncio
async def test_duplicate_leg_key_rejected_at_dispatch(
    repos: SqliteRepositories,
) -> None:
    coord_task = await _setup(repos)
    cctx = _ctx(repos, "coordinator-1", coord_task)
    g = (await _fan_in_open(cctx, {"expect_replies": 2, "quorum": 2, "result_schema": _SCHEMA}))[
        "group_id"
    ]
    await _dispatch_task(
        cctx, {"agent": "rev-1", "goal": "g", "acceptance": "a", "fan_in": {"group_id": g, "leg_key": 0}}
    )
    with pytest.raises(ToolError, match="already taken"):
        await _dispatch_task(
            cctx, {"agent": "rev-2", "goal": "g", "acceptance": "a", "fan_in": {"group_id": g, "leg_key": 0}}
        )


@pytest.mark.asyncio
async def test_deadline_expiry_resolves_partial(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    coord_task = await _setup(repos)
    cctx = _ctx(repos, "coordinator-1", coord_task)
    g = (
        await _fan_in_open(
            cctx, {"expect_replies": 2, "quorum": 2, "result_schema": _SCHEMA, "deadline_in_s": 60}
        )
    )["group_id"]
    # Force the deadline into the past — no results delivered.
    await repos.conn.execute(
        "UPDATE fan_in_groups SET deadline = '2000-01-01T00:00:00+00:00' WHERE id = ?", (g,)
    )
    await repos.conn.commit()
    await _scheduler(repos, tmp_path)._resolve_fan_in_barriers()
    grp = await repos.fan_in.get(g)
    assert grp is not None and grp.status == "expired"


@pytest.mark.asyncio
async def test_fan_in_status_and_cancel(
    repos: SqliteRepositories,
) -> None:
    coord_task = await _setup(repos)
    cctx = _ctx(repos, "coordinator-1", coord_task)
    g = (await _fan_in_open(cctx, {"expect_replies": 2, "quorum": 2, "result_schema": _SCHEMA}))[
        "group_id"
    ]
    st = await _fan_in_status(cctx, {"group_id": g})
    assert st["status"] == "open" and st["delivered"] == 0 and st["quorum"] == 2

    cancelled = await _fan_in_cancel(cctx, {"group_id": g})
    assert cancelled["cancelled"] is True
    grp = await repos.fan_in.get(g)
    assert grp is not None and grp.status == "cancelled"
    # A second cancel is a guarded no-op (already terminal).
    again = await _fan_in_cancel(cctx, {"group_id": g})
    assert again["cancelled"] is False


@pytest.mark.asyncio
async def test_fan_in_index_exists_and_count_is_correct(
    repos: SqliteRepositories,
) -> None:
    # The barrier-count expression index must be registered (the bounded-scan
    # guard). recipient is already indexed, so even if the planner declines
    # this expression index the count stays bounded by the coordinator's inbox.
    async with repos.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='mailbox_messages_fan_in'"
    ) as cur:
        assert await cur.fetchone() is not None
    # COUNT(DISTINCT leg_key) is correct and idempotent: re-delivering the same
    # logical result (same external_id) collapses to one leg.
    coord_task = await _setup(repos)
    cctx = _ctx(repos, "coordinator-1", coord_task)
    g = (await _fan_in_open(cctx, {"expect_replies": 1, "quorum": 1, "result_schema": _SCHEMA}))[
        "group_id"
    ]
    d = await _dispatch_task(
        cctx, {"agent": "rev-1", "goal": "g", "acceptance": "a", "fan_in": {"group_id": g, "leg_key": 0}}
    )
    await _send_result(repos, "rev-1", d["task_id"], g, 0, {"verdict": "approve"})
    await OutboxDispatcher(repos).tick()
    await OutboxDispatcher(repos).tick()  # redelivery is idempotent
    assert await repos.mailbox.count_fan_in_results("coordinator-1", g) == 1


@pytest.mark.asyncio
async def test_late_result_to_resolved_group_is_accepted(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    """The burn loop this kills: quorum < expect_replies resolves the group
    while a straggler still runs; its result used to be REJECTED, so the leg
    completed 'without a typed result', got downgraded to failed, and a
    transient restart re-ran the whole wakeup — late again, forever. A late
    result to a quorum_met/expired group is idempotent low-urgency mail; the
    coordinator just ignores it. Only `cancelled` still refuses."""
    coord_task = await _setup(repos)
    cctx = _ctx(repos, "coordinator-1", coord_task)
    g = (await _fan_in_open(
        cctx, {"expect_replies": 2, "quorum": 1, "result_schema": _SCHEMA}
    ))["group_id"]
    d1 = await _dispatch_task(
        cctx, {"agent": "rev-1", "goal": "g", "acceptance": "a",
               "fan_in": {"group_id": g, "leg_key": 0}}
    )
    d2 = await _dispatch_task(
        cctx, {"agent": "rev-2", "goal": "g", "acceptance": "a",
               "fan_in": {"group_id": g, "leg_key": 1}}
    )
    await _send_result(repos, "rev-1", d1["task_id"], g, 0, {"verdict": "approve"})
    await OutboxDispatcher(repos).tick()
    sched = _scheduler(repos, tmp_path)
    await sched._resolve_fan_in_barriers()
    grp = await repos.fan_in.get(g)
    assert grp is not None and grp.status == "quorum_met"

    # The straggler finishes AFTER resolution — submission must succeed.
    out = await _send_result(
        repos, "rev-2", d2["task_id"], g, 1, {"verdict": "reject"}
    )
    assert out["status"] in ("queued", "sent")


@pytest.mark.asyncio
async def test_result_to_cancelled_group_still_rejected(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    coord_task = await _setup(repos)
    cctx = _ctx(repos, "coordinator-1", coord_task)
    g = (await _fan_in_open(
        cctx, {"expect_replies": 2, "quorum": 2, "result_schema": _SCHEMA}
    ))["group_id"]
    d1 = await _dispatch_task(
        cctx, {"agent": "rev-1", "goal": "g", "acceptance": "a",
               "fan_in": {"group_id": g, "leg_key": 0}}
    )
    await _fan_in_cancel(cctx, {"group_id": g})

    with pytest.raises(ToolError, match="cancelled — result rejected"):
        await _send_result(
            repos, "rev-1", d1["task_id"], g, 0, {"verdict": "approve"}
        )


@pytest.mark.asyncio
async def test_result_to_archived_coordinator_is_accepted_and_discarded(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    """An ephemeral coordinator can aggregate at quorum and get reaped
    before a straggler finishes. Raising would error the leg every turn and
    burn its restart budget on an undeliverable result — accept-and-drop so
    the leg can close out."""
    coord_task = await _setup(repos)
    cctx = _ctx(repos, "coordinator-1", coord_task)
    g = (await _fan_in_open(
        cctx, {"expect_replies": 2, "quorum": 1, "result_schema": _SCHEMA}
    ))["group_id"]
    d1 = await _dispatch_task(
        cctx, {"agent": "rev-1", "goal": "g", "acceptance": "a",
               "fan_in": {"group_id": g, "leg_key": 0}}
    )
    await repos.agents.archive("coordinator-1", reason="reaped")

    out = await _send_result(repos, "rev-1", d1["task_id"], g, 0, {"verdict": "ok"})
    assert out["status"] == "discarded"
    assert "coordinator" in out["note"]


@pytest.mark.asyncio
async def test_fan_in_cancel_flags_inflight_legs_to_stand_down(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    """'The coordinator asked the legs to stand down' must be mechanism,
    not aspiration: cancel propagates the durable B2 cancel flag to every
    roster member's child task, so legs stop at their next tool boundary
    instead of finishing work whose submission would be rejected."""
    coord_task = await _setup(repos)
    cctx = _ctx(repos, "coordinator-1", coord_task)
    g = (await _fan_in_open(
        cctx, {"expect_replies": 2, "quorum": 2, "result_schema": _SCHEMA}
    ))["group_id"]
    d1 = await _dispatch_task(
        cctx, {"agent": "rev-1", "goal": "g", "acceptance": "a",
               "fan_in": {"group_id": g, "leg_key": 0}}
    )
    d2 = await _dispatch_task(
        cctx, {"agent": "rev-2", "goal": "g", "acceptance": "a",
               "fan_in": {"group_id": g, "leg_key": 1}}
    )
    out = await _fan_in_cancel(cctx, {"group_id": g})
    assert out["cancelled"] is True
    assert sorted(out["legs_stopped"]) == sorted([d1["task_id"], d2["task_id"]])
    for tid in (d1["task_id"], d2["task_id"]):
        t = await repos.tasks.get(tid)
        assert t is not None and (t.metadata or {}).get("cancel_requested")


@pytest.mark.asyncio
async def test_leg_contract_voided_for_cancelled_group(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    """A leg finishing without a result on a CANCELLED group did exactly
    what it was told — the O2 contract check must return None (void), not
    False (downgrade-to-failed), or cancel itself becomes a burn loop."""
    coord_task = await _setup(repos)
    cctx = _ctx(repos, "coordinator-1", coord_task)
    g = (await _fan_in_open(
        cctx, {"expect_replies": 1, "quorum": 1, "result_schema": _SCHEMA}
    ))["group_id"]
    d1 = await _dispatch_task(
        cctx, {"agent": "rev-1", "goal": "g", "acceptance": "a",
               "fan_in": {"group_id": g, "leg_key": 0}}
    )
    await _fan_in_cancel(cctx, {"group_id": g})

    sched = _scheduler(repos, tmp_path)
    leg_task = await repos.tasks.get(d1["task_id"])
    assert leg_task is not None
    verdict = await sched._fan_in_leg_submitted_result(leg_task, d1["task_id"])
    assert verdict is None, "cancelled group must void the contract, not fail it"
