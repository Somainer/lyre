"""Scheduler Phase 0 — auto-wake on unread `urgency >= high` mail.

When you `lyre send dispatcher "..."` (or any agent sends to another agent
with urgency high/blocker), the scheduler must convert that into a real
task so the recipient actually wakes up. Without this, mail rots in the
DB forever unless something else triggers the agent.

Matches the Inbox UI tier (urgency >= high). normal/low remain passive.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lyre.config import Config
from lyre.persistence.models import MailboxMessage, Persona, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.memory import ensure_skeleton
from lyre.scheduler.scheduler import Scheduler

from .fake_adapter import FakeAdapter
from .helpers import fake_entry, fake_registry


def _config(tmp_path: Path) -> Config:
    mem = tmp_path / "memory"
    ensure_skeleton(mem)
    return Config(
        db_path=tmp_path / "x.db",
        object_store_path=tmp_path / "objstore",
        memory_path=mem,
        anthropic_api_key="fake",
        anthropic_base_url=None,
        default_model="m",
        model_override=None,
    )


async def _seed_dispatcher(repos: SqliteRepositories) -> None:
    await repos.personas.upsert(
        Persona(
            name="dispatcher", role_description="dispatcher",
            system_prompt="l", allowed_lyre_tools=["mailbox_send"],
            model_preference={
                "tier": "flagship", "requires": ["tool_use"], "prefer": [],
            },
        )
    )
    # Post-A3: Phase 0 iterates agents, not personas.
    await repos.personas.upsert(
        Persona(name="owner", role_description="o", system_prompt="o")
    )
    await repos.agents.create(agent_id="owner", persona_name="owner")
    await repos.agents.create(agent_id="dispatcher", persona_name="dispatcher")
    await repos.mailbox.ensure_mailbox("owner")
    await repos.mailbox.ensure_mailbox("dispatcher")


def _make_scheduler(
    repos: SqliteRepositories, cfg: Config, *,
    auto_wake_on_mail: bool = True,
) -> Scheduler:
    return Scheduler(
        repos, cfg,
        poll_interval_s=0.05,
        registry=fake_registry(
            fake_entry(id="m-flagship", tier="flagship"),
            fake_entry(id="m-workhorse", tier="workhorse"),
        ),
        adapter_for_test=lambda e: FakeAdapter(),
        auto_wake_on_mail=auto_wake_on_mail,
    )


# ---------------------------------------------------------------------------
# Core behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blocker_mail_auto_dispatches_check_inbox_task(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    cfg.object_store_path.mkdir(parents=True, exist_ok=True)
    await _seed_dispatcher(repos)

    # owner sends a blocker to dispatcher
    mail_id = await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="dispatcher", external_id="m1", sender="owner",
            urgency="blocker", body="STOP, need decision",
        )
    )

    scheduler = _make_scheduler(repos, cfg)
    await scheduler._auto_dispatch_for_unread_mail()

    # Should have created exactly one task for dispatcher
    actives = await repos.tasks.find_active_for_persona("dispatcher")
    assert len(actives) == 1
    t = actives[0]
    assert t.persona_name == "dispatcher"
    assert t.metadata is not None
    assert t.metadata["auto_dispatched"] is True
    assert t.metadata["triggered_by_mail_id"] == mail_id
    assert t.metadata["triggered_by_urgency"] == "blocker"
    # Goal mentions the urgency of the triggering mail so the agent can
    # prioritise on wake.
    assert "blocker" in t.goal


@pytest.mark.asyncio
async def test_high_urgency_also_triggers(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    cfg.object_store_path.mkdir(parents=True, exist_ok=True)
    await _seed_dispatcher(repos)

    await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="dispatcher", external_id="m1", sender="owner",
            urgency="high", body="please reply soon",
        )
    )
    scheduler = _make_scheduler(repos, cfg)
    await scheduler._auto_dispatch_for_unread_mail()

    actives = await repos.tasks.find_active_for_persona("dispatcher")
    assert len(actives) == 1


@pytest.mark.asyncio
async def test_normal_urgency_triggers_idle_wake(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    """FYI mail (urgency=normal) should ALSO auto-dispatch when the agent
    is idle — every message gets read, urgency just controls how
    aggressively it surfaces."""
    cfg = _config(tmp_path)
    cfg.object_store_path.mkdir(parents=True, exist_ok=True)
    await _seed_dispatcher(repos)

    await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="dispatcher", external_id="m1", sender="owner",
            urgency="normal", body="fyi",
        )
    )
    scheduler = _make_scheduler(repos, cfg)
    await scheduler._auto_dispatch_for_unread_mail()

    actives = await repos.tasks.find_active_for_persona("dispatcher")
    assert len(actives) == 1
    assert actives[0].metadata is not None
    assert actives[0].metadata["triggered_by_urgency"] == "normal"


@pytest.mark.asyncio
async def test_low_urgency_does_not_trigger(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    """urgency=low is pure archive — never auto-wakes. Agent reads it only
    if explicitly chooses to."""
    cfg = _config(tmp_path)
    cfg.object_store_path.mkdir(parents=True, exist_ok=True)
    await _seed_dispatcher(repos)
    await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="dispatcher", external_id="m1", sender="owner",
            urgency="low", body="archive note",
        )
    )
    scheduler = _make_scheduler(repos, cfg)
    await scheduler._auto_dispatch_for_unread_mail()
    assert await repos.tasks.find_active_for_persona("dispatcher") == []


@pytest.mark.asyncio
async def test_low_does_not_trigger_even_alongside_normal(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    """Sanity: when only `low` mail exists (no higher urgency), Phase 0
    never fires."""
    cfg = _config(tmp_path)
    cfg.object_store_path.mkdir(parents=True, exist_ok=True)
    await _seed_dispatcher(repos)
    for i in range(3):
        await repos.mailbox.insert_message(
            MailboxMessage(
                recipient="dispatcher", external_id=f"l{i}", sender="owner",
                urgency="low", body=f"archive {i}",
            )
        )
    scheduler = _make_scheduler(repos, cfg)
    await scheduler._auto_dispatch_for_unread_mail()
    assert await repos.tasks.find_active_for_persona("dispatcher") == []


# ---------------------------------------------------------------------------
# Idempotency / don't-thrash invariants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_double_dispatch_when_task_already_active(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    """If dispatcher already has an in-flight or pending task, the auto-wake
    must NOT pile on a second task — that would race the agent."""
    cfg = _config(tmp_path)
    cfg.object_store_path.mkdir(parents=True, exist_ok=True)
    await _seed_dispatcher(repos)

    # Pre-existing pending task (tied to the dispatcher AGENT, not just persona)
    pre_existing = await repos.tasks.create(
        TaskSpec(agent_id="dispatcher", goal="prior work", acceptance="a")
    )
    await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="dispatcher", external_id="m1", sender="owner",
            urgency="blocker", body="STOP",
        )
    )
    scheduler = _make_scheduler(repos, cfg)
    await scheduler._auto_dispatch_for_unread_mail()

    actives = await repos.tasks.find_active_for_persona("dispatcher")
    assert len(actives) == 1
    assert actives[0].id == pre_existing
    # No auto-dispatched task was created
    assert actives[0].metadata is None or not actives[0].metadata.get(
        "auto_dispatched"
    )


@pytest.mark.asyncio
async def test_repeat_tick_does_not_create_duplicate_auto_tasks(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    """First tick auto-dispatches. Second tick (still unread, still pending
    auto-task) must not create another."""
    cfg = _config(tmp_path)
    cfg.object_store_path.mkdir(parents=True, exist_ok=True)
    await _seed_dispatcher(repos)
    await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="dispatcher", external_id="m1", sender="owner",
            urgency="blocker", body="STOP",
        )
    )

    scheduler = _make_scheduler(repos, cfg)
    await scheduler._auto_dispatch_for_unread_mail()
    await scheduler._auto_dispatch_for_unread_mail()
    await scheduler._auto_dispatch_for_unread_mail()

    actives = await repos.tasks.find_active_for_persona("dispatcher")
    assert len(actives) == 1


@pytest.mark.asyncio
async def test_same_mail_never_triggers_twice_even_if_agent_never_reads(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    """Regression for the production loop bug: if an agent completes its
    auto-task WITHOUT calling mailbox_read (e.g. silent turn / LLM
    confused), the same mail must NOT spawn a fresh auto-task on the
    next tick. Guarded by the persistent last_auto_triggered_msg_id
    cursor.
    """
    cfg = _config(tmp_path)
    cfg.object_store_path.mkdir(parents=True, exist_ok=True)
    await _seed_dispatcher(repos)
    mail_id = await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="dispatcher", external_id="m1", sender="owner",
            urgency="normal", body="fyi",
        )
    )
    scheduler = _make_scheduler(repos, cfg)

    # Tick 1: dispatch
    await scheduler._auto_dispatch_for_unread_mail()
    first_actives = await repos.tasks.find_active_for_persona("dispatcher")
    assert len(first_actives) == 1

    # Simulate agent completing the task BUT NEVER CALLING mailbox_read
    # (the symptom that drove the original loop bug).
    await repos.tasks.update_status(first_actives[0].id, "completed")
    # Mail is still unread — agent didn't touch it
    assert await repos.mailbox.count_unread("dispatcher") == 1

    # Tick 2: must NOT create another task because the scheduler-side
    # cursor (last_auto_triggered_msg_id) was advanced when we dispatched.
    await scheduler._auto_dispatch_for_unread_mail()
    await scheduler._auto_dispatch_for_unread_mail()
    await scheduler._auto_dispatch_for_unread_mail()
    assert await repos.tasks.find_active_for_persona("dispatcher") == []

    # Cursor was actually persisted
    assert (
        await repos.mailbox.get_last_auto_triggered_id("dispatcher") == mail_id
    )


@pytest.mark.asyncio
async def test_cursor_persists_across_scheduler_instances(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    """The auto-trigger cursor lives in the DB, not in-memory — so
    restarting the scheduler (or running it as a subprocess) doesn't
    cause a duplicate dispatch for already-poked mail."""
    cfg = _config(tmp_path)
    cfg.object_store_path.mkdir(parents=True, exist_ok=True)
    await _seed_dispatcher(repos)
    await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="dispatcher", external_id="m1", sender="owner",
            urgency="normal", body="fyi",
        )
    )

    s1 = _make_scheduler(repos, cfg)
    await s1._auto_dispatch_for_unread_mail()
    actives = await repos.tasks.find_active_for_persona("dispatcher")
    assert len(actives) == 1
    await repos.tasks.update_status(actives[0].id, "completed")

    # Fresh scheduler instance — must see the persisted cursor and skip
    s2 = _make_scheduler(repos, cfg)
    await s2._auto_dispatch_for_unread_mail()
    assert await repos.tasks.find_active_for_persona("dispatcher") == []


@pytest.mark.asyncio
async def test_higher_id_mail_still_triggers_after_cursor_advances(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    """Cursor must NOT block fresh mail with id > cursor. It only blocks
    the specific mail that already caused a dispatch."""
    cfg = _config(tmp_path)
    cfg.object_store_path.mkdir(parents=True, exist_ok=True)
    await _seed_dispatcher(repos)

    m1 = await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="dispatcher", external_id="m1", sender="owner",
            urgency="normal", body="first",
        )
    )
    scheduler = _make_scheduler(repos, cfg)
    await scheduler._auto_dispatch_for_unread_mail()
    first = (await repos.tasks.find_active_for_persona("dispatcher"))[0]
    await repos.tasks.update_status(first.id, "completed")

    # New mail with HIGHER id arrives
    m2 = await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="dispatcher", external_id="m2", sender="owner",
            urgency="normal", body="second",
        )
    )
    await scheduler._auto_dispatch_for_unread_mail()
    actives = await repos.tasks.find_active_for_persona("dispatcher")
    assert len(actives) == 1
    assert actives[0].metadata is not None
    assert actives[0].metadata["triggered_by_mail_id"] == m2
    assert m2 > m1


@pytest.mark.asyncio
async def test_mark_read_prevents_re_trigger(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    """After agent reads the mail (read_at set), a subsequent tick
    MUST NOT create a fresh auto-task — read_unread query filters it."""
    cfg = _config(tmp_path)
    cfg.object_store_path.mkdir(parents=True, exist_ok=True)
    await _seed_dispatcher(repos)
    mail_id = await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="dispatcher", external_id="m1", sender="owner",
            urgency="blocker", body="STOP",
        )
    )

    scheduler = _make_scheduler(repos, cfg)
    await scheduler._auto_dispatch_for_unread_mail()
    auto_task = (await repos.tasks.find_active_for_persona("dispatcher"))[0]
    await repos.tasks.update_status(auto_task.id, "completed")
    await repos.mailbox.mark_messages_read("dispatcher", [mail_id])

    # Next tick: no unread → no new task
    await scheduler._auto_dispatch_for_unread_mail()
    assert await repos.tasks.find_active_for_persona("dispatcher") == []


@pytest.mark.asyncio
async def test_new_mail_after_read_triggers_again(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    """Fresh urgency≥normal mail after old mail was read must re-trigger."""
    cfg = _config(tmp_path)
    cfg.object_store_path.mkdir(parents=True, exist_ok=True)
    await _seed_dispatcher(repos)

    m1 = await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="dispatcher", external_id="m1", sender="owner",
            urgency="blocker", body="first",
        )
    )
    scheduler = _make_scheduler(repos, cfg)
    await scheduler._auto_dispatch_for_unread_mail()
    auto_task = (await repos.tasks.find_active_for_persona("dispatcher"))[0]
    await repos.tasks.update_status(auto_task.id, "completed")
    await repos.mailbox.mark_messages_read("dispatcher", [m1])

    # Second mail arrives
    m2 = await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="dispatcher", external_id="m2", sender="owner",
            urgency="high", body="follow up",
        )
    )
    await scheduler._auto_dispatch_for_unread_mail()
    actives = await repos.tasks.find_active_for_persona("dispatcher")
    assert len(actives) == 1
    assert actives[0].metadata is not None
    assert actives[0].metadata["triggered_by_mail_id"] == m2


# ---------------------------------------------------------------------------
# Per-persona scoping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unread_mail_to_dispatcher_does_not_auto_wake_worker(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    """Mail addressed to dispatcher must not auto-dispatch a worker task."""
    cfg = _config(tmp_path)
    cfg.object_store_path.mkdir(parents=True, exist_ok=True)
    await _seed_dispatcher(repos)
    await repos.personas.upsert(
        Persona(
            name="worker-maintainer", role_description="w",
            system_prompt="w",
            model_preference={
                "tier": "workhorse", "requires": ["tool_use"], "prefer": [],
            },
        )
    )
    await repos.agents.create(
        agent_id="worker-maintainer-1", persona_name="worker-maintainer"
    )
    await repos.mailbox.ensure_mailbox("worker-maintainer-1")

    await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="dispatcher", external_id="m1", sender="owner",
            urgency="blocker", body="STOP",
        )
    )
    scheduler = _make_scheduler(repos, cfg)
    await scheduler._auto_dispatch_for_unread_mail()

    assert len(await repos.tasks.find_active_for_persona("dispatcher")) == 1
    assert (
        await repos.tasks.find_active_for_persona("worker-maintainer") == []
    )


@pytest.mark.asyncio
async def test_owner_persona_never_auto_woken(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    """The owner persona exists as an FK target only; it's not an LLM
    agent and must never be auto-dispatched."""
    cfg = _config(tmp_path)
    cfg.object_store_path.mkdir(parents=True, exist_ok=True)
    await _seed_dispatcher(repos)
    # Seed an owner persona
    await repos.personas.upsert(
        Persona(name="owner", role_description="o", system_prompt="o",
                model_preference=None)
    )
    await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="owner", external_id="m1", sender="dispatcher",
            urgency="blocker", body="STOP",
        )
    )
    scheduler = _make_scheduler(repos, cfg)
    await scheduler._auto_dispatch_for_unread_mail()
    assert await repos.tasks.find_active_for_persona("owner") == []


# ---------------------------------------------------------------------------
# Opt-out
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_wake_off_means_no_phase_0_dispatch(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    cfg.object_store_path.mkdir(parents=True, exist_ok=True)
    await _seed_dispatcher(repos)
    await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="dispatcher", external_id="m1", sender="owner",
            urgency="blocker", body="STOP",
        )
    )
    scheduler = _make_scheduler(repos, cfg, auto_wake_on_mail=False)
    await scheduler._tick()  # full tick — should not auto-dispatch
    assert await repos.tasks.find_active_for_persona("dispatcher") == []


# ---------------------------------------------------------------------------
# End-to-end through full _tick()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_tick_picks_up_auto_dispatched_task(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    """Phase 0 creates the task; same tick may also run it via Phase 3.
    Verify the chain: send blocker → tick → dispatcher task exists +
    eventually completed."""
    cfg = _config(tmp_path)
    cfg.object_store_path.mkdir(parents=True, exist_ok=True)
    await _seed_dispatcher(repos)
    await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="dispatcher", external_id="m1", sender="owner",
            urgency="blocker", body="STOP",
        )
    )

    from lyre.adapter.llm_adapter import ContentDelta, TurnComplete
    fake = FakeAdapter()
    fake.push_turn([
        ContentDelta(text="ack"),
        TurnComplete(stop_reason="end_turn"),
    ])
    scheduler = Scheduler(
        repos, cfg, poll_interval_s=0.05,
        registry=fake_registry(fake_entry(id="m-flagship", tier="flagship")),
        adapter_for_test=lambda e: fake,
    )
    # Tick 1: Phase 0 creates auto-task, Phase 3 picks it up + runs it
    await scheduler._tick()
    actives = await repos.tasks.find_active_for_persona("dispatcher")
    # Either the task is now completed (so no longer active), or it's still
    # running. The auto-task was definitely created during this tick.
    if actives:
        # Still in flight
        assert any(
            t.metadata and t.metadata.get("auto_dispatched") for t in actives
        )
    else:
        # Completed within the same tick
        all_tasks = await repos.tasks.find_recent(limit=5)
        assert any(
            t.metadata and t.metadata.get("auto_dispatched")
            and t.status == "completed"
            for t in all_tasks
        )
