"""Poll-based mailbox broadcaster.

Decoupled from the persistence layer: the dashboard runs this as a background
asyncio task that polls `mailbox_messages` since a watermark and fans out new
rows to every connected SSE subscriber. Latency = poll_interval (default
500ms) — fine for owner observation. When we eventually move off SQLite, swap
this for Postgres LISTEN/NOTIFY without touching the rest.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import structlog

from ..persistence.models import MailboxMessage
from ..persistence.repositories import Repositories

log = structlog.get_logger()


@dataclass
class MailboxBroadcaster:
    repos: Repositories
    recipient: str = "owner"
    poll_interval_s: float = 0.5
    queue_max: int = 200
    _subscribers: set[asyncio.Queue] = field(default_factory=set)
    _last_seen_id: int = 0
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    _task: asyncio.Task | None = None

    async def prime(self) -> None:
        """Initialize the watermark to the current max id so we only fan out
        messages that arrive AFTER startup. Without this, every dashboard
        boot would replay the entire mailbox history to the first subscriber.
        """
        recent = await self.repos.mailbox.read_messages_paged(
            self.recipient, before_id=None, limit=1
        )
        if recent and recent[0].id is not None:
            self._last_seen_id = recent[0].id

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.queue_max)
        self._subscribers.add(q)
        log.debug(
            "broadcaster_subscribed",
            recipient=self.recipient,
            n=len(self._subscribers),
        )
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)
        log.debug(
            "broadcaster_unsubscribed",
            recipient=self.recipient,
            n=len(self._subscribers),
        )

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("MailboxBroadcaster already started")
        self._task = asyncio.create_task(
            self._loop(), name=f"mailbox_broadcaster:{self.recipient}"
        )

    async def stop(self) -> None:
        self._stop_event.set()
        # Wake every subscribed SSE handler IMMEDIATELY so they exit
        # cleanly instead of sitting in queue.get for their 2s timeout.
        # Without this, uvicorn's graceful shutdown waits for those
        # blocked handlers to drain — visible as several seconds of
        # "exit hangs" on Ctrl-C.
        self._wake_subscribers_for_shutdown()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def _wake_subscribers_for_shutdown(self) -> None:
        """Drop a None sentinel onto every subscribed queue. SSE handlers
        treat None as "broadcaster is going away; exit your drain loop."
        """
        for q in list(self._subscribers):
            try:
                q.put_nowait(None)  # type: ignore[arg-type]
            except asyncio.QueueFull:
                # Make room — full queue means a slow subscriber that's
                # about to be torn down anyway; we just need them to
                # wake at least once.
                try:
                    q.get_nowait()
                    q.put_nowait(None)  # type: ignore[arg-type]
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    async def _loop(self) -> None:
        log.info(
            "broadcaster_started",
            recipient=self.recipient,
            poll_interval_s=self.poll_interval_s,
            baseline=self._last_seen_id,
        )
        while not self._stop_event.is_set():
            try:
                msgs = await self.repos.mailbox.read_messages(
                    self.recipient, since_id=self._last_seen_id, limit=100
                )
                if msgs:
                    for m in msgs:
                        if m.id is not None and m.id > self._last_seen_id:
                            self._last_seen_id = m.id
                        self._publish(m)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "broadcaster_poll_error",
                    recipient=self.recipient,
                    error=str(exc),
                )
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.poll_interval_s
                )
            except TimeoutError:
                pass
        log.info("broadcaster_stopped", recipient=self.recipient)

    def _publish(self, msg: MailboxMessage) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                # Drop oldest to keep up.
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(msg)
                except asyncio.QueueFull:
                    pass
