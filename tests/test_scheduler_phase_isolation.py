"""Scheduler phase isolation: one poison row must fail ALONE.

Before these guards, one exception anywhere in Phase −1/0.5/0 aborted the
whole tick — run() caught it, the next tick failed at the same row, and a
single corrupt scheduled_mail row starved every dispatch phase forever.
Now: rows are isolated, repeat offenders are quarantined (scheduled_mail
durably via status='quarantined'; fan-in groups through their existing
terminal 'expired' path), and a failing phase never blocks the phases
behind it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from lyre.persistence.models import (
    FanInGroup,
    MailboxMessage,
    Persona,
    ScheduledMail,
)
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.scheduler.scheduler import (
    _FANIN_QUARANTINE_AFTER,
    _SCHEDMAIL_QUARANTINE_AFTER,
    Scheduler,
)

from .helpers import fake_entry, fake_registry


async def _seed_agents(repos: SqliteRepositories) -> None:
    for name in ("dispatcher", "worker"):
        await repos.personas.upsert(
            Persona(name=name, role_description=name, system_prompt=name)
        )
    await repos.agents.create(agent_id="dispatcher", persona_name="dispatcher")
    await repos.agents.create(agent_id="worker-1", persona_name="worker")
    await repos.mailbox.ensure_mailbox("dispatcher")
    await repos.mailbox.ensure_mailbox("worker-1")


def _scheduler(repos: SqliteRepositories, tmp_path: Path) -> Scheduler:
    from lyre.config import Config

    return Scheduler(
        repos,
        Config(
            db_path=str(tmp_path / "lyre.db"),  # type: ignore[arg-type]
            object_store_path=tmp_path / "obj",
            memory_path=None,  # type: ignore[arg-type]
            anthropic_api_key=None,
            anthropic_base_url=None,
            default_model="m",
        ),
        poll_interval_s=0.05,
        registry=fake_registry(fake_entry(id="m", tier="flagship")),
    )


def _due_mail(recipient: str, creator: str = "worker-1") -> ScheduledMail:
    return ScheduledMail(
        recipient=recipient,
        sender=creator,
        body=f"hello {recipient}",
        scheduled_for=datetime.now(UTC) - timedelta(seconds=5),
        created_by_agent=creator,
    )


def _poison_no_progress(scheduler: Scheduler, poison_recipient: str) -> None:
    """Make delivery of rows to `poison_recipient` raise inside the loop
    body, deterministically — stands in for any corrupt-row failure."""
    original = scheduler._evaluate_no_progress

    async def _maybe_boom(sched: ScheduledMail) -> tuple[int, bool]:
        if sched.recipient == poison_recipient:
            raise RuntimeError("corrupt row: boom")
        return await original(sched)

    scheduler._evaluate_no_progress = _maybe_boom  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Phase −1: scheduled mail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poison_scheduled_mail_does_not_block_other_due_rows(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    await _seed_agents(repos)
    poison_id = await repos.scheduled_mail.create(_due_mail("dispatcher"))
    await repos.scheduled_mail.create(_due_mail("worker-1"))
    scheduler = _scheduler(repos, tmp_path)
    _poison_no_progress(scheduler, "dispatcher")

    await scheduler._deliver_scheduled_mail()

    # The healthy row delivered despite the poison row preceding it
    # (find_ready orders by scheduled_for; both are due).
    worker_mail = await repos.mailbox.read_unread("worker-1", limit=10)
    assert any(m.body == "hello worker-1" for m in worker_mail)
    # The poison row took a failure count but stays pending (retry next
    # tick — a transient failure must not be punished as poison).
    rows = await repos.scheduled_mail.list_filtered(status="all", limit=10)
    poison = next(r for r in rows if r.id == poison_id)
    assert poison.delivery_failure_count == 1
    assert poison.status == "pending"


@pytest.mark.asyncio
async def test_poison_scheduled_mail_quarantined_after_threshold(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    await _seed_agents(repos)
    poison_id = await repos.scheduled_mail.create(_due_mail("dispatcher"))
    scheduler = _scheduler(repos, tmp_path)
    _poison_no_progress(scheduler, "dispatcher")

    for _ in range(_SCHEDMAIL_QUARANTINE_AFTER):
        await scheduler._deliver_scheduled_mail()

    rows = await repos.scheduled_mail.list_filtered(status="all", limit=10)
    poison = next(r for r in rows if r.id == poison_id)
    assert poison.status == "quarantined"
    assert poison.delivery_failure_count == _SCHEDMAIL_QUARANTINE_AFTER
    assert poison.bounce_reason is not None
    assert "boom" in poison.bounce_reason
    # find_ready (status='pending' filter) stops returning it — the
    # retry loop is broken durably, surviving restarts.
    from lyre.runtime.future_mail import iso, now_utc

    assert await repos.scheduled_mail.find_ready(iso(now_utc())) == []
    # The creator was told, idempotently.
    creator_mail = await repos.mailbox.read_unread("worker-1", limit=10)
    notices = [
        m for m in creator_mail
        if m.external_id == f"sched-quarantine:{poison_id}"
    ]
    assert len(notices) == 1
    assert "QUARANTINED" in notices[0].body


@pytest.mark.asyncio
async def test_successful_delivery_resets_the_failure_count(
    repos: SqliteRepositories,
) -> None:
    """The counter means CONSECUTIVE failures: a healthy recurring row hit
    by transient blips spread over its lifetime must never accumulate to
    quarantine (review finding: the counter originally counted lifetime
    failures — three DB-lock blips months apart silently killed a daily
    standup mail)."""
    await _seed_agents(repos)
    row_id = await repos.scheduled_mail.create(_due_mail("dispatcher"))

    for _ in range(_SCHEDMAIL_QUARANTINE_AFTER - 1):
        quarantined = await repos.scheduled_mail.record_delivery_failure(
            row_id, "transient blip", _SCHEDMAIL_QUARANTINE_AFTER
        )
        assert quarantined is False
    # A successful delivery (recurring re-arm) resets the streak.
    # last_delivery_id is FK-protected — use a real delivered message.
    msg_id = await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="dispatcher", external_id="delivered-ok",
            sender="worker-1", urgency="normal", body="hello",
        )
    )
    await repos.scheduled_mail.mark_delivered(
        mail_id=row_id, delivered_msg_id=msg_id,
        next_scheduled_for="2999-01-01T00:00:00Z", completed=False,
    )
    quarantined = await repos.scheduled_mail.record_delivery_failure(
        row_id, "another blip", _SCHEDMAIL_QUARANTINE_AFTER
    )
    assert quarantined is False
    rows = await repos.scheduled_mail.list_filtered(status="all", limit=10)
    row = next(r for r in rows if r.id == row_id)
    assert row.status == "pending"
    assert row.delivery_failure_count == 1


# ---------------------------------------------------------------------------
# Tick-level: a failing phase never blocks the phases behind it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failing_phase_does_not_block_later_phases(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    await _seed_agents(repos)
    scheduler = _scheduler(repos, tmp_path)

    async def _boom() -> None:
        raise RuntimeError("phase -1 exploded")

    reached: list[str] = []

    async def _spy() -> None:
        reached.append("phase 0")

    scheduler._deliver_scheduled_mail = _boom  # type: ignore[method-assign]
    scheduler._auto_dispatch_for_unread_mail = _spy  # type: ignore[method-assign]

    await scheduler._tick()  # must not raise

    assert reached == ["phase 0"]


# ---------------------------------------------------------------------------
# Phase 0.5: fan-in groups
# ---------------------------------------------------------------------------


def _group(gid: str, deadline: datetime) -> FanInGroup:
    return FanInGroup(
        id=gid,
        coordinator_agent_id="dispatcher",
        expect_replies=2,
        quorum=2,
        result_schema={},
        deadline=deadline,
    )


def _poison_fan_in_count(repos: SqliteRepositories, poison_gid: str) -> None:
    original = repos.mailbox.count_fan_in_results

    async def _maybe_boom(coordinator: str, group_id: str) -> int:
        if group_id == poison_gid:
            raise RuntimeError("corrupt group: boom")
        return await original(coordinator, group_id)

    repos.mailbox.count_fan_in_results = _maybe_boom  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_poison_fan_in_group_is_isolated_then_force_expired(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    await _seed_agents(repos)
    past = datetime.now(UTC) - timedelta(minutes=1)
    await repos.fan_in.create_group(_group("poison-g", past))
    await repos.fan_in.create_group(_group("healthy-g", past))
    scheduler = _scheduler(repos, tmp_path)
    _poison_fan_in_count(repos, "poison-g")

    await scheduler._resolve_fan_in_barriers()

    # The healthy group resolved (deadline passed) despite the poison one.
    healthy = await repos.fan_in.get("healthy-g")
    assert healthy is not None and healthy.status == "expired"
    poison = await repos.fan_in.get("poison-g")
    assert poison is not None and poison.status == "open"

    # Repeat offender: forced through the existing terminal path, with a
    # coordinator notice, instead of retrying every tick forever.
    for _ in range(_FANIN_QUARANTINE_AFTER - 1):
        await scheduler._resolve_fan_in_barriers()
    poison = await repos.fan_in.get("poison-g")
    assert poison is not None and poison.status == "expired"
    coordinator_mail = await repos.mailbox.read_unread("dispatcher", limit=10)
    assert any(
        m.external_id == "fanin:poison-g:quarantined" for m in coordinator_mail
    )


# ---------------------------------------------------------------------------
# Phase 0: auto-wake
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poison_agent_does_not_block_other_auto_wakes(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    await _seed_agents(repos)
    for recipient in ("dispatcher", "worker-1"):
        await repos.mailbox.insert_message(
            MailboxMessage(
                recipient=recipient, external_id=f"m-{recipient}",
                sender="owner", urgency="normal", body="wake up",
            )
        )
    scheduler = _scheduler(repos, tmp_path)
    original_create = repos.tasks.create

    async def _maybe_boom(spec: Any) -> str:
        if spec.agent_id == "dispatcher":
            raise RuntimeError("corrupt agent: boom")
        return await original_create(spec)

    repos.tasks.create = _maybe_boom  # type: ignore[method-assign]

    await scheduler._auto_dispatch_for_unread_mail()  # must not raise

    # dispatcher's wake failed alone; worker-1 still got its inbox task.
    worker_tasks = await repos.tasks.find_pending(limit=10)
    owners = {t.agent_id for t in worker_tasks}
    assert "worker-1" in owners
    assert "dispatcher" not in owners
