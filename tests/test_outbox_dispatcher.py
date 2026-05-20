"""Tests for the outbox dispatcher loop."""

from __future__ import annotations

import asyncio

import pytest

from lyre.outbox.dispatcher import OutboxDispatcher
from lyre.persistence.models import OutboxRow, Persona, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories


async def _seed_task_and_wakeup(repos: SqliteRepositories) -> tuple[str, str]:
    await repos.personas.upsert(
        Persona(name="worker", role_description="w", system_prompt="w")
    )
    task_id = await repos.tasks.create(
        TaskSpec(persona_name="worker", goal="g", acceptance="a")
    )
    wakeup_id = await repos.wakeups.start(task_id, "worker")
    return task_id, wakeup_id


@pytest.mark.asyncio
async def test_dispatch_mailbox_send_delivers_message(
    repos: SqliteRepositories,
) -> None:
    task_id, wakeup_id = await _seed_task_and_wakeup(repos)
    await repos.mailbox.ensure_mailbox("owner")
    await repos.outbox.enqueue(
        [
            OutboxRow(
                task_id=task_id,
                wakeup_id=wakeup_id,
                kind="mailbox_send",
                payload={
                    "recipient": "owner",
                    "sender": "worker",
                    "urgency": "normal",
                    "body": "hi owner",
                    "task_id": task_id,
                    "external_id": f"{wakeup_id}:tu_1",
                },
                external_id=f"{wakeup_id}:tu_1",
            )
        ]
    )
    disp = OutboxDispatcher(repos, poll_interval_s=0.01)
    n = await disp.tick()
    assert n == 1

    msgs = await repos.mailbox.read_messages("owner")
    assert len(msgs) == 1
    assert msgs[0].body == "hi owner"
    assert msgs[0].sender == "worker"
    assert msgs[0].urgency == "normal"

    # Outbox row is marked dispatched (no longer dequeued).
    assert await repos.outbox.dequeue_batch(limit=10) == []


@pytest.mark.asyncio
async def test_dispatch_idempotent_on_retry(repos: SqliteRepositories) -> None:
    task_id, wakeup_id = await _seed_task_and_wakeup(repos)
    await repos.mailbox.ensure_mailbox("owner")
    row = OutboxRow(
        task_id=task_id,
        wakeup_id=wakeup_id,
        kind="mailbox_send",
        payload={
            "recipient": "owner",
            "sender": "worker",
            "urgency": "normal",
            "body": "x",
            "external_id": "ext-1",
        },
        external_id="ext-1",
    )
    await repos.outbox.enqueue([row])
    disp = OutboxDispatcher(repos)
    await disp.tick()
    # Re-enqueue the same external_id → outbox dedupes; even if we somehow
    # smuggled a second row through, mailbox dedupes by (recipient, external_id).
    await repos.outbox.enqueue([row])
    await disp.tick()
    msgs = await repos.mailbox.read_messages("owner")
    assert len(msgs) == 1


@pytest.mark.asyncio
async def test_tier1_notification_fans_out_to_subscribers(
    repos: SqliteRepositories,
) -> None:
    task_id, wakeup_id = await _seed_task_and_wakeup(repos)
    await repos.mailbox.ensure_mailbox("owner")
    await repos.outbox.enqueue(
        [
            OutboxRow(
                task_id=task_id,
                wakeup_id=wakeup_id,
                kind="tier1_notification",
                payload={
                    "kind": "opened_pr",
                    "task_id": task_id,
                    "persona": "worker",
                    "details": {"url": "https://github.com/x/y/pull/1"},
                },
                external_id="ext-tier1",
            )
        ]
    )
    disp = OutboxDispatcher(repos, tier1_subscribers=["owner"])
    n = await disp.tick()
    assert n == 1

    msgs = await repos.mailbox.read_messages("owner")
    assert len(msgs) == 1
    assert "opened_pr" in msgs[0].body
    assert "github.com/x/y/pull/1" in msgs[0].body
    assert msgs[0].metadata is not None
    assert msgs[0].metadata.get("tier1") is True


@pytest.mark.asyncio
async def test_dispatcher_run_loop_stops_on_request(
    repos: SqliteRepositories,
) -> None:
    disp = OutboxDispatcher(repos, poll_interval_s=0.05)
    task = asyncio.create_task(disp.run())
    await asyncio.sleep(0.15)
    disp.request_stop()
    await asyncio.wait_for(task, timeout=2.0)
    # No exceptions = pass.


@pytest.mark.asyncio
async def test_unknown_kind_marks_failed(repos: SqliteRepositories) -> None:
    task_id, wakeup_id = await _seed_task_and_wakeup(repos)
    # Bypass the check constraint by directly inserting via SQL with an allowed
    # kind, then rewrite — actually CHECK enforces valid kinds, so this test
    # exercises mark_failed via a different path. We'll force an error by
    # giving mailbox_send a missing recipient.
    await repos.outbox.enqueue(
        [
            OutboxRow(
                task_id=task_id,
                wakeup_id=wakeup_id,
                kind="mailbox_send",
                payload={"sender": "x", "body": "y"},
                external_id="ext-fail",
            )
        ]
    )
    disp = OutboxDispatcher(repos)
    n = await disp.tick()
    assert n == 0

    # Row is still undispatched but marked failed once.
    async with repos.conn.execute(
        "SELECT dispatch_attempts, last_error FROM outbox WHERE external_id = ?",
        ("ext-fail",),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["dispatch_attempts"] == 1
    assert "recipient" in (row["last_error"] or "")
