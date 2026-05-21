"""DashboardBroadcaster: change detection across mailbox/tasks/wakeups/agents.

Tests use the broadcaster's `subscribe()` queue interface directly rather
than the HTTP SSE endpoint, mirroring how `test_dashboard_routes.py`
already tests `MailboxBroadcaster`. Avoids the EventSource client-side
complexity for a server-side correctness check.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from lyre.dashboard.dashboard_broadcaster import (
    EVENT_ACTIVITY,
    EVENT_AGENT_STATUS,
    EVENT_HEALTH,
    EVENT_STATS,
    DashboardBroadcaster,
)
from lyre.persistence.db import init_db
from lyre.persistence.models import MailboxMessage, Persona, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories


async def _seed(tmp_path: Path):
    db = tmp_path / "lyre.db"
    conn = await init_db(db)
    repos = SqliteRepositories(conn)
    await repos.personas.upsert(
        Persona(name="worker", role_description="w", system_prompt="w")
    )
    await repos.agents.create(agent_id="dispatcher", persona_name="worker")
    await repos.mailbox.ensure_mailbox("owner")
    return conn, repos


@pytest.mark.asyncio
async def test_prime_then_no_change_emits_nothing(tmp_path: Path) -> None:
    """A primed broadcaster against a quiet DB should NOT emit events.
    The cursor is at HEAD; nothing has happened since."""
    conn, repos = await _seed(tmp_path)
    try:
        bc = DashboardBroadcaster(repos=repos, poll_interval_s=0.05)
        await bc.prime()
        await bc.start()
        try:
            q = bc.subscribe()
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(q.get(), timeout=0.3)
        finally:
            await bc.stop()
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_new_mailbox_message_fires_stats_and_activity(
    tmp_path: Path,
) -> None:
    """Inserting a mailbox row must trip both the stats tile (unread
    count tile) and the activity feed (cross-agent communication is
    overview-worthy). It must NOT trip the agent-status badge or health
    pill (no wakeup state changed)."""
    conn, repos = await _seed(tmp_path)
    try:
        bc = DashboardBroadcaster(repos=repos, poll_interval_s=0.05)
        await bc.prime()
        await bc.start()
        try:
            q = bc.subscribe()
            await repos.mailbox.insert_message(
                MailboxMessage(
                    recipient="owner", external_id="new-1",
                    sender="dispatcher", urgency="normal", body="hi",
                )
            )
            events = await asyncio.wait_for(q.get(), timeout=2.0)
            assert EVENT_STATS in events
            assert EVENT_ACTIVITY in events
            assert EVENT_AGENT_STATUS not in events
            assert EVENT_HEALTH not in events
        finally:
            await bc.stop()
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_wakeup_start_fires_status_and_health(tmp_path: Path) -> None:
    """A new wakeup means a new "running" agent — the agent-status badge
    must update, and the health pill's running count climbs."""
    conn, repos = await _seed(tmp_path)
    try:
        bc = DashboardBroadcaster(repos=repos, poll_interval_s=0.05)
        await bc.prime()
        await bc.start()
        try:
            q = bc.subscribe()
            task_id = await repos.tasks.create(
                TaskSpec(persona_name="worker", goal="g", acceptance="a")
            )
            await repos.wakeups.start(task_id, "worker")
            # First poll may emit only one of the two if the task insert
            # and the wakeup insert land between different ticks; drain
            # up to two events and verify the union.
            seen: set[str] = set()
            with _drain_for(q, 1.5) as drain:
                async for events in drain:
                    seen.update(events)
                    if EVENT_AGENT_STATUS in seen and EVENT_HEALTH in seen:
                        break
            assert EVENT_AGENT_STATUS in seen
            assert EVENT_HEALTH in seen
            assert EVENT_ACTIVITY in seen
        finally:
            await bc.stop()
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_transcript_growth_fires_activity_only(tmp_path: Path) -> None:
    """Mid-wakeup: the agent appends thinking_delta / tool_use rows to
    the transcript file but the DB high-water marks are quiet. Without
    transcript-watch the dashboard sat still until the wakeup ended;
    with it the broadcaster must fire EVENT_ACTIVITY within one poll.

    Other event channels (stats / agent-status / health) MUST NOT fire
    — transcript growth doesn't change unread counts, running-wakeup
    count, etc.
    """
    conn, repos = await _seed(tmp_path)
    try:
        # Set up an active wakeup with a real transcript file path.
        task_id = await repos.tasks.create(
            TaskSpec(persona_name="worker", goal="g", acceptance="a")
        )
        wakeup_id = await repos.wakeups.start(task_id, "worker")
        transcript = tmp_path / "object_store" / "wakeups" / wakeup_id / "transcript.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text('{"type":"system","ts":1}\n', encoding="utf-8")
        await repos.wakeups.set_transcript_uri(wakeup_id, f"file://{transcript}")

        bc = DashboardBroadcaster(repos=repos, poll_interval_s=0.05)
        await bc.prime()
        await bc.start()
        try:
            q = bc.subscribe()

            # Simulate agent_loop appending a thinking_delta line.
            with transcript.open("a", encoding="utf-8") as fp:
                fp.write('{"type":"thinking_delta","text":"hmm","ts":2}\n')

            seen: set[str] = set()
            with _drain_for(q, 1.5) as drain:
                async for events in drain:
                    seen.update(events)
                    if EVENT_ACTIVITY in seen:
                        break
            assert EVENT_ACTIVITY in seen, (
                "transcript file growth must trigger EVENT_ACTIVITY so "
                "the agent-detail SSE stream refreshes mid-wakeup"
            )
            # transcript pulse is activity-only — channels gated on DB
            # state stay quiet.
            assert EVENT_STATS not in seen
            assert EVENT_AGENT_STATUS not in seen
            assert EVENT_HEALTH not in seen
        finally:
            await bc.stop()
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_does_not_replay_pre_existing_state(tmp_path: Path) -> None:
    """`prime()` snapshots the world; rows inserted BEFORE prime must
    not produce a notification AFTER subscribers connect."""
    conn, repos = await _seed(tmp_path)
    try:
        await repos.mailbox.insert_message(
            MailboxMessage(
                recipient="owner", external_id="historical",
                sender="dispatcher", urgency="normal", body="old",
            )
        )
        bc = DashboardBroadcaster(repos=repos, poll_interval_s=0.05)
        await bc.prime()
        await bc.start()
        try:
            q = bc.subscribe()
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(q.get(), timeout=0.3)
        finally:
            await bc.stop()
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _drain_for:  # noqa: N801 — context-manager-as-class fine here
    """Drain everything a queue produces within `timeout_s` seconds.

    Usage:
        with _drain_for(q, 1.5) as drain:
            async for events in drain:
                ...
                if condition: break
    """

    def __init__(self, queue: asyncio.Queue, timeout_s: float):
        self.queue = queue
        self.timeout_s = timeout_s

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    async def __aiter__(self):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_s
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            try:
                yield await asyncio.wait_for(self.queue.get(), timeout=remaining)
            except TimeoutError:
                return
