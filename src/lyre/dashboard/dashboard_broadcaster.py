"""Dashboard-wide change broadcaster — replaces per-element HTMX polling.

The old dashboard had five separate `hx-trigger="every Ns"` polls (2s for
activity, 3s for the agent-status badge, 5s for home stats + health pill,
plus the per-agent timeline at 3s). Each was independent: every client tick
re-rendered a full HTML partial regardless of whether anything had changed.

This broadcaster collapses that into a single asyncio task polling the
relevant tables (tasks / wakeups / mailbox / agents) on one interval, then
fanning out *change notifications* — sets of event names like
`{"stats", "activity"}` — to SSE subscribers. The SSE endpoint renders the
fresh HTML fragment for each event and emits it to the browser via
HTMX `sse-swap`.

Latency = poll_interval (default 1s). Cheaper than the union of the old
polls because one DB pass replaces five HTTP round-trips, and rendering is
skipped entirely when nothing changed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime

import structlog

from ..persistence.repositories import Repositories

log = structlog.get_logger()


# The set of events the broadcaster emits. Each maps to one HTMX
# `sse-swap` target in the templates. Keep these stable — they're public
# strings consumed by the browser.
EVENT_STATS = "stats"
EVENT_ACTIVITY = "activity"
EVENT_AGENT_STATUS = "agent-status"
EVENT_HEALTH = "health"

ALL_EVENTS: frozenset[str] = frozenset(
    {EVENT_STATS, EVENT_ACTIVITY, EVENT_AGENT_STATUS, EVENT_HEALTH}
)


@dataclass
class _Cursor:
    """High-water marks per table. None on first scan; thereafter the
    last-seen value used to detect deltas."""

    mailbox_max_id: int | None = None
    task_max_updated: str | None = None
    wakeup_max_started: str | None = None
    wakeup_max_ended: str | None = None
    wakeup_active_count: int | None = None
    agent_max_created: str | None = None


@dataclass
class DashboardBroadcaster:
    """Polls dashboard-relevant tables, pushes change-event sets to subs.

    See module docstring for context. The MailboxBroadcaster on the home
    "Live feed" card stays — its consumer needs structured JSON payloads
    to prepend rows. This broadcaster pushes coarse "concept X changed"
    signals; the SSE endpoint renders HTML in response.
    """

    repos: Repositories
    poll_interval_s: float = 1.0
    queue_max: int = 64
    _subscribers: set[asyncio.Queue] = field(default_factory=set)
    _cursor: _Cursor = field(default_factory=_Cursor)
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    _task: asyncio.Task | None = None

    async def prime(self) -> None:
        """Initialize cursor to current state so a fresh subscriber gets
        only deltas from now on. Without priming, the first poll tick
        after startup would mark everything as 'changed' and push a wave
        of updates to subscribers."""
        self._cursor = await self._snapshot()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.queue_max)
        self._subscribers.add(q)
        log.debug("dashboard_broadcaster_subscribed", n=len(self._subscribers))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)
        log.debug("dashboard_broadcaster_unsubscribed", n=len(self._subscribers))

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("DashboardBroadcaster already started")
        self._task = asyncio.create_task(
            self._loop(), name="dashboard_broadcaster"
        )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        log.info(
            "dashboard_broadcaster_started",
            poll_interval_s=self.poll_interval_s,
        )
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.poll_interval_s
                )
                break  # stop_event was set during the wait
            except TimeoutError:
                pass
            try:
                events = await self._detect_changes()
                if events:
                    self._publish(events)
            except Exception as exc:  # noqa: BLE001
                log.warning("dashboard_broadcaster_poll_error", error=str(exc))
        log.info("dashboard_broadcaster_stopped")

    # ------------------------------------------------------------------
    # Change detection
    # ------------------------------------------------------------------

    async def _snapshot(self) -> _Cursor:
        """Read current high-water marks from each watched table."""
        mailbox_recent = await self.repos.mailbox.read_messages_paged(
            "owner", before_id=None, limit=1
        )
        mailbox_max = (
            mailbox_recent[0].id if mailbox_recent else None
        )

        # tasks: max updated_at — use find_recent and pick the freshest.
        recent_tasks = await self.repos.tasks.find_recent(limit=1)
        task_updated = (
            _iso(recent_tasks[0].updated_at) if recent_tasks else None
        )

        # wakeups: track both started_at (new wakeup begun) and ended_at
        # (terminal status fired). Plus active count so the health pill
        # updates when something finishes (the count drops).
        recent_wakeups = await self.repos.wakeups.list_recent(limit=1)
        wakeup_started = (
            _iso(recent_wakeups[0].started_at) if recent_wakeups else None
        )
        wakeup_ended = (
            _iso(recent_wakeups[0].ended_at)
            if recent_wakeups and recent_wakeups[0].ended_at
            else None
        )
        active_wakeups = await self.repos.wakeups.list_active()
        active_count = len(active_wakeups)

        agents = await self.repos.agents.list_all(include_archived=True)
        agent_max = max(
            (_iso(a.created_at) for a in agents if a.created_at),
            default=None,
        )

        return _Cursor(
            mailbox_max_id=mailbox_max,
            task_max_updated=task_updated,
            wakeup_max_started=wakeup_started,
            wakeup_max_ended=wakeup_ended,
            wakeup_active_count=active_count,
            agent_max_created=agent_max,
        )

    async def _detect_changes(self) -> set[str]:
        """Compare a fresh snapshot to the stored cursor; return the set
        of SSE event names that should fire."""
        new_cur = await self._snapshot()
        old_cur = self._cursor
        events: set[str] = set()

        mailbox_changed = (
            new_cur.mailbox_max_id is not None
            and new_cur.mailbox_max_id != old_cur.mailbox_max_id
        )
        task_changed = new_cur.task_max_updated != old_cur.task_max_updated
        wakeup_started_changed = (
            new_cur.wakeup_max_started != old_cur.wakeup_max_started
        )
        wakeup_ended_changed = (
            new_cur.wakeup_max_ended != old_cur.wakeup_max_ended
        )
        active_changed = (
            new_cur.wakeup_active_count != old_cur.wakeup_active_count
        )
        agent_changed = new_cur.agent_max_created != old_cur.agent_max_created

        # Map raw deltas → public event names.
        # stats card: anything that affects the four tiles
        if (
            mailbox_changed
            or task_changed
            or wakeup_started_changed
            or wakeup_ended_changed
            or active_changed
        ):
            events.add(EVENT_STATS)
        # activity timeline: same events drive it
        if (
            mailbox_changed
            or task_changed
            or wakeup_started_changed
            or wakeup_ended_changed
            or agent_changed
        ):
            events.add(EVENT_ACTIVITY)
        # topnav agent-status badge (count of running wakeups)
        if wakeup_started_changed or wakeup_ended_changed or active_changed:
            events.add(EVENT_AGENT_STATUS)
        # topbar health pill (live count)
        if active_changed:
            events.add(EVENT_HEALTH)

        self._cursor = new_cur
        return events

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    def _publish(self, events: set[str]) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(events)
            except asyncio.QueueFull:
                # Slow subscriber — drop oldest to keep moving.
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(events)
                except asyncio.QueueFull:
                    pass


def _iso(v: object) -> str | None:
    """Normalize a timestamp value (datetime or string) to ISO string,
    or None. Used to make cursor comparisons cheap (string equality
    instead of datetime comparisons that have to handle None / tz)."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v)
