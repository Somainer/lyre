"""Outbox dispatcher.

Walks undispatched outbox rows and applies their side effects:
  - kind='mailbox_send'        → insert into mailbox_messages (idempotent on
                                  recipient+external_id) then mark dispatched
  - kind='tier1_notification'  → fan out as urgency=normal mailbox message to
                                  the owner (single subscriber in MVP); same
                                  idempotency guarantee
The dispatcher is the only writer of mailbox_messages outside of bootstrap.
This is the mechanism that makes "tool call → message delivery" durable across
a process kill: the outbox row sits there until dispatched.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from ..persistence.models import MailboxMessage, OutboxRow
from ..persistence.repositories import Repositories
from ..runtime.kill_switch import KillSwitch

log = structlog.get_logger()

_TIER1_SUBSCRIBERS_FALLBACK = ["owner"]


class OutboxDispatcher:
    def __init__(
        self,
        repos: Repositories,
        poll_interval_s: float = 1.0,
        batch_limit: int = 50,
        tier1_subscribers: list[str] | None = None,
        kill_switch: KillSwitch | None = None,
    ):
        self.repos = repos
        self.poll_interval_s = poll_interval_s
        self.batch_limit = batch_limit
        self.tier1_subscribers = tier1_subscribers or list(_TIER1_SUBSCRIBERS_FALLBACK)
        self.kill_switch = kill_switch or KillSwitch()
        self._stop_event = asyncio.Event()

    def request_stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        log.info(
            "outbox_dispatcher_started",
            poll_interval_s=self.poll_interval_s,
            tier1_subscribers=self.tier1_subscribers,
        )
        while not self._stop_event.is_set():
            try:
                processed = await self.tick()
                if processed == 0:
                    try:
                        await asyncio.wait_for(
                            self._stop_event.wait(), timeout=self.poll_interval_s
                        )
                    except TimeoutError:
                        pass
            except Exception as exc:  # noqa: BLE001
                log.exception("outbox_dispatcher_tick_error", error=str(exc))
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=self.poll_interval_s
                    )
                except TimeoutError:
                    pass
        log.info("outbox_dispatcher_stopped")

    async def tick(self) -> int:
        """Drain one batch. Returns number of rows successfully dispatched."""
        batch = await self.repos.outbox.dequeue_batch(limit=self.batch_limit)
        if not batch:
            return 0
        # Kill point 4 / "post_outbox_pre_dispatch": fires when there ARE
        # undispatched rows the dispatcher just saw, but BEFORE the actual
        # delivery happens. Simulates the agent finishing + outbox written +
        # dispatcher dying. The row stays in outbox; the next dispatcher run
        # picks it up and delivers it.
        self.kill_switch.check("post_outbox_pre_dispatch")
        dispatched = 0
        for row in batch:
            try:
                await self._dispatch_one(row)
                if row.id is not None:
                    await self.repos.outbox.mark_dispatched(row.id)
                dispatched += 1
            except Exception as exc:  # noqa: BLE001
                if row.id is not None:
                    await self.repos.outbox.mark_failed(row.id, repr(exc))
                log.warning(
                    "outbox_dispatch_failed",
                    row_id=row.id,
                    kind=row.kind,
                    error=str(exc),
                )
        return dispatched

    async def _dispatch_one(self, row: OutboxRow) -> None:
        if row.kind == "mailbox_send":
            await self._dispatch_mailbox_send(row)
        elif row.kind == "tier1_notification":
            await self._dispatch_tier1_notification(row)
        else:
            raise ValueError(f"Unknown outbox kind: {row.kind!r}")

    async def _dispatch_mailbox_send(self, row: OutboxRow) -> None:
        payload = row.payload
        recipient = payload.get("recipient")
        if not recipient:
            raise ValueError("mailbox_send payload missing 'recipient'")
        msg = MailboxMessage(
            recipient=recipient,
            external_id=payload.get("external_id") or row.external_id,
            sender=payload.get("sender") or "system",
            urgency=payload.get("urgency", "normal"),
            body=payload.get("body") or "",
            task_id=payload.get("task_id") or row.task_id,
            parent_msg_id=payload.get("parent_msg_id"),
            broadcast_id=payload.get("broadcast_id"),
            recipients_all=payload.get("recipients_all"),
            metadata=payload.get("metadata"),
            attachments=payload.get("attachments"),
        )
        await self.repos.mailbox.insert_message(msg)

    async def _dispatch_tier1_notification(self, row: OutboxRow) -> None:
        payload = row.payload
        kind = payload.get("kind", "unknown")
        body = _render_tier1_body(kind, payload)
        for recipient in self.tier1_subscribers:
            sub_external_id = f"{row.external_id}:{recipient}"
            await self.repos.mailbox.insert_message(
                MailboxMessage(
                    recipient=recipient,
                    external_id=sub_external_id,
                    sender=payload.get("persona") or "system",
                    urgency="normal",
                    body=body,
                    task_id=payload.get("task_id") or row.task_id,
                    metadata={
                        "tier1": True,
                        "kind": kind,
                        "details": payload.get("details"),
                    },
                )
            )


def _render_tier1_body(kind: str, payload: dict[str, Any]) -> str:
    """Plain-text summary for tier-1 notifications. Owner reads it raw."""
    details = payload.get("details") or {}
    task = payload.get("task_id") or "?"
    persona = payload.get("persona") or "?"
    head = f"[Tier 1 / {kind}] task={task} by={persona}"
    if not details:
        return head
    # Pull the most common keys to the front; fall through to repr.
    salient = []
    for key in ("url", "branch", "sha", "path", "summary"):
        if key in details:
            salient.append(f"{key}={details[key]}")
    if salient:
        return head + "\n" + "  ".join(salient)
    return head + "\n" + repr(details)
