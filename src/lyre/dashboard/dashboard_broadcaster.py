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
from pathlib import Path

import structlog

from ..persistence.models import Wakeup
from ..persistence.repositories import Repositories
from ..runtime.transcript import transcript_path
from ..transcript_tail import TranscriptTailer
from .activity import LiveTranscriptFolder

log = structlog.get_logger()


# The set of events the broadcaster emits. Each maps to one HTMX
# `sse-swap` target in the templates. Keep these stable — they're public
# strings consumed by the browser.
EVENT_STATS = "stats"
EVENT_ACTIVITY = "activity"
EVENT_AGENT_STATUS = "agent-status"
EVENT_HEALTH = "health"

# Per-wakeup live-card events: "wakeup-card:<wakeup_id>". Dynamic names —
# each active wakeup's card element declares sse-swap for its own id; the
# SSE handler treats any name with this prefix as "render that card".
CARD_EVENT_PREFIX = "wakeup-card:"

ALL_EVENTS: frozenset[str] = frozenset(
    {EVENT_STATS, EVENT_ACTIVITY, EVENT_AGENT_STATUS, EVENT_HEALTH}
)


@dataclass
class LiveWakeup:
    """Broadcaster-held streaming state for one active wakeup: an
    incremental file tailer + the fold of everything it has read.
    Shared by every SSE subscriber — one file read per poll tick total,
    regardless of how many browser tabs are watching."""

    wakeup: Wakeup
    tailer: TranscriptTailer
    folder: LiveTranscriptFolder


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
    # Root of the object store, for deriving active wakeups' transcript
    # paths (their transcript_uri column is NULL until end-of-wakeup).
    # None disables live cards (tests / minimal embeddings) — the DB
    # high-water events still work.
    object_store_root: Path | None = None
    _live: dict[str, LiveWakeup] = field(default_factory=dict)
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
        """Read current high-water marks in a single SQL query."""
        async with self.repos.conn.execute(self._SNAPSHOT_SQL) as cur:
            row = await cur.fetchone()
        if row is None:
            return _Cursor()
        return _Cursor(
            mailbox_max_id=row["mailbox_max_id"],
            task_max_updated=row["task_max_updated"],
            wakeup_max_started=row["wakeup_max_started"],
            wakeup_max_ended=row["wakeup_max_ended"],
            wakeup_active_count=row["wakeup_active_count"],
            agent_max_created=row["agent_max_created"],
        )

    def live_folders(self) -> dict[str, LiveTranscriptFolder]:
        """Snapshot view for the activity renderers — folded streaming
        state per active wakeup, no file I/O on the render path."""
        return {wid: lw.folder for wid, lw in self._live.items()}

    def live_wakeup(self, wakeup_id: str) -> LiveWakeup | None:
        return self._live.get(wakeup_id)

    async def _poll_live(self) -> set[str]:
        """Tail every active wakeup's transcript incrementally; return a
        per-card event name for each card whose content advanced.

        The DB-side high-water marks only flip when a wakeup STARTS or
        ENDS — they're silent during the long middle of a turn while the
        agent streams thinking_delta / content_delta / tool_use rows.
        This is what makes mid-wakeup SSE updates actually fire, so the
        dashboard streams the model's output as it happens instead of
        standing still until ended_at finally moves. Each tick reads
        only the NEW bytes (offset-based tailer), so cost stays O(new
        output), not O(transcript length).
        """
        if self.object_store_root is None:
            return set()
        try:
            active = await self.repos.wakeups.list_active()
        except Exception:  # noqa: BLE001 — never let the watcher crash the loop
            return set()
        changed: set[str] = set()
        seen: set[str] = set()
        for w in active:
            seen.add(w.id)
            lw = self._live.get(w.id)
            if lw is None:
                started_iso = (
                    w.started_at.isoformat()
                    if isinstance(w.started_at, datetime)
                    else (w.started_at or "")
                )
                lw = LiveWakeup(
                    wakeup=w,
                    tailer=TranscriptTailer(
                        transcript_path(self.object_store_root, w.id)
                    ),
                    folder=LiveTranscriptFolder(
                        wakeup_id=w.id,
                        persona=w.persona_name,
                        task_id=w.task_id,
                        started_at=started_iso,
                    ),
                )
                self._live[w.id] = lw
            rows = await asyncio.to_thread(lw.tailer.poll)
            if rows:
                lw.folder.ingest(rows)
                changed.add(f"{CARD_EVENT_PREFIX}{w.id}")
        # Wakeups that ended (or vanished) drop their state — the
        # wakeup_max_ended watermark fires EVENT_ACTIVITY on the same
        # tick, and the full timeline render replaces the card with the
        # regular ended-wakeup events.
        for gone in set(self._live) - seen:
            del self._live[gone]
        return changed

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
        # activity timeline: lifecycle changes only. Mid-wakeup streaming
        # deliberately does NOT re-render the timeline — it rides the
        # per-card events from _poll_live below, so one growing wakeup
        # costs one small card render per tick, not a full feed
        # re-render + whole-timeline DOM swap.
        if (
            mailbox_changed
            or task_changed
            or wakeup_started_changed
            or wakeup_ended_changed
            or agent_changed
        ):
            events.add(EVENT_ACTIVITY)
        events |= await self._poll_live()
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


