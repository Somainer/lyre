"""In-flight mail watcher (formerly BlockerWatcher).

While an agent's wakeup is in flight, this background asyncio task polls the
persona's mailbox for new messages at or above a configured urgency floor.
When new mail shows up:

  1. The watcher sets an `asyncio.Event` (`signal`) and caches the message list
     in `pending` so the agent loop can attribute "what triggered the
     interrupt / reminder" without an extra DB roundtrip.
  2. The agent loop observes the signal at two checkpoints:
       a. *Between LLM stream events* — break out of the current stream
          gracefully. This is reserved for `urgency=blocker` ("system is
          waiting") so high/normal don't yank the agent off mid-thought.
       b. *At the turn boundary* — inject a user-role notice listing the new
          mail. Fires for ANY urgency ≥ floor (default: high).

The split corresponds to Q4: blocker = stop and reply now; high = should
reply, no panic.

After the agent handles the mail (mailbox_read auto-marks read), the next
poll cycle sees no new unread blocker mail and the signal
stays clear.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import structlog

from ..persistence.models import MailboxMessage
from ..persistence.repositories import Repositories

log = structlog.get_logger()

_URGENCY_RANK = {"low": 1, "normal": 2, "high": 3, "blocker": 4}


@dataclass
class MailWatcher:
    repos: Repositories
    recipient: str
    baseline_msg_id: int
    min_urgency: str = "high"      # urgency ≥ this surfaces; "high" → high+blocker
    poll_interval_s: float = 1.0
    signal: asyncio.Event = field(default_factory=asyncio.Event)
    pending: list[MailboxMessage] = field(default_factory=list)
    _shown_baseline: int = 0
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    _task: asyncio.Task | None = None

    def __post_init__(self) -> None:
        self._shown_baseline = self.baseline_msg_id
        if self.min_urgency not in _URGENCY_RANK:
            raise ValueError(
                f"min_urgency must be one of {list(_URGENCY_RANK)!r}"
            )

    @property
    def has_blocker_pending(self) -> bool:
        """True iff any cached pending message is urgency=blocker.
        The agent loop uses this to decide mid-stream break vs
        turn-boundary-only injection."""
        return any(m.urgency == "blocker" for m in self.pending)

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("MailWatcher already started")
        self._task = asyncio.create_task(
            self._loop(), name=f"mail_watcher:{self.recipient}"
        )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def acknowledge(self) -> list[MailboxMessage]:
        """Agent loop calls this after injecting the notice. Advances the
        in-memory cursor past the highest-seen mail id so we don't re-fire
        for already-shown messages; the durable per-message read_at is
        set when the agent actually calls mailbox_read."""
        msgs = list(self.pending)
        for m in msgs:
            if m.id is not None and m.id > self._shown_baseline:
                self._shown_baseline = m.id
        self.pending.clear()
        self.signal.clear()
        return msgs

    async def _loop(self) -> None:
        log.debug(
            "mail_watcher_started",
            recipient=self.recipient,
            baseline=self.baseline_msg_id,
            min_urgency=self.min_urgency,
            poll_interval_s=self.poll_interval_s,
        )
        while not self._stop_event.is_set():
            try:
                msgs = await self.repos.mailbox.read_messages(
                    self.recipient, since_id=self._shown_baseline, limit=100
                )
                # Filter by urgency threshold
                rank_floor = _URGENCY_RANK[self.min_urgency]
                filtered = [
                    m for m in msgs
                    if m.id is not None
                    and m.id > self._shown_baseline
                    and _URGENCY_RANK.get(m.urgency, 0) >= rank_floor
                ]
                if filtered:
                    self.pending = filtered
                    if not self.signal.is_set():
                        log.info(
                            "mail_signal_raised",
                            recipient=self.recipient,
                            count=len(filtered),
                            urgencies=[m.urgency for m in filtered],
                            first_id=filtered[0].id,
                        )
                        self.signal.set()
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "mail_watcher_poll_error",
                    recipient=self.recipient,
                    error=str(exc),
                )
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.poll_interval_s
                )
            except TimeoutError:
                pass
        log.debug("mail_watcher_stopped", recipient=self.recipient)


def format_mail_notice(msgs: list[MailboxMessage]) -> str:
    """Render the user-role message we inject when new mail arrives during
    an in-flight wakeup. Wording differentiates blocker (must stop) from
    high (please handle next)."""
    has_blocker = any(m.urgency == "blocker" for m in msgs)
    if has_blocker:
        header = (
            "⚠ INTERRUPT: new urgency=blocker mailbox message(s) arrived "
            "during your wakeup. Stop your current work, handle the "
            "blocker(s) first, then respond and resume."
        )
    else:
        header = (
            "📬 REMINDER: new urgency≥high mailbox message(s) arrived "
            "during your wakeup. Handle them on this or the next turn "
            "(no need to abandon what you're doing); the next mailbox_read will auto-mark."
        )
    lines = [header, "", f"Message count: {len(msgs)}"]
    lines.append(
        "Preview (use mailbox_read for the canonical full text):"
    )
    for m in msgs[:5]:
        sender = m.sender or "?"
        body = (m.body or "").strip()
        preview = body if len(body) <= 200 else body[:200] + "…"
        lines.append(
            f"  - [id={m.id}] urgency={m.urgency} from {sender}: {preview}"
        )
    if len(msgs) > 5:
        lines.append(f"  - … and {len(msgs) - 5} more")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Back-compat aliases — drop in a future cleanup once all imports migrate
# ---------------------------------------------------------------------------

BlockerWatcher = MailWatcher
format_interrupt_notice = format_mail_notice
