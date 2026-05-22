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
from pathlib import Path

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
    # Sum of file sizes for every active wakeup's transcript.jsonl.
    # The DB-side high-water marks above only flip when a wakeup STARTS
    # or ENDS — they're silent during the long middle of a turn while
    # the agent writes thinking_delta / content_delta / tool_use rows.
    # Watching transcript-file growth here is what makes mid-wakeup
    # SSE refresh actually fire, so the dashboard can stream the
    # model's reasoning as it happens instead of standing still until
    # ended_at finally moves. Size (vs mtime) chosen because transcripts
    # are append-only and ms-resolution mtime is filesystem-dependent.
    transcript_size_total: int | None = None


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
    # Each event payload is the set of event names that changed; the
    # shutdown sentinel is an empty `frozenset()` plus a closing None.
    _subscribers: set[asyncio.Queue[set[str] | None]] = field(default_factory=set)
    _cursor: _Cursor = field(default_factory=_Cursor)
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    _task: asyncio.Task[None] | None = None

    async def prime(self) -> None:
        """Initialize cursor to current state so a fresh subscriber gets
        only deltas from now on. Without priming, the first poll tick
        after startup would mark everything as 'changed' and push a wave
        of updates to subscribers."""
        self._cursor = await self._snapshot()

    def subscribe(self) -> asyncio.Queue[set[str] | None]:
        q: asyncio.Queue[set[str] | None] = asyncio.Queue(
            maxsize=self.queue_max
        )
        self._subscribers.add(q)
        log.debug("dashboard_broadcaster_subscribed", n=len(self._subscribers))
        return q

    def unsubscribe(self, q: asyncio.Queue[set[str] | None]) -> None:
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
        # Wake every subscribed SSE handler IMMEDIATELY (None sentinel
        # on each queue). Without this, uvicorn's graceful shutdown
        # waits for handlers to notice via their queue.get timeout —
        # several seconds of "exit hangs" on Ctrl-C.
        self._wake_subscribers_for_shutdown()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def _wake_subscribers_for_shutdown(self) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(None)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

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
            # Skip the snapshot entirely when no one's listening. py-spy
            # showed the dashboard spending ~all its time in aiosqlite's
            # worker thread; the broadcaster polling every second
            # regardless of subscribers was a significant contributor.
            # Snapshots and event-detect only run when ≥1 SSE handler
            # is subscribed.
            if not self._subscribers:
                continue
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

    # All high-water-mark probes fit in a single SQL via correlated
    # subqueries. The original implementation called five separate repo
    # methods, each loading FULL Pydantic models (including a list_all
    # over the agents table) just to extract one scalar — py-spy
    # confirmed this was a noticeable share of aiosqlite worker time.
    # One SQL = one round-trip on the shared connection, one row
    # returned. SQLite optimizes the SELECT MAX() against indexed
    # columns to ~O(1) per subquery.
    _SNAPSHOT_SQL = """
        SELECT
            (SELECT MAX(id) FROM mailbox_messages
                WHERE recipient='owner')                       AS mailbox_max_id,
            (SELECT MAX(updated_at) FROM tasks)                AS task_max_updated,
            (SELECT MAX(started_at) FROM wakeups)              AS wakeup_max_started,
            (SELECT MAX(ended_at)   FROM wakeups)              AS wakeup_max_ended,
            (SELECT COUNT(*) FROM wakeups WHERE ended_at IS NULL)
                                                               AS wakeup_active_count,
            (SELECT MAX(created_at) FROM agents)               AS agent_max_created
    """

    async def _snapshot(self) -> _Cursor:
        """Read current high-water marks in a single SQL query, plus a
        cheap stat-walk over any active wakeup's transcript file."""
        async with self.repos.conn.execute(self._SNAPSHOT_SQL) as cur:
            row = await cur.fetchone()
        if row is None:
            return _Cursor(transcript_size_total=0)
        return _Cursor(
            mailbox_max_id=row["mailbox_max_id"],
            task_max_updated=row["task_max_updated"],
            wakeup_max_started=row["wakeup_max_started"],
            wakeup_max_ended=row["wakeup_max_ended"],
            wakeup_active_count=row["wakeup_active_count"],
            agent_max_created=row["agent_max_created"],
            transcript_size_total=await self._transcript_size_total(),
        )

    async def _transcript_size_total(self) -> int:
        """Sum file sizes for every in-flight wakeup's transcript.

        Returns 0 (not None) when there are no active wakeups so any
        previous in-flight bytes cleanly clear from the cursor — the
        cursor uses `!=` for delta detection, and 0→None would
        spuriously look "changed" forever.

        Cheap: typically zero or one active wakeups, each a single
        `stat()` syscall. Wrapped in try/except because the file may
        not yet exist (wakeup just started, agent_loop hasn't written
        anything) or vanish under us (object store cleanup) — neither
        is fatal.
        """
        try:
            active = await self.repos.wakeups.list_active()
        except Exception:  # noqa: BLE001 — never let the watcher crash the loop
            return 0
        total = 0
        for w in active:
            uri = w.transcript_uri
            if not uri or not uri.startswith("file://"):
                continue
            path = Path(uri[len("file://"):])
            try:
                total += path.stat().st_size
            except OSError:
                continue  # missing / unreadable — treat as zero
        return total

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
        # `transcript_size_total` is the mid-wakeup pulse — wakeup_*
        # cover start / end, this covers everything between. We compare
        # against `old_cur` but only when it has a value already, so
        # the very first tick (which always looks "changed") doesn't
        # storm subscribers right after prime().
        transcript_changed = (
            old_cur.transcript_size_total is not None
            and new_cur.transcript_size_total != old_cur.transcript_size_total
        )

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
        # activity timeline: same DB events + the new transcript pulse
        # so thinking / tool_use / content_delta land in the feed within
        # one poll tick of being written, not just at wakeup_end.
        if (
            mailbox_changed
            or task_changed
            or wakeup_started_changed
            or wakeup_ended_changed
            or agent_changed
            or transcript_changed
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


