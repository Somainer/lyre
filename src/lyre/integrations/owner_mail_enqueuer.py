"""Owner-mail → channel_publish outbox enqueuer.

Channel-neutral bridge between the owner's mailbox and the
``channel_publish`` outbox kind. One enqueuer instance per process,
subscribes to MailboxBroadcaster for ``recipient="owner"`` and
writes one outbox row per (channel, msg) pair so the dispatcher can
fan out independently to every registered channel.

Kill-safety:

  * **Steady state** — every new owner mail emitted by the
    broadcaster produces N outbox rows (N = enabled channels).
    Idempotency comes from the outbox's
    ``UNIQUE(kind, external_id)`` constraint with
    ``external_id = "channel:<name>:owner-mail:<msg_id>"``;
    re-emitting the same mail is a no-op.

  * **Cold start** — the broadcaster only emits mail with
    ``id > _last_seen_id`` after ``prime()``. Mail that landed
    during downtime would otherwise be invisible. ``catch_up()``
    on startup sweeps every owner mail not yet enqueued for each
    channel (left-anti-join against the outbox), enqueues them,
    then the steady-state subscriber takes over.

  * **New channel added later** — same catch_up sweep picks up
    every existing owner mail for the freshly-registered channel
    on the next process restart. (Hot-add isn't supported; runtime
    composition of the registry is restart-only.)

Each spawned outbox row carries the source mail's ``task_id``
(NULL after migration 0004 when the mail had no task attribution).
This keeps trace-by-task working for agent-originated mail without
requiring a synthetic task for owner-side or system mail.
"""

from __future__ import annotations

import asyncio
import json as _json
from dataclasses import dataclass, field

import structlog

from ..dashboard.sse import MailboxBroadcaster
from ..persistence.models import MailboxMessage, OutboxRow
from ..persistence.repositories import Repositories
from . import ChannelRegistry

log = structlog.get_logger()


@dataclass
class OwnerMailEnqueuer:
    repos: Repositories
    channels: ChannelRegistry
    broadcaster_recipient: str = "owner"
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    _task: asyncio.Task[None] | None = None

    def request_stop(self) -> None:
        self._stop_event.set()

    async def run(self, broadcaster: MailboxBroadcaster) -> None:
        """Run the catch-up sweep then drain the broadcaster forever.

        ``broadcaster`` is a primed-and-started
        :class:`MailboxBroadcaster` for the owner mailbox; we
        subscribe to its queue and translate every yielded mail
        into one outbox row per registered channel.
        """
        if not self.channels:
            log.info("owner_mail_enqueuer_skipped_no_channels")
            return

        log.info(
            "owner_mail_enqueuer_started",
            channels=self.channels.names(),
        )

        await self.catch_up()

        q = broadcaster.subscribe()
        try:
            while not self._stop_event.is_set():
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=1.0)
                except TimeoutError:
                    continue
                # None sentinel = broadcaster shutting down.
                if msg is None:
                    break
                await self._enqueue_for_channels(msg)
        finally:
            broadcaster.unsubscribe(q)
            log.info("owner_mail_enqueuer_stopped")

    async def catch_up(self) -> int:
        """Sweep owner mail that hasn't been enqueued for every
        registered channel yet. Returns number of rows enqueued
        (counted across channels — N mails × M channels = N×M)."""
        n = 0
        for ch_name in self.channels.names():
            mails = await self._owner_mails_not_yet_enqueued(ch_name)
            for mail in mails:
                await self._enqueue_one(ch_name, mail)
                n += 1
        if n:
            log.info("owner_mail_enqueuer_caught_up", rows=n)
        return n

    async def _owner_mails_not_yet_enqueued(
        self, channel_name: str,
    ) -> list[MailboxMessage]:
        """Left-anti-join: owner-bound mails for which no
        channel_publish outbox row exists yet (for this channel).
        Cap at 500 to avoid pathological replays after long
        downtime — a follow-up sweep on the next tick picks up the
        rest."""
        return await self.repos.mailbox.list_pending_channel_publish(
            recipient=self.broadcaster_recipient,
            channel_name=channel_name,
            limit=500,
        )

    async def _enqueue_for_channels(self, msg: MailboxMessage) -> None:
        """Fan-out: one outbox row per registered channel."""
        for ch_name in self.channels.names():
            await self._enqueue_one(ch_name, msg)

    async def _enqueue_one(
        self, channel_name: str, msg: MailboxMessage,
    ) -> None:
        """Resolve reply-to (if any) and enqueue. ``ON CONFLICT
        DO NOTHING`` on the outbox's UNIQUE(kind, external_id)
        absorbs duplicate sweeps."""
        if msg.id is None:
            return  # nothing to thread / dedup against
        reply_to_external_id = await self._resolve_reply_to(
            channel_name, msg,
        )
        external_id = f"channel:{channel_name}:owner-mail:{msg.id}"
        payload = {
            "channel": channel_name,
            "msg_id": msg.id,
            "reply_to_external_id": reply_to_external_id,
        }
        await self.repos.outbox.enqueue([
            OutboxRow(
                # task_id / wakeup_id inherited from the source mail
                # when present (NULL otherwise, allowed by 0004).
                task_id=msg.task_id,
                wakeup_id=None,
                kind="channel_publish",
                payload=payload,
                external_id=external_id,
            ),
        ])

    async def _resolve_reply_to(
        self, channel_name: str, msg: MailboxMessage,
    ) -> str | None:
        """If ``msg.parent_msg_id`` is set and the parent has a
        recorded ``metadata.channels.<channel>.message_id``, return
        that — the channel's outbound handler will post the new
        message as a thread reply. Returns None when there's no
        parent or no recorded mapping (channels handle that as
        "post root-level")."""
        if not msg.parent_msg_id:
            return None
        parent = await self.repos.mailbox.get_message(msg.parent_msg_id)
        if parent is None or not parent.metadata:
            return None
        channels_meta = parent.metadata.get("channels")
        # When metadata came from a JSON-encoded TOML or older row
        # it may still be a string. Stay defensive — JSON-decode if
        # needed.
        if isinstance(channels_meta, str):
            try:
                channels_meta = _json.loads(channels_meta)
            except (ValueError, TypeError):
                return None
        if not isinstance(channels_meta, dict):
            return None
        ch_entry = channels_meta.get(channel_name)
        if not isinstance(ch_entry, dict):
            return None
        ext = ch_entry.get("message_id")
        return ext if isinstance(ext, str) else None
