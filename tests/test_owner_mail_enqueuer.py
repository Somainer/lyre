"""OwnerMailEnqueuer — owner-mail → channel_publish outbox bridge.

These exercise the catch-up sweep, fan-out across channels, dedup
via UNIQUE(kind, external_id), and thread reply-to resolution. The
broadcaster + channel are both mocked / replaced so this layer
tests in isolation.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import ClassVar

import pytest

from lyre.integrations import ChannelRegistry
from lyre.integrations.owner_mail_enqueuer import OwnerMailEnqueuer
from lyre.persistence.db import init_db
from lyre.persistence.models import MailboxMessage
from lyre.persistence.sqlite_impl import SqliteRepositories


class _FakeChannel:
    name: ClassVar[str] = "fake"

    async def run(self, stop_event) -> None:  # not exercised
        pass

    async def publish_owner_mail(self, msg, reply_to_external_id):
        return f"fake-{msg.id}"


class _OtherChannel:
    name: ClassVar[str] = "other"

    async def run(self, stop_event) -> None:
        pass

    async def publish_owner_mail(self, msg, reply_to_external_id):
        return f"other-{msg.id}"


async def _setup(tmp_path: Path) -> SqliteRepositories:
    conn = await init_db(tmp_path / "lyre.db")
    repos = SqliteRepositories(conn)
    await repos.mailbox.ensure_mailbox("owner")
    return repos


@pytest.mark.asyncio
async def test_catch_up_sweeps_unenqueued_mail(tmp_path: Path) -> None:
    """Owner-bound mail that landed before the enqueuer started
    must still get an outbox row (otherwise the channel would never
    see it). Sweep finds the mail via NOT EXISTS join."""
    repos = await _setup(tmp_path)
    try:
        m1 = await repos.mailbox.insert_message(MailboxMessage(
            recipient="owner", external_id="m1",
            sender="worker", urgency="normal", body="one",
        ))
        m2 = await repos.mailbox.insert_message(MailboxMessage(
            recipient="owner", external_id="m2",
            sender="worker", urgency="normal", body="two",
        ))
        reg = ChannelRegistry()
        reg.register(_FakeChannel())

        enq = OwnerMailEnqueuer(repos, reg)
        n = await enq.catch_up()
        assert n == 2

        # Two channel_publish rows landed; ids = mail.id.
        async with repos.conn.execute(
            "SELECT external_id FROM outbox "
            "WHERE kind='channel_publish' ORDER BY id",
        ) as cur:
            ids = [r["external_id"] for r in await cur.fetchall()]
        assert ids == [
            f"channel:fake:owner-mail:{m1}",
            f"channel:fake:owner-mail:{m2}",
        ]
    finally:
        await repos.conn.close()


@pytest.mark.asyncio
async def test_catch_up_fan_outs_across_multiple_channels(
    tmp_path: Path,
) -> None:
    """N channels × M mails = N×M outbox rows. Each channel gets
    its own copy so a slow / failing channel doesn't block another."""
    repos = await _setup(tmp_path)
    try:
        m1 = await repos.mailbox.insert_message(MailboxMessage(
            recipient="owner", external_id="m1",
            sender="x", urgency="normal", body="hi",
        ))
        reg = ChannelRegistry()
        reg.register(_FakeChannel())
        reg.register(_OtherChannel())

        n = await OwnerMailEnqueuer(repos, reg).catch_up()
        assert n == 2

        async with repos.conn.execute(
            "SELECT external_id FROM outbox "
            "WHERE kind='channel_publish' ORDER BY external_id",
        ) as cur:
            ids = sorted(r["external_id"] for r in await cur.fetchall())
        assert ids == [
            f"channel:fake:owner-mail:{m1}",
            f"channel:other:owner-mail:{m1}",
        ]
    finally:
        await repos.conn.close()


@pytest.mark.asyncio
async def test_catch_up_skips_already_enqueued(tmp_path: Path) -> None:
    """Running catch-up twice doesn't duplicate. UNIQUE(kind,
    external_id) absorbs any racing inserts, but the sweep itself
    also filters via NOT EXISTS so we don't waste round-trips."""
    repos = await _setup(tmp_path)
    try:
        await repos.mailbox.insert_message(MailboxMessage(
            recipient="owner", external_id="m1",
            sender="x", urgency="normal", body="hi",
        ))
        reg = ChannelRegistry()
        reg.register(_FakeChannel())
        enq = OwnerMailEnqueuer(repos, reg)

        n1 = await enq.catch_up()
        n2 = await enq.catch_up()
        assert n1 == 1
        assert n2 == 0  # nothing new to enqueue

        async with repos.conn.execute(
            "SELECT COUNT(*) AS n FROM outbox "
            "WHERE kind='channel_publish'",
        ) as cur:
            row = await cur.fetchone()
        assert row["n"] == 1
    finally:
        await repos.conn.close()


@pytest.mark.asyncio
async def test_catch_up_no_channels_is_noop(tmp_path: Path) -> None:
    """Empty registry → no outbox rows. The integration-disabled
    path stays free of channel side effects."""
    repos = await _setup(tmp_path)
    try:
        await repos.mailbox.insert_message(MailboxMessage(
            recipient="owner", external_id="m1",
            sender="x", urgency="normal", body="hi",
        ))
        n = await OwnerMailEnqueuer(repos, ChannelRegistry()).catch_up()
        assert n == 0
    finally:
        await repos.conn.close()


@pytest.mark.asyncio
async def test_reply_resolves_parent_channel_external_id(
    tmp_path: Path,
) -> None:
    """When a mail replies to a previous mail that already has
    metadata.channels.<name>.message_id, the new outbox row carries
    that id as `reply_to_external_id` so the channel can thread."""
    repos = await _setup(tmp_path)
    try:
        parent_id = await repos.mailbox.insert_message(MailboxMessage(
            recipient="owner", external_id="parent",
            sender="worker", urgency="normal", body="initial",
        ))
        # Simulate that the parent has already been published to Lark.
        await repos.mailbox.set_channel_external_id(
            parent_id, "fake", "lark_msg_001",
        )
        child_id = await repos.mailbox.insert_message(MailboxMessage(
            recipient="owner", external_id="child",
            sender="worker", urgency="normal", body="follow-up",
            parent_msg_id=parent_id,
        ))
        reg = ChannelRegistry()
        reg.register(_FakeChannel())

        await OwnerMailEnqueuer(repos, reg).catch_up()

        async with repos.conn.execute(
            "SELECT payload FROM outbox WHERE external_id = ?",
            (f"channel:fake:owner-mail:{child_id}",),
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        import json
        payload = json.loads(row["payload"])
        assert payload["reply_to_external_id"] == "lark_msg_001"
    finally:
        await repos.conn.close()


@pytest.mark.asyncio
async def test_steady_state_subscribes_and_enqueues_new_mail(
    tmp_path: Path,
) -> None:
    """End-to-end: enqueuer.run subscribes to a MailboxBroadcaster
    and enqueues each delivered mail. Uses the real broadcaster to
    prove the subscription contract."""
    from lyre.dashboard.sse import MailboxBroadcaster

    repos = await _setup(tmp_path)
    try:
        reg = ChannelRegistry()
        reg.register(_FakeChannel())
        broadcaster = MailboxBroadcaster(
            repos=repos, recipient="owner", poll_interval_s=0.05,
        )
        await broadcaster.prime()
        await broadcaster.start()
        enq = OwnerMailEnqueuer(repos, reg)
        runner = asyncio.create_task(enq.run(broadcaster))

        # Give the enqueuer time to finish catch-up + subscribe.
        await asyncio.sleep(0.1)
        new_id = await repos.mailbox.insert_message(MailboxMessage(
            recipient="owner", external_id="new",
            sender="worker", urgency="normal", body="fresh",
        ))
        # Wait for the broadcaster to poll + the enqueuer to enqueue.
        for _ in range(40):
            async with repos.conn.execute(
                "SELECT 1 FROM outbox WHERE external_id = ?",
                (f"channel:fake:owner-mail:{new_id}",),
            ) as cur:
                if await cur.fetchone() is not None:
                    break
            await asyncio.sleep(0.05)

        async with repos.conn.execute(
            "SELECT external_id FROM outbox "
            "WHERE kind='channel_publish'",
        ) as cur:
            rows = await cur.fetchall()
        assert any(
            r["external_id"] == f"channel:fake:owner-mail:{new_id}"
            for r in rows
        )

        enq.request_stop()
        await broadcaster.stop()
        await runner
    finally:
        await repos.conn.close()
