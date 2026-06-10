"""H2: the no-progress gate on budgeted self-mail loops.

A recurring self-mail with max_occurrences stops after N fires — but inside
that allowance a loop can spin uselessly ("沿无意义路径继续"). The gate adds
the missing axis: stop after K CONSECUTIVE rounds that did real work (above
the floor) yet produced no outward output on the loop's thread. Waiting
heartbeats (below the floor) are never counted; any visible progress resets
the counter. The scheduler enforces the cap in Phase -1, same stop path as
the T4 occurrence budget.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lyre.config import Config
from lyre.persistence.models import Persona, ScheduledMail, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.tools import ToolContext
from lyre.runtime.tools.mailbox import MAILBOX_SEND
from lyre.scheduler.scheduler import Scheduler

from .helpers import fake_entry, fake_registry

_PAST_ISO = "2020-01-01T00:00:00.000Z"   # window start: before any signal row
_ANCIENT_ISO = "2019-01-01T00:00:00.000Z"  # backdated task creation
_THREAD = "th-loop-1"


def _config(tmp_path: Path) -> Config:
    return Config(
        db_path=str(tmp_path / "lyre.db"),
        object_store_path=tmp_path / "obj",
        memory_path=None,
        anthropic_api_key=None,
        anthropic_base_url=None,
        default_model="m",
        model_override=None,
    )


def _make_scheduler(repos: SqliteRepositories, cfg: Config) -> Scheduler:
    return Scheduler(
        repos, cfg, poll_interval_s=0.05,
        registry=fake_registry(fake_entry(id="m", tier="flagship")),
    )


async def _seed(repos: SqliteRepositories) -> str:
    """Dispatcher agent + a long-lived thread task whose created_at is
    backdated so creating it doesn't read as thread activity. Returns the
    task id (the loop's working task that wakeups attach to)."""
    await repos.personas.upsert(
        Persona(name="dispatcher", role_description="d", system_prompt="d")
    )
    await repos.agents.create(agent_id="dispatcher", persona_name="dispatcher")
    await repos.mailbox.ensure_mailbox("dispatcher")
    task_id = await repos.tasks.create(
        TaskSpec(
            agent_id="dispatcher", goal="loop work", acceptance="a",
            metadata={"thread_id": _THREAD},
        )
    )
    await repos.conn.execute(
        "UPDATE tasks SET created_at = ? WHERE id = ?", (_ANCIENT_ISO, task_id)
    )
    await repos.conn.commit()
    return task_id


def _loop_spec(*, max_no_progress: int | None) -> ScheduledMail:
    return ScheduledMail(
        recipient="dispatcher", sender="dispatcher", urgency="normal",
        body="iterate", scheduled_for=datetime.now(UTC) - timedelta(minutes=5),
        recur_kind="interval", recur_value="1m",
        recur_until=datetime.now(UTC) + timedelta(days=7),
        max_no_progress=max_no_progress, created_by_agent="dispatcher",
        metadata={"thread_id": _THREAD},
    )


async def _force_due_with_window(repos: SqliteRepositories, sid: int) -> None:
    """Make the row due again AND pin the window start to a known past
    instant, so signal rows created by the test (with natural `now`
    timestamps) deterministically fall inside the window."""
    await repos.conn.execute(
        "UPDATE scheduled_mail SET scheduled_for = ?, last_delivered_at = ? "
        "WHERE id = ?",
        (_PAST_ISO, _PAST_ISO, sid),
    )
    await repos.conn.commit()


async def _worked_wakeup(
    repos: SqliteRepositories, task_id: str, *, tools: int, out_tokens: int
) -> None:
    wk = await repos.wakeups.start(task_id, "dispatcher", agent_id="dispatcher")
    await repos.wakeups.end(
        wk, end_status="completed",
        metering={"tool_call_count": tools, "token_output": out_tokens},
    )


async def _np(repos: SqliteRepositories, sid: int) -> tuple[int, str]:
    s = await repos.scheduled_mail.get(sid)
    assert s is not None
    return (s.no_progress_count, s.status)


# ---------------------------------------------------------------------------
# The gate trips
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consecutive_worked_silent_rounds_trip_the_gate(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    task_id = await _seed(repos)
    sid = await repos.scheduled_mail.create(_loop_spec(max_no_progress=2))
    sched = _make_scheduler(repos, cfg)

    # Occurrence 1: no previous window — never counted.
    await sched._deliver_scheduled_mail()
    assert await _np(repos, sid) == (0, "pending")

    # Round 1: real work, zero outward output → count 1, still pending.
    await _force_due_with_window(repos, sid)
    await _worked_wakeup(repos, task_id, tools=10, out_tokens=50)
    await sched._deliver_scheduled_mail()
    assert await _np(repos, sid) == (1, "pending")

    # Round 2: same again → cap reached → FINAL wake, no re-arm.
    await _force_due_with_window(repos, sid)
    await sched._deliver_scheduled_mail()
    count, status = await _np(repos, sid)
    assert (count, status) == (2, "completed")

    msgs = await repos.mailbox.read_messages("dispatcher", since_id=0)
    finals = [m for m in msgs if m.urgency == "high"]
    assert len(finals) == 1
    assert "no-progress gate reached" in finals[0].body

    # A further tick delivers nothing (the row completed).
    await sched._deliver_scheduled_mail()
    assert len(await repos.mailbox.read_messages("dispatcher", since_id=0)) == 3


# ---------------------------------------------------------------------------
# Progress resets; waiters and ungated loops are untouched
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nonself_thread_mail_resets_the_counter(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    from lyre.persistence.models import MailboxMessage

    cfg = _config(tmp_path)
    task_id = await _seed(repos)
    sid = await repos.scheduled_mail.create(_loop_spec(max_no_progress=2))
    sched = _make_scheduler(repos, cfg)

    await sched._deliver_scheduled_mail()
    await _force_due_with_window(repos, sid)
    await _worked_wakeup(repos, task_id, tools=10, out_tokens=50)
    await sched._deliver_scheduled_mail()
    assert (await _np(repos, sid))[0] == 1

    # Outward output: the agent mailed someone else on the thread. (The
    # loop's own self→self wakes never count — they'd otherwise feed the
    # gate with its own deliveries.)
    await repos.mailbox.ensure_mailbox("owner")
    await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="owner", external_id="x-1", sender="dispatcher",
            urgency="normal", body="found it",
            metadata={"thread_id": _THREAD},
        )
    )
    await _force_due_with_window(repos, sid)
    await sched._deliver_scheduled_mail()
    assert await _np(repos, sid) == (0, "pending")


@pytest.mark.asyncio
async def test_checkpoint_progress_resets_the_counter(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    task_id = await _seed(repos)
    sid = await repos.scheduled_mail.create(_loop_spec(max_no_progress=2))
    sched = _make_scheduler(repos, cfg)

    await sched._deliver_scheduled_mail()
    await _force_due_with_window(repos, sid)
    await _worked_wakeup(repos, task_id, tools=10, out_tokens=50)
    await sched._deliver_scheduled_mail()
    assert (await _np(repos, sid))[0] == 1

    # report_progress writes the checkpoint → checkpoint_updated_at moves
    # (updated_at alone is deliberately NOT trusted — lease churn touches it).
    wk = await repos.wakeups.start(task_id, "dispatcher", agent_id="dispatcher")
    assert await repos.tasks.claim_lease(task_id, wk, duration_sec=600)
    await repos.tasks.update_checkpoint(task_id, {"step": "found-lead"}, wk)
    await _force_due_with_window(repos, sid)
    await sched._deliver_scheduled_mail()
    assert (await _np(repos, sid))[0] == 0


@pytest.mark.asyncio
async def test_waiting_heartbeat_below_floor_is_not_counted(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    task_id = await _seed(repos)
    sid = await repos.scheduled_mail.create(_loop_spec(max_no_progress=1))
    sched = _make_scheduler(repos, cfg)

    await sched._deliver_scheduled_mail()
    # Wake → peek at the inbox (1 tool call, tiny output) → sleep. Even with
    # cap=1 this must never trip: waiters are not thrashers.
    for _ in range(3):
        await _force_due_with_window(repos, sid)
        await _worked_wakeup(repos, task_id, tools=1, out_tokens=20)
        await sched._deliver_scheduled_mail()
        assert await _np(repos, sid) == (0, "pending")


@pytest.mark.asyncio
async def test_loop_without_gate_is_unaffected(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    task_id = await _seed(repos)
    sid = await repos.scheduled_mail.create(_loop_spec(max_no_progress=None))
    sched = _make_scheduler(repos, cfg)

    await sched._deliver_scheduled_mail()
    for _ in range(3):
        await _force_due_with_window(repos, sid)
        await _worked_wakeup(repos, task_id, tools=10, out_tokens=5000)
        await sched._deliver_scheduled_mail()
    s = await repos.scheduled_mail.get(sid)
    assert s is not None and s.status == "pending" and s.no_progress_count == 0


@pytest.mark.asyncio
async def test_evaluation_is_idempotent_for_one_window(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    """Crash between mailbox insert and mark_delivered re-delivers and
    re-evaluates the SAME window (last_delivered_at unchanged) — the count
    must come out identical, never double-incremented."""
    cfg = _config(tmp_path)
    task_id = await _seed(repos)
    sid = await repos.scheduled_mail.create(_loop_spec(max_no_progress=3))
    sched = _make_scheduler(repos, cfg)

    await sched._deliver_scheduled_mail()
    await _force_due_with_window(repos, sid)
    await _worked_wakeup(repos, task_id, tools=10, out_tokens=50)
    s = await repos.scheduled_mail.get(sid)
    assert s is not None
    first = await sched._evaluate_no_progress(s)
    second = await sched._evaluate_no_progress(s)
    assert first == second == (1, False)


# ---------------------------------------------------------------------------
# Tool surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mailbox_send_arms_the_gate(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    await _seed(repos)
    task_id = await repos.tasks.create(
        TaskSpec(agent_id="dispatcher", goal="g", acceptance="a")
    )
    wk = await repos.wakeups.start(task_id, "dispatcher", agent_id="dispatcher")
    ctx = ToolContext(
        repos=repos, task_id=task_id, wakeup_id=wk,
        persona_name="dispatcher", agent_id="dispatcher",
    )
    res = await MAILBOX_SEND.handler(
        ctx,
        {
            "to": "dispatcher", "body": "loop step",
            "deliver_in": "1h", "recur_every": "1h",
            "max_occurrences": 24, "max_no_progress": 3,
            "_tool_use_id": "tu",
        },
    )
    s = await repos.scheduled_mail.get(res["scheduled_ids"][0])
    assert s is not None and s.max_no_progress == 3

    # One-shot mail can't carry the gate — there's no loop to stop.
    from lyre.runtime.tools import ToolError

    with pytest.raises(ToolError, match="max_no_progress only applies"):
        await MAILBOX_SEND.handler(
            ctx,
            {
                "to": "dispatcher", "body": "once", "deliver_in": "1h",
                "max_no_progress": 3, "_tool_use_id": "tu2",
            },
        )
