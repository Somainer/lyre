"""Tests for future / recurring mail.

Covers:
  - Helper layer: duration parsing, cron parsing, first-fire resolution,
    past-time rejection, recur_until cap, next-fire computation.
  - Tool layer: mailbox_send scheduling branch (validation + persistence).
  - Scheduler delivery: one-shot, interval recurrence, cron recurrence,
    recur_until horizon, archived recipient bounce.
  - Cancel.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lyre.config import Config
from lyre.persistence.models import (
    Persona,
    ScheduledMail,
)
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.future_mail import (
    PastDeliveryError,
    compute_next_fire,
    default_recur_until,
    iso,
    parse_duration,
    resolve_first_fire,
    validate_cron,
)
from lyre.runtime.tools import ToolContext, ToolError
from lyre.runtime.tools.mailbox import (
    CANCEL_SCHEDULED_MAIL,
    LIST_SCHEDULED_MAIL,
    MAILBOX_SEND,
)
from lyre.scheduler.scheduler import Scheduler

from .helpers import fake_entry, fake_registry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


async def _seed(repos: SqliteRepositories) -> None:
    for name in ("dispatcher", "worker-maintainer", "owner"):
        await repos.personas.upsert(
            Persona(name=name, role_description=name, system_prompt=name)
        )
    await repos.agents.create(agent_id="owner", persona_name="owner")
    await repos.agents.create(agent_id="dispatcher", persona_name="dispatcher")
    await repos.agents.create(
        agent_id="worker-1", persona_name="worker-maintainer"
    )
    await repos.mailbox.ensure_mailbox("owner")
    await repos.mailbox.ensure_mailbox("dispatcher")


def _make_scheduler(repos: SqliteRepositories, cfg: Config) -> Scheduler:
    return Scheduler(
        repos, cfg,
        poll_interval_s=0.05,
        registry=fake_registry(fake_entry(id="m", tier="flagship")),
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_parse_duration_basic() -> None:
    assert parse_duration("1m") == timedelta(minutes=1)
    assert parse_duration("30m") == timedelta(minutes=30)
    assert parse_duration("2h") == timedelta(hours=2)
    assert parse_duration("1d") == timedelta(days=1)
    assert parse_duration("1w") == timedelta(weeks=1)


def test_parse_duration_rejects_seconds() -> None:
    """Lyre is minute-grained; seconds shorthand must error to force the
    agent to think clearly about the time scale."""
    with pytest.raises(ValueError, match="second-grained"):
        parse_duration("30s")


def test_parse_duration_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_duration("forever")
    with pytest.raises(ValueError):
        parse_duration("0m")  # below minimum


def test_parse_duration_rejects_above_horizon() -> None:
    with pytest.raises(ValueError, match="1-year"):
        parse_duration("60w")  # ~14 months


def test_validate_cron_accepts_valid() -> None:
    validate_cron("0 9 * * 1-5")
    validate_cron("*/15 * * * *")


def test_validate_cron_rejects_invalid() -> None:
    with pytest.raises(ValueError):
        validate_cron("nope")
    with pytest.raises(ValueError):
        validate_cron("@daily")  # special strings not supported


def test_resolve_first_fire_relative() -> None:
    now = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
    fire = resolve_first_fire(None, "1h", None, now=now)
    assert fire == datetime(2026, 5, 17, 13, 0, tzinfo=UTC)


def test_resolve_first_fire_rejects_past_absolute() -> None:
    """S3: past deliver_at must error so the agent recomputes."""
    now = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
    with pytest.raises(PastDeliveryError) as exc_info:
        resolve_first_fire("2025-01-01T00:00:00Z", None, None, now=now)
    # The error includes current UTC so the agent can correct.
    assert "Current UTC is 2026-05-17" in str(exc_info.value)


def test_resolve_first_fire_rejects_beyond_horizon() -> None:
    now = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="1 year"):
        resolve_first_fire("2028-01-01T00:00:00Z", None, None, now=now)


def test_resolve_first_fire_cron_only() -> None:
    """recur_cron without deliver_at = first fire is next cron match."""
    now = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)  # Sunday
    fire = resolve_first_fire(None, None, "0 9 * * 1-5", now=now)
    # Next workday 9am from Sunday noon = Monday 9am
    assert fire == datetime(2026, 5, 18, 9, 0, tzinfo=UTC)


def test_compute_next_fire_interval() -> None:
    now = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
    nxt = compute_next_fire("interval", "1h", after=now)
    assert nxt == now + timedelta(hours=1)


def test_compute_next_fire_returns_none_past_until() -> None:
    """When the would-be next fire exceeds recur_until, returns None
    (caller marks the schedule completed)."""
    now = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
    end = now + timedelta(minutes=30)  # before next 1h fire
    assert compute_next_fire("interval", "1h", after=now, recur_until=end) is None


def test_default_recur_until_is_one_year() -> None:
    now = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
    until = default_recur_until(now)
    assert until == now + timedelta(days=365)


# ---------------------------------------------------------------------------
# Tool layer — mailbox_send scheduling branch
# ---------------------------------------------------------------------------


@pytest.fixture
async def ctx(repos: SqliteRepositories) -> ToolContext:
    await _seed(repos)
    task_id = await repos.tasks.create(
        # Use TaskSpec via direct insert to keep this independent of dispatch.
        __import__("lyre.persistence.models", fromlist=["TaskSpec"]).TaskSpec(
            agent_id="dispatcher", goal="g", acceptance="a"
        )
    )
    wakeup_id = await repos.wakeups.start(task_id, "dispatcher")
    return ToolContext(
        repos=repos, task_id=task_id, wakeup_id=wakeup_id,
        persona_name="dispatcher", agent_id="dispatcher",
    )


@pytest.mark.asyncio
async def test_mailbox_send_schedules_one_shot(ctx: ToolContext) -> None:
    """Pass deliver_in → row appears in scheduled_mail with pending status."""
    result = await MAILBOX_SEND.handler(
        ctx,
        {
            "to": "worker-1",
            "body": "follow up later",
            "deliver_in": "2h",
            "_tool_use_id": "tu1",
        },
    )
    assert result["status"] == "scheduled"
    assert len(result["scheduled_ids"]) == 1
    assert result["recur_kind"] is None

    rows = await ctx.repos.scheduled_mail.list_filtered(status="pending")
    assert len(rows) == 1
    assert rows[0].recipient == "worker-1"
    assert rows[0].recur_kind is None
    assert rows[0].body == "follow up later"


@pytest.mark.asyncio
async def test_mailbox_send_schedules_with_interval_recurrence(
    ctx: ToolContext,
) -> None:
    result = await MAILBOX_SEND.handler(
        ctx,
        {
            "to": "worker-1",
            "body": "weekly check",
            "deliver_in": "1w",
            "recur_every": "1w",
            "_tool_use_id": "tu1",
        },
    )
    assert result["recur_kind"] == "interval"
    assert result["recur_value"] == "1w"
    assert result["recur_until"] is not None  # default cap was applied


@pytest.mark.asyncio
async def test_mailbox_send_schedules_with_cron(ctx: ToolContext) -> None:
    result = await MAILBOX_SEND.handler(
        ctx,
        {
            "to": "worker-1",
            "body": "workday standup",
            "recur_cron": "0 9 * * 1-5",
            "_tool_use_id": "tu1",
        },
    )
    assert result["recur_kind"] == "cron"
    assert result["recur_value"] == "0 9 * * 1-5"


@pytest.mark.asyncio
async def test_mailbox_send_rejects_past_deliver_at(ctx: ToolContext) -> None:
    """S3: agent passes a past time → ToolError so it can re-compute."""
    with pytest.raises(ToolError, match="past"):
        await MAILBOX_SEND.handler(
            ctx,
            {
                "to": "worker-1",
                "body": "x",
                "deliver_at": "2020-01-01T00:00:00Z",
                "_tool_use_id": "tu1",
            },
        )


@pytest.mark.asyncio
async def test_mailbox_send_rejects_both_recurrence_styles(
    ctx: ToolContext,
) -> None:
    with pytest.raises(ToolError, match="at most one"):
        await MAILBOX_SEND.handler(
            ctx,
            {
                "to": "worker-1",
                "body": "x",
                "deliver_in": "1h",
                "recur_every": "1h",
                "recur_cron": "0 9 * * *",
                "_tool_use_id": "tu1",
            },
        )


@pytest.mark.asyncio
async def test_mailbox_send_rejects_invalid_cron(ctx: ToolContext) -> None:
    with pytest.raises(ToolError, match="cron"):
        await MAILBOX_SEND.handler(
            ctx,
            {
                "to": "worker-1",
                "body": "x",
                "recur_cron": "not a cron",
                "_tool_use_id": "tu1",
            },
        )


@pytest.mark.asyncio
async def test_mailbox_send_unknown_recipient_still_blocks_scheduled(
    ctx: ToolContext,
) -> None:
    """The recipient validation runs BEFORE the scheduling branch."""
    with pytest.raises(ToolError, match="unknown recipient"):
        await MAILBOX_SEND.handler(
            ctx,
            {
                "to": "ghost-agent",
                "body": "x",
                "deliver_in": "1h",
                "_tool_use_id": "tu1",
            },
        )


# ---------------------------------------------------------------------------
# Scheduler — Phase -1 delivery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase_minus_one_delivers_one_shot(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    """A due one-shot row produces a mailbox_message and marks completed."""
    cfg = _config(tmp_path)
    await _seed(repos)
    past = datetime.now(UTC) - timedelta(minutes=5)
    sid = await repos.scheduled_mail.create(
        ScheduledMail(
            recipient="dispatcher", sender="owner", urgency="normal",
            body="overdue", scheduled_for=past, created_by_agent="owner",
        )
    )

    sched = _make_scheduler(repos, cfg)
    await sched._deliver_scheduled_mail()

    row = await repos.scheduled_mail.get(sid)
    assert row.status == "completed"
    assert row.last_delivery_id is not None
    assert row.occurrence_count == 1
    msgs = await repos.mailbox.read_messages("dispatcher", since_id=0)
    assert any(m.body == "overdue" for m in msgs)


@pytest.mark.asyncio
async def test_phase_minus_one_recurring_advances_scheduled_for(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    """Recurring delivery mutates scheduled_for to next fire; status stays
    pending; occurrence_count increments."""
    cfg = _config(tmp_path)
    await _seed(repos)
    past = datetime.now(UTC) - timedelta(minutes=5)
    sid = await repos.scheduled_mail.create(
        ScheduledMail(
            recipient="dispatcher", sender="owner", urgency="normal",
            body="recur", scheduled_for=past,
            recur_kind="interval", recur_value="1h",
            recur_until=datetime.now(UTC) + timedelta(days=7),
            created_by_agent="owner",
        )
    )

    sched = _make_scheduler(repos, cfg)
    await sched._deliver_scheduled_mail()

    row = await repos.scheduled_mail.get(sid)
    assert row.status == "pending"
    assert row.occurrence_count == 1
    # next fire is in the future (~1h ahead of `past`, which was 5min ago)
    new_ts = row.scheduled_for
    new_str = (
        new_ts.isoformat() if hasattr(new_ts, "isoformat") else str(new_ts)
    )
    assert new_str > iso(datetime.now(UTC))


@pytest.mark.asyncio
async def test_phase_minus_one_completes_past_recur_until(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    """Last occurrence (next fire > recur_until) → status='completed'."""
    cfg = _config(tmp_path)
    await _seed(repos)
    past = datetime.now(UTC) - timedelta(minutes=5)
    # recur_until is BEFORE the next-would-be-fire (1h ahead).
    sid = await repos.scheduled_mail.create(
        ScheduledMail(
            recipient="dispatcher", sender="owner", urgency="normal",
            body="last", scheduled_for=past,
            recur_kind="interval", recur_value="1h",
            recur_until=datetime.now(UTC) - timedelta(minutes=1),
            created_by_agent="owner",
        )
    )

    sched = _make_scheduler(repos, cfg)
    await sched._deliver_scheduled_mail()

    row = await repos.scheduled_mail.get(sid)
    assert row.status == "completed"


@pytest.mark.asyncio
async def test_phase_minus_one_bounces_archived_recipient(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    """Archived recipient → bounce notice to creator + mark bounced."""
    cfg = _config(tmp_path)
    await _seed(repos)
    await repos.agents.archive("worker-1")
    past = datetime.now(UTC) - timedelta(minutes=5)
    sid = await repos.scheduled_mail.create(
        ScheduledMail(
            recipient="worker-1", sender="dispatcher", urgency="normal",
            body="ping", scheduled_for=past, created_by_agent="dispatcher",
        )
    )

    sched = _make_scheduler(repos, cfg)
    await sched._deliver_scheduled_mail()

    row = await repos.scheduled_mail.get(sid)
    assert row.status == "bounced"
    # Leader (creator) got a bounce notice
    msgs = await repos.mailbox.read_messages("dispatcher", since_id=0)
    bounce_msgs = [
        m for m in msgs
        if m.metadata and m.metadata.get("bounce") is True
    ]
    assert len(bounce_msgs) == 1
    assert "BOUNCE" in bounce_msgs[0].body


@pytest.mark.asyncio
async def test_phase_minus_one_skips_not_yet_due(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    await _seed(repos)
    future = datetime.now(UTC) + timedelta(hours=1)
    sid = await repos.scheduled_mail.create(
        ScheduledMail(
            recipient="dispatcher", sender="owner", urgency="normal",
            body="later", scheduled_for=future, created_by_agent="owner",
        )
    )
    sched = _make_scheduler(repos, cfg)
    await sched._deliver_scheduled_mail()
    row = await repos.scheduled_mail.get(sid)
    assert row.status == "pending"
    msgs = await repos.mailbox.read_messages("dispatcher", since_id=0)
    assert not any(m.body == "later" for m in msgs)


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_pending_schedule(ctx: ToolContext) -> None:
    res = await MAILBOX_SEND.handler(
        ctx,
        {
            "to": "worker-1", "body": "x",
            "deliver_in": "1d", "_tool_use_id": "tu1",
        },
    )
    sid = res["scheduled_ids"][0]
    cancel_res = await CANCEL_SCHEDULED_MAIL.handler(ctx, {"id": sid})
    assert cancel_res["cancelled"] is True
    row = await ctx.repos.scheduled_mail.get(sid)
    assert row.status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_idempotent_rejects_already_cancelled(
    ctx: ToolContext,
) -> None:
    res = await MAILBOX_SEND.handler(
        ctx,
        {"to": "worker-1", "body": "x", "deliver_in": "1d", "_tool_use_id": "t"},
    )
    sid = res["scheduled_ids"][0]
    await CANCEL_SCHEDULED_MAIL.handler(ctx, {"id": sid})
    with pytest.raises(ToolError, match="already cancelled"):
        await CANCEL_SCHEDULED_MAIL.handler(ctx, {"id": sid})


@pytest.mark.asyncio
async def test_cancel_stops_recurring(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    """After cancel, a re-tick must NOT deliver."""
    cfg = _config(tmp_path)
    await _seed(repos)
    past = datetime.now(UTC) - timedelta(minutes=5)
    sid = await repos.scheduled_mail.create(
        ScheduledMail(
            recipient="dispatcher", sender="owner", urgency="normal",
            body="r", scheduled_for=past,
            recur_kind="interval", recur_value="1h",
            recur_until=datetime.now(UTC) + timedelta(days=7),
            created_by_agent="owner",
        )
    )
    await repos.scheduled_mail.mark_cancelled(sid, "owner", "test")
    before = await repos.mailbox.read_messages("dispatcher", since_id=0)
    sched = _make_scheduler(repos, cfg)
    await sched._deliver_scheduled_mail()
    after = await repos.mailbox.read_messages("dispatcher", since_id=0)
    assert len(after) == len(before)


# ---------------------------------------------------------------------------
# list_scheduled_mail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_scheduled_mail_filters(ctx: ToolContext) -> None:
    await MAILBOX_SEND.handler(
        ctx, {"to": "worker-1", "body": "a", "deliver_in": "1h", "_tool_use_id": "t1"}
    )
    await MAILBOX_SEND.handler(
        ctx, {"to": "owner", "body": "b", "deliver_in": "2h", "_tool_use_id": "t2"}
    )
    all_pending = await LIST_SCHEDULED_MAIL.handler(ctx, {})
    assert all_pending["count"] == 2
    only_owner = await LIST_SCHEDULED_MAIL.handler(ctx, {"recipient": "owner"})
    assert only_owner["count"] == 1
    assert only_owner["scheduled_mails"][0]["recipient"] == "owner"


# ---------------------------------------------------------------------------
# Crash-safety: duplicate delivery via deterministic external_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_double_tick_does_not_double_deliver_oneshot(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    """If somehow Phase -1 runs twice on the same row (mid-delivery crash
    recovery), the deterministic external_id keeps mailbox_messages
    unique. After the second pass status is completed and no duplicate
    row exists."""
    cfg = _config(tmp_path)
    await _seed(repos)
    past = datetime.now(UTC) - timedelta(minutes=5)
    sid = await repos.scheduled_mail.create(
        ScheduledMail(
            recipient="dispatcher", sender="owner", urgency="normal",
            body="once", scheduled_for=past, created_by_agent="owner",
        )
    )
    sched = _make_scheduler(repos, cfg)
    await sched._deliver_scheduled_mail()
    # Simulate replay: revert status to pending and re-run.
    await repos.scheduled_mail.conn.execute(
        "UPDATE scheduled_mail SET status='pending', last_delivery_id=NULL,"
        " occurrence_count=0 WHERE id=?", (sid,)
    )
    await repos.scheduled_mail.conn.commit()
    await sched._deliver_scheduled_mail()
    msgs = await repos.mailbox.read_messages("dispatcher", since_id=0)
    assert sum(1 for m in msgs if m.body == "once") == 1
