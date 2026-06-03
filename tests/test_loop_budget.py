"""T4: a recurring self-mail with max_occurrences is a budgeted loop.

AGENT_THREADS §5 — loop = budgeted recurring self-mail, no new machinery. The
scheduler enforces the cap in Phase -1: it stops re-arming once the cap is
reached and marks the final wake high-urgency, so an opt-in loop can't run
forever regardless of what the model wants.
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

_PAST_ISO = "2020-01-01T00:00:00.000Z"  # sorts before any real `now`


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


async def _seed(repos: SqliteRepositories) -> None:
    await repos.personas.upsert(
        Persona(name="dispatcher", role_description="d", system_prompt="d")
    )
    await repos.agents.create(agent_id="dispatcher", persona_name="dispatcher")
    await repos.mailbox.ensure_mailbox("dispatcher")


async def _force_due(repos: SqliteRepositories, sid: int) -> None:
    await repos.scheduled_mail.conn.execute(
        "UPDATE scheduled_mail SET scheduled_for=? WHERE id=?", (_PAST_ISO, sid)
    )
    await repos.scheduled_mail.conn.commit()


def _loop_spec(*, max_occurrences: int | None) -> ScheduledMail:
    return ScheduledMail(
        recipient="dispatcher", sender="dispatcher", urgency="normal",
        body="iterate", scheduled_for=datetime.now(UTC) - timedelta(minutes=5),
        recur_kind="interval", recur_value="1m",
        recur_until=datetime.now(UTC) + timedelta(days=7),
        max_occurrences=max_occurrences, created_by_agent="dispatcher",
    )


@pytest.mark.asyncio
async def test_max_occurrences_caps_the_recurring_loop(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    await _seed(repos)
    sid = await repos.scheduled_mail.create(_loop_spec(max_occurrences=2))
    sched = _make_scheduler(repos, cfg)

    # Iteration 1: below the cap → delivered normally, re-armed (still pending).
    await sched._deliver_scheduled_mail()
    s = await repos.scheduled_mail.get(sid)
    assert s is not None and s.status == "pending" and s.occurrence_count == 1

    # Iteration 2 == the cap → final wake; the scheduler stops re-arming.
    await _force_due(repos, sid)
    await sched._deliver_scheduled_mail()
    s = await repos.scheduled_mail.get(sid)
    assert s is not None and s.status == "completed" and s.occurrence_count == 2

    # A further tick delivers nothing (completed rows aren't `find_ready`).
    await sched._deliver_scheduled_mail()
    msgs = await repos.mailbox.read_messages("dispatcher", since_id=0)
    assert len(msgs) == 2
    finals = [m for m in msgs if m.urgency == "high"]
    assert len(finals) == 1 and "loop budget reached" in finals[0].body
    assert [m for m in msgs if m.urgency == "normal"]  # iteration 1 was normal


@pytest.mark.asyncio
async def test_unbounded_recurrence_is_unaffected(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    # Back-compat: no max_occurrences → keeps re-arming, no forced high-urgency.
    cfg = _config(tmp_path)
    await _seed(repos)
    sid = await repos.scheduled_mail.create(_loop_spec(max_occurrences=None))
    sched = _make_scheduler(repos, cfg)
    await sched._deliver_scheduled_mail()
    s = await repos.scheduled_mail.get(sid)
    assert s is not None and s.status == "pending" and s.occurrence_count == 1
    msgs = await repos.mailbox.read_messages("dispatcher", since_id=0)
    assert msgs[-1].urgency == "normal"


@pytest.mark.asyncio
async def test_mailbox_send_arms_a_bounded_loop(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    await _seed(repos)
    # A real task backs the FK on created_by_task / task_id.
    task_id = await repos.tasks.create(
        TaskSpec(agent_id="dispatcher", goal="g", acceptance="a")
    )
    wakeup_id = await repos.wakeups.start(task_id, "dispatcher", agent_id="dispatcher")
    ctx = ToolContext(
        repos=repos, task_id=task_id, wakeup_id=wakeup_id,
        persona_name="dispatcher", agent_id="dispatcher",
    )
    res = await MAILBOX_SEND.handler(
        ctx,
        {
            # deliver_in anchors the first fire; recur_every sets the cadence.
            "to": "dispatcher", "body": "loop step",
            "deliver_in": "1h", "recur_every": "1h",
            "max_occurrences": 5, "_tool_use_id": "tu",
        },
    )
    sid = res["scheduled_ids"][0]
    s = await repos.scheduled_mail.get(sid)
    assert s is not None
    assert s.max_occurrences == 5
    assert s.recur_kind == "interval"
