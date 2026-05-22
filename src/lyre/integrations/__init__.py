"""External channel integrations — owner-facing IM/chat surfaces.

An *external channel* is a bidirectional bridge between the owner's
``mailbox_messages.recipient = "owner"`` flow and a third-party
messaging system (Lark/Feishu today, Slack/Discord/Telegram tomorrow).

Two responsibilities per channel:

  1. **Inbound**: events from the external system that the
     ``authorized_user_id`` produced become mail rows with
     ``sender="owner"`` and a recipient chosen by addressing rules
     (default → dispatcher persona's seeded agent id, i.e. its
     ``display_name`` from identity.md; ``@<agent_id>`` prefix
     override; thread-reply inherits its parent's recipient). Goes
     through ``repos.mailbox.insert_message`` directly — no outbox,
     since the owner sits at the system edge.

  2. **Outbound**: mail addressed ``recipient="owner"`` is mirrored
     to every enabled channel via the ``channel_publish`` outbox
     kind. The dispatcher routes through ``ChannelRegistry.get(name)``
     and calls :meth:`ExternalChannel.publish_owner_mail` — the
     returned external message id is persisted on
     ``msg.metadata.channels.<name>.message_id`` so future threading
     can resolve replies.

This module defines the Protocol + registry; the individual channel
implementations live in subpackages (``lyre.integrations.lark``,
future ``lyre.integrations.slack``, …). Downstream code (outbox
dispatcher, owner-mail enqueuer, ``lyre serve`` wiring) is
channel-agnostic — it sees the registry, not specific channel types.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, ClassVar, Protocol

if TYPE_CHECKING:
    from ..persistence.models import MailboxMessage


class ExternalChannel(Protocol):
    """The contract every owner-mailbox surface (Lark / Slack / …)
    must satisfy. Kept tight so adding a new channel is "drop in a
    subpackage that exports a class with these four members" and
    nothing else."""

    # Stable identifier used as the channel namespace in
    # ``metadata.channels.<name>.*`` and as the routing key in
    # ``OutboxRow.payload["channel"]``. Convention: lowercase ASCII,
    # no dashes; matches the config sub-section
    # (``[integrations.<name>]``) too.
    name: ClassVar[str]

    async def run(self, stop_event: asyncio.Event) -> None:
        """Run the channel's inbound long-lived loop (WebSocket /
        Socket Mode / Gateway / long-poll) until ``stop_event`` is
        set. Implementations are responsible for reconnect / backoff
        on transient failures — let the loop exit only when
        ``stop_event`` flips or a non-recoverable error fires."""
        ...

    async def publish_owner_mail(
        self,
        msg: MailboxMessage,
        reply_to_external_id: str | None,
    ) -> str | None:
        """Post ``msg`` to the channel as a message to the
        authorized user.

        ``reply_to_external_id`` carries the channel's own message
        id of the mail this is replying to (looked up via
        ``parent.metadata.channels.<name>.message_id`` upstream). If
        the channel supports threading, the post should land in the
        same thread; otherwise it's a hint and can be ignored.

        Returns the channel-side message id (string) so the outbox
        dispatcher can persist it on the mail's
        ``metadata.channels.<name>.message_id``. Return ``None`` if
        the channel doesn't expose a stable id; threading from that
        message just won't work, but delivery is still successful.

        Implementations should be idempotent if practical — the
        outbox already dedupes via ``external_id``, but a channel
        whose post API has its own client-side dedup token is even
        safer.
        """
        ...


class ChannelRegistry:
    """Small dict-like holder mapping ``name`` → channel instance.

    Single source of truth shared by the outbox dispatcher (looks
    up channel by name when handling a ``channel_publish`` row) and
    the owner-mail enqueuer (iterates enabled channels to enqueue
    one outbox row per channel per mail).

    Built once in ``lyre serve`` from ``cfg.integrations``; not
    mutated after startup — channels can't be hot-added without
    restarting the daemon.
    """

    def __init__(self) -> None:
        self._channels: dict[str, ExternalChannel] = {}

    def register(self, channel: ExternalChannel) -> None:
        if channel.name in self._channels:
            raise ValueError(
                f"channel {channel.name!r} already registered"
            )
        self._channels[channel.name] = channel

    def get(self, name: str) -> ExternalChannel | None:
        return self._channels.get(name)

    def names(self) -> list[str]:
        return list(self._channels.keys())

    def values(self) -> list[ExternalChannel]:
        return list(self._channels.values())

    def __len__(self) -> int:
        return len(self._channels)

    def __bool__(self) -> bool:
        return bool(self._channels)
