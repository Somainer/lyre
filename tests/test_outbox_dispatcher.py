"""Tests for the outbox dispatcher loop."""

from __future__ import annotations

import asyncio
from typing import ClassVar

import pytest

from lyre.integrations import ChannelRegistry
from lyre.outbox.dispatcher import OutboxDispatcher
from lyre.persistence.models import MailboxMessage, OutboxRow, Persona, TaskSpec
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


# ---------------------------------------------------------------------------
# channel_publish: external-channel mirror of owner mail
# ---------------------------------------------------------------------------


class _RecordingChannel:
    """ExternalChannel test double — records every publish call."""

    name: ClassVar[str] = "fake"

    def __init__(self, return_id: str | None = "fake-msg-1") -> None:
        self.return_id = return_id
        self.calls: list[tuple[MailboxMessage, str | None]] = []

    async def run(self, stop_event) -> None:  # not exercised here
        pass

    async def publish_owner_mail(
        self, msg: MailboxMessage, reply_to_external_id: str | None,
    ) -> str | None:
        self.calls.append((msg, reply_to_external_id))
        return self.return_id


@pytest.mark.asyncio
async def test_channel_publish_routes_to_registered_channel(
    repos: SqliteRepositories,
) -> None:
    """A channel_publish row should look up its channel by name and
    call publish_owner_mail with the resolved mail + reply hint."""
    task_id, wakeup_id = await _seed_task_and_wakeup(repos)
    await repos.mailbox.ensure_mailbox("owner")
    msg_id = await repos.mailbox.insert_message(MailboxMessage(
        recipient="owner", external_id="m1",
        sender="worker", urgency="normal", body="hello",
    ))

    ch = _RecordingChannel(return_id="lark-msg-42")
    registry = ChannelRegistry()
    registry.register(ch)

    await repos.outbox.enqueue([OutboxRow(
        task_id=task_id, wakeup_id=wakeup_id,
        kind="channel_publish",
        payload={
            "channel": "fake",
            "msg_id": msg_id,
            "reply_to_external_id": None,
        },
        external_id=f"channel:fake:owner-mail:{msg_id}",
    )])
    disp = OutboxDispatcher(repos, channel_registry=registry)
    n = await disp.tick()
    assert n == 1
    assert len(ch.calls) == 1
    delivered_msg, reply = ch.calls[0]
    assert delivered_msg.id == msg_id
    assert reply is None


@pytest.mark.asyncio
async def test_channel_publish_persists_external_id_on_success(
    repos: SqliteRepositories,
) -> None:
    """After successful publish, msg.metadata.channels.<name>.message_id
    must hold the channel-side id so future replies can resolve
    threading without re-querying."""
    task_id, wakeup_id = await _seed_task_and_wakeup(repos)
    await repos.mailbox.ensure_mailbox("owner")
    msg_id = await repos.mailbox.insert_message(MailboxMessage(
        recipient="owner", external_id="m2",
        sender="worker", urgency="normal", body="hello",
    ))

    registry = ChannelRegistry()
    registry.register(_RecordingChannel(return_id="lark-msg-99"))

    await repos.outbox.enqueue([OutboxRow(
        task_id=task_id, wakeup_id=wakeup_id,
        kind="channel_publish",
        payload={"channel": "fake", "msg_id": msg_id},
        external_id=f"channel:fake:owner-mail:{msg_id}",
    )])
    await OutboxDispatcher(repos, channel_registry=registry).tick()

    refreshed = await repos.mailbox.get_message(msg_id)
    assert refreshed is not None
    assert refreshed.metadata is not None
    assert (
        refreshed.metadata["channels"]["fake"]["message_id"]
        == "lark-msg-99"
    )


@pytest.mark.asyncio
async def test_channel_publish_unknown_channel_marks_failed(
    repos: SqliteRepositories,
) -> None:
    """Enqueueing for a channel that isn't registered must error
    loudly — that's how operators notice they forgot to enable a
    channel after restarting."""
    task_id, wakeup_id = await _seed_task_and_wakeup(repos)
    await repos.mailbox.ensure_mailbox("owner")
    msg_id = await repos.mailbox.insert_message(MailboxMessage(
        recipient="owner", external_id="m3",
        sender="worker", urgency="normal", body="hi",
    ))
    await repos.outbox.enqueue([OutboxRow(
        task_id=task_id, wakeup_id=wakeup_id,
        kind="channel_publish",
        payload={"channel": "ghost", "msg_id": msg_id},
        external_id=f"channel:ghost:owner-mail:{msg_id}",
    )])
    # Empty registry — no channels at all.
    n = await OutboxDispatcher(repos).tick()
    assert n == 0
    async with repos.conn.execute(
        "SELECT last_error FROM outbox WHERE external_id = ?",
        (f"channel:ghost:owner-mail:{msg_id}",),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert "ghost" in (row["last_error"] or "")


@pytest.mark.asyncio
async def test_set_channel_external_id_preserves_other_metadata(
    repos: SqliteRepositories,
) -> None:
    """Writing one channel's mapping must NOT clobber existing
    metadata (other channels' sub-trees, unrelated keys). This is
    the cross-channel safety invariant."""
    await repos.mailbox.ensure_mailbox("owner")
    msg_id = await repos.mailbox.insert_message(MailboxMessage(
        recipient="owner", external_id="m-meta",
        sender="worker", urgency="normal", body="x",
        metadata={
            "unrelated_key": "stay",
            "channels": {"slack": {"message_id": "slack-1"}},
        },
    ))
    await repos.mailbox.set_channel_external_id(msg_id, "lark", "lark-1")
    refreshed = await repos.mailbox.get_message(msg_id)
    assert refreshed is not None
    meta = refreshed.metadata or {}
    # New channel mapping landed.
    assert meta["channels"]["lark"]["message_id"] == "lark-1"
    # Old channel mapping survived.
    assert meta["channels"]["slack"]["message_id"] == "slack-1"
    # Unrelated key survived.
    assert meta["unrelated_key"] == "stay"
