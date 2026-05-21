"""Smoke tests for SQLite repositories.

Goal: every method exercised in Sprint 0/1 actually works as advertised on a real
SQLite file. These tests are the safety net before we wire more on top.
"""

from __future__ import annotations

import asyncio

import pytest

from lyre.persistence.models import MailboxMessage, OutboxRow, Persona, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories


@pytest.mark.asyncio
async def test_persona_upsert_and_get(repos: SqliteRepositories) -> None:
    p = Persona(
        name="leader",
        role_description="boss",
        system_prompt="you lead",
        allowed_lyre_tools=["mailbox_send", "dispatch_task"],
    )
    await repos.personas.upsert(p)
    fetched = await repos.personas.get("leader")
    assert fetched is not None
    assert fetched.name == "leader"
    assert fetched.allowed_lyre_tools == ["mailbox_send", "dispatch_task"]
    assert fetched.status == "approved"

    p2 = Persona(
        name="leader",
        role_description="boss v2",
        system_prompt="you lead v2",
        allowed_lyre_tools=["mailbox_send"],
    )
    await repos.personas.upsert(p2)
    fetched2 = await repos.personas.get("leader")
    assert fetched2 is not None
    assert fetched2.role_description == "boss v2"


@pytest.mark.asyncio
async def test_task_lifecycle_with_lease(repos: SqliteRepositories) -> None:
    await repos.personas.upsert(
        Persona(name="worker", role_description="w", system_prompt="w")
    )

    task_id = await repos.tasks.create(
        TaskSpec(persona_name="worker", goal="g", acceptance="a")
    )
    assert task_id

    task = await repos.tasks.get(task_id)
    assert task is not None
    assert task.status == "pending"
    assert task.lease_holder is None

    # First claim succeeds; second concurrent claim fails.
    wakeup_a = "wakeup-a"
    wakeup_b = "wakeup-b"
    assert await repos.tasks.claim_lease(task_id, wakeup_a, duration_sec=60)
    assert not await repos.tasks.claim_lease(task_id, wakeup_b, duration_sec=60)

    # Owner can renew, non-owner cannot.
    assert await repos.tasks.renew_lease(task_id, wakeup_a, duration_sec=60)
    assert not await repos.tasks.renew_lease(task_id, wakeup_b, duration_sec=60)

    # Updating checkpoint as holder works; as non-holder is a no-op.
    await repos.tasks.update_checkpoint(task_id, {"phase": "edit"}, wakeup_a)
    after = await repos.tasks.get(task_id)
    assert after is not None and after.checkpoint == {"phase": "edit"}

    await repos.tasks.release_lease(task_id, wakeup_a)
    released = await repos.tasks.get(task_id)
    assert released is not None
    assert released.lease_holder is None


@pytest.mark.asyncio
async def test_task_expired_lease_is_reclaimable(
    repos: SqliteRepositories,
) -> None:
    await repos.personas.upsert(
        Persona(name="worker", role_description="w", system_prompt="w")
    )
    task_id = await repos.tasks.create(
        TaskSpec(persona_name="worker", goal="g", acceptance="a")
    )

    assert await repos.tasks.claim_lease(task_id, "wakeup-1", duration_sec=0)
    # Sleep just enough so that strftime('now') > lease_until (zero-duration leases
    # are immediately expired in SQLite's second-resolution arithmetic, but make
    # the assertion robust).
    await asyncio.sleep(1.1)

    expired = await repos.tasks.find_expired_leases(limit=5)
    assert any(t.id == task_id for t in expired)
    # New holder can claim.
    assert await repos.tasks.claim_lease(task_id, "wakeup-2", duration_sec=60)


@pytest.mark.asyncio
async def test_mailbox_insert_read_mark(repos: SqliteRepositories) -> None:
    await repos.mailbox.ensure_mailbox("owner")
    await repos.mailbox.ensure_mailbox("leader")

    msg = MailboxMessage(
        recipient="leader",
        external_id="ext-1",
        sender="owner",
        urgency="normal",
        body="hello leader",
    )
    msg_id = await repos.mailbox.insert_message(msg)
    assert msg_id > 0

    # Re-inserting same external_id is a no-op.
    dup_id = await repos.mailbox.insert_message(msg)
    assert dup_id == -1, "duplicate external_id should not produce a new row"

    msgs = await repos.mailbox.read_messages("leader")
    assert len(msgs) == 1
    assert msgs[0].body == "hello leader"

    # Per-message read state: mark + verify it's filtered from read_unread
    await repos.mailbox.mark_messages_read("leader", [msg_id])
    assert await repos.mailbox.count_unread("leader") == 0
    unread = await repos.mailbox.read_unread("leader")
    assert unread == []

    # since_id filter (system-side read_messages still works)
    msgs2 = await repos.mailbox.read_messages("leader", since_id=msg_id)
    assert msgs2 == []


@pytest.mark.asyncio
async def test_blockers_only_returned_for_urgency_blocker(
    repos: SqliteRepositories,
) -> None:
    await repos.mailbox.ensure_mailbox("worker")
    await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="worker", external_id="n", sender="o",
            urgency="normal", body="ok",
        )
    )
    await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="worker", external_id="b", sender="o",
            urgency="blocker", body="STOP",
        )
    )
    blockers = await repos.mailbox.read_blockers("worker")
    assert [m.body for m in blockers] == ["STOP"]


@pytest.mark.asyncio
async def test_outbox_enqueue_idempotent_and_dequeue(
    repos: SqliteRepositories,
) -> None:
    await repos.personas.upsert(
        Persona(name="w", role_description="w", system_prompt="w")
    )
    task_id = await repos.tasks.create(TaskSpec(persona_name="w", goal="g", acceptance="a"))
    wakeup_id = await repos.wakeups.start(task_id, "w")

    row = OutboxRow(
        task_id=task_id,
        wakeup_id=wakeup_id,
        kind="mailbox_send",
        payload={"to": "owner", "body": "hi"},
        external_id="ext-xyz",
    )
    await repos.outbox.enqueue([row])
    await repos.outbox.enqueue([row])  # idempotent

    batch = await repos.outbox.dequeue_batch(limit=10)
    assert len(batch) == 1
    assert batch[0].payload == {"to": "owner", "body": "hi"}

    await repos.outbox.mark_dispatched(batch[0].id or -1)
    batch2 = await repos.outbox.dequeue_batch(limit=10)
    assert batch2 == []


@pytest.mark.asyncio
async def test_local_hot_round_trip(repos: SqliteRepositories) -> None:
    await repos.personas.upsert(
        Persona(name="w", role_description="w", system_prompt="w")
    )
    task_id = await repos.tasks.create(TaskSpec(persona_name="w", goal="g", acceptance="a"))

    await repos.local_hot.put(task_id, "edits", {"files": ["README.md"]})
    val = await repos.local_hot.get(task_id, "edits")
    assert val == {"files": ["README.md"]}

    await repos.local_hot.clear_task(task_id)
    assert await repos.local_hot.get(task_id, "edits") is None


@pytest.mark.asyncio
async def test_wakeup_start_persists_agent_id(
    repos: SqliteRepositories,
) -> None:
    """Without this column being written, the dashboard's per-agent
    "running" detection misses two-stage agent_ids and shows them as
    queued instead of busy. The column has always existed in schema —
    the regression was that WakeupsRepo.start never set it."""
    await repos.personas.upsert(
        Persona(name="worker-maintainer", role_description="w", system_prompt="w")
    )
    await repos.agents.create(
        agent_id="worker-maintainer/refactor-auth",
        persona_name="worker-maintainer",
    )
    task_id = await repos.tasks.create(
        TaskSpec(
            persona_name="worker-maintainer",
            agent_id="worker-maintainer/refactor-auth",
            goal="g", acceptance="a",
        )
    )
    wakeup_id = await repos.wakeups.start(
        task_id, "worker-maintainer",
        agent_id="worker-maintainer/refactor-auth",
    )

    async with repos.conn.execute(
        "SELECT agent_id, persona_name FROM wakeups WHERE id = ?",
        (wakeup_id,),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["agent_id"] == "worker-maintainer/refactor-auth"
    assert row["persona_name"] == "worker-maintainer"


@pytest.mark.asyncio
async def test_wakeup_start_agent_id_optional_for_backcompat(
    repos: SqliteRepositories,
) -> None:
    """Callers that haven't been updated (most tests, bootstrap paths)
    still work — agent_id defaults to NULL and the row is created."""
    await repos.personas.upsert(
        Persona(name="w", role_description="w", system_prompt="w")
    )
    task_id = await repos.tasks.create(
        TaskSpec(persona_name="w", goal="g", acceptance="a")
    )
    wakeup_id = await repos.wakeups.start(task_id, "w")
    async with repos.conn.execute(
        "SELECT agent_id FROM wakeups WHERE id = ?", (wakeup_id,)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["agent_id"] is None


@pytest.mark.asyncio
async def test_wakeup_end_records_metering(repos: SqliteRepositories) -> None:
    await repos.personas.upsert(
        Persona(name="w", role_description="w", system_prompt="w")
    )
    task_id = await repos.tasks.create(TaskSpec(persona_name="w", goal="g", acceptance="a"))
    wakeup_id = await repos.wakeups.start(task_id, "w")
    await repos.wakeups.end(
        wakeup_id,
        end_status="completed",
        metering={
            "token_input": 100,
            "token_output": 50,
            "wall_clock_ms": 1234,
            "tool_call_count": 2,
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
        },
    )
    await repos.wakeups.set_transcript_uri(wakeup_id, "file:///x")

    # Verify by raw SQL — there's no get() method exposed.
    async with repos.conn.execute(
        "SELECT * FROM wakeups WHERE id = ?", (wakeup_id,)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["end_status"] == "completed"
    assert row["token_input"] == 100
    assert row["transcript_uri"] == "file:///x"
