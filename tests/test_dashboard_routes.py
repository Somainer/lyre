"""Dashboard route + broadcaster tests.

We exercise the FastAPI app via TestClient against a real on-disk SQLite,
seeded with realistic-shape data (a running wakeup with a transcript on
disk so /activity has tool_use lines to parse).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from lyre.dashboard import MailboxBroadcaster, create_app
from lyre.persistence.db import init_db
from lyre.persistence.models import MailboxMessage, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.personas.seed import seed_personas


@pytest_asyncio.fixture
async def seeded_dashboard(
    tmp_path: Path,
) -> AsyncIterator[tuple[TestClient, SqliteRepositories, dict]]:
    """Spin up a dashboard against a freshly-seeded DB + a fake in-flight
    wakeup with a transcript. Yields (client, repos, ids)."""
    db = tmp_path / "lyre.db"
    obj = tmp_path / "objstore"
    obj.mkdir(parents=True)
    conn = await init_db(db)
    repos = SqliteRepositories(conn)
    await seed_personas(repos.personas)
    # Post-A3: dashboard send-form validates recipient against agents.
    # Seed an agent for each persona (id == persona name) so existing
    # CLI/dashboard usage like `send leader "..."` keeps working.
    for persona in await repos.personas.list_active():
        await repos.agents.create(
            agent_id=persona.name, persona_name=persona.name
        )
    await repos.mailbox.ensure_mailbox("owner")

    # Seed: one running task w/ active wakeup + a finished task
    running_tid = await repos.tasks.create(
        TaskSpec(persona_name="worker-maintainer", goal="demo task",
                 acceptance="demo done")
    )
    await repos.tasks.update_status(running_tid, "in_progress")
    running_wid = await repos.wakeups.start(running_tid, "worker-maintainer")
    # Transcript with tool_use lines (the activity tab tail-reads these)
    wdir = obj / "wakeups" / running_wid
    wdir.mkdir(parents=True)
    tp = wdir / "transcript.jsonl"
    tp.write_text("\n".join([
        json.dumps({"type": "tool_use", "id": "x1", "name": "python_exec",
                    "input": {"code": "print('hi')"}, "ts": 1747500000000}),
        json.dumps({"type": "tool_use", "id": "x2", "name": "shell_exec",
                    "input": {"argv": ["git", "status"]}, "ts": 1747500001000}),
    ]) + "\n")
    await repos.wakeups.set_transcript_uri(running_wid, f"file://{tp}")

    done_tid = await repos.tasks.create(
        TaskSpec(persona_name="leader", goal="prior task", acceptance="ok")
    )
    await repos.tasks.update_status(done_tid, "completed")
    done_wid = await repos.wakeups.start(done_tid, "leader")
    await repos.wakeups.end(done_wid, end_status="completed", metering={
        "token_input": 500, "token_output": 120, "wall_clock_ms": 3200,
        "tool_call_count": 2, "provider": "anthropic", "model": "claude-sonnet-4-6",
    })

    # Seed mailbox: a normal + a blocker
    await repos.mailbox.insert_message(
        MailboxMessage(recipient="owner", external_id="m-normal",
                       sender="leader", urgency="normal", body="status update")
    )
    await repos.mailbox.insert_message(
        MailboxMessage(recipient="owner", external_id="m-block",
                       sender="worker-maintainer", urgency="blocker",
                       body="STOP, awaiting decision")
    )

    broadcaster = MailboxBroadcaster(
        repos=repos, recipient="owner", poll_interval_s=0.05,
    )
    await broadcaster.prime()

    app = create_app(repos, broadcaster)
    client = TestClient(app)
    ids = {"running_task": running_tid, "done_task": done_tid,
           "running_wakeup": running_wid}
    try:
        yield client, repos, ids
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------


def test_home_renders_with_cards_and_blockers(
    seeded_dashboard: tuple[TestClient, SqliteRepositories, dict],
) -> None:
    client, *_ = seeded_dashboard
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    # Stat-tile labels (new IA — snake_case statuses, breakdown segments).
    assert "in_progress" in body
    assert "blockers unread" in body
    assert "Tasks in flight" in body
    assert "Unread mail to owner" in body
    assert "STOP, awaiting decision" in body  # blocker preview


def test_activity_is_overview_only_no_transcript_noise(
    seeded_dashboard: tuple[TestClient, SqliteRepositories, dict],
) -> None:
    """/activity is the high-level cross-agent overview: tasks, wakeup
    boundaries, mailbox. Transcript-derived events (tool_use,
    assistant_text, turn_end, notes) live at /agents/<id> so this page
    stays scan-able."""
    client, *_ = seeded_dashboard
    r = client.get("/activity")
    assert r.status_code == 200
    body = r.text
    # The active-strip should declare an agent is running
    assert "agent" in body.lower()
    assert "worker-maintainer" in body
    # Mailbox events DO appear (cross-agent communication is overview-worthy)
    assert "STOP" in body
    # Filter selector
    assert "30m" in body and "5m" in body
    # Transcript-derived events do NOT appear at the overview level — go
    # to /agents/<id> for the drill-down.
    assert "python_exec" not in body
    assert "shell_exec" not in body


def test_agent_detail_includes_tool_use_from_active_transcript(
    seeded_dashboard: tuple[TestClient, SqliteRepositories, dict],
) -> None:
    """The transcript noise that used to be on /activity is now scoped
    to /agents/<id> — owner can drill into the agent they're
    troubleshooting."""
    client, *_ = seeded_dashboard
    r = client.get("/agents/worker-maintainer")
    assert r.status_code == 200
    body = r.text
    assert "python_exec" in body
    assert "shell_exec" in body


def test_agents_list_renders_all_live_agents(
    seeded_dashboard: tuple[TestClient, SqliteRepositories, dict],
) -> None:
    """/agents lists every live agent. Seeded personas → seeded agents
    (id == persona name) → all appear."""
    client, *_ = seeded_dashboard
    r = client.get("/agents")
    assert r.status_code == 200
    body = r.text
    assert "leader" in body
    assert "worker-maintainer" in body
    # busy indicator on the agent that's currently running a wakeup
    assert "running" in body or "busy" in body


def test_agent_detail_filters_to_only_that_agent(
    seeded_dashboard: tuple[TestClient, SqliteRepositories, dict],
) -> None:
    """The per-agent timeline must not bleed in events from other
    agents — that's the whole point of the drill-down."""
    client, *_ = seeded_dashboard
    # The seeded mailbox has messages: leader→owner and
    # worker-maintainer→owner. The /agents/leader page must show
    # leader's mail and NOT worker-maintainer's.
    r = client.get("/agents/leader")
    assert r.status_code == 200
    body = r.text
    # Cross-agent send by worker-maintainer must be filtered out.
    assert "STOP, awaiting decision" not in body
    # Leader-originated mail must still show (it involves leader).
    assert "status update" in body


def test_agent_detail_404_for_unknown_agent(
    seeded_dashboard: tuple[TestClient, SqliteRepositories, dict],
) -> None:
    client, *_ = seeded_dashboard
    r = client.get("/agents/ghost-agent")
    assert r.status_code == 404


def test_activity_partial_returns_same_event_kinds(
    seeded_dashboard: tuple[TestClient, SqliteRepositories, dict],
) -> None:
    """Activity feed renders the post-rework event types. wakeup_start
    and turn_end were dropped (pure lifecycle noise); wakeup_end stays
    because it carries the terminal status."""
    client, *_ = seeded_dashboard
    r = client.get("/partials/activity?minutes=60")
    assert r.status_code == 200
    # Active-strip + chat-bubble feed shell.
    assert "active-strip" in r.text
    # Task transitions still surface.
    assert "task" in r.text
    # wakeup_start was intentionally removed — its absence is part of
    # the contract.
    assert "wakeup_start" not in r.text


def test_agent_detail_surfaces_assistant_text_from_completed_wakeup(
    tmp_path: Path,
) -> None:
    """Owner needs to read what the model SAID to troubleshoot. The
    per-agent timeline at /agents/<id> aggregates content_delta from
    *completed* wakeups' transcripts into one assistant_text event
    (otherwise debugging requires `lyre audit`)."""
    import asyncio as _asyncio
    import json as _json

    from lyre.dashboard import create_app
    from lyre.dashboard.sse import MailboxBroadcaster
    from lyre.persistence.db import init_db
    from lyre.persistence.models import TaskSpec
    from lyre.persistence.sqlite_impl import SqliteRepositories
    from lyre.personas.seed import seed_personas

    async def _setup():
        db = tmp_path / "lyre.db"
        obj = tmp_path / "objstore"
        obj.mkdir(parents=True)
        conn = await init_db(db)
        repos = SqliteRepositories(conn)
        await seed_personas(repos.personas)
        for p in await repos.personas.list_active():
            await repos.agents.create(agent_id=p.name, persona_name=p.name)
        await repos.mailbox.ensure_mailbox("owner")
        # A completed wakeup with a transcript containing content_delta
        # chunks the model emitted before mailbox_send.
        tid = await repos.tasks.create(
            TaskSpec(agent_id="leader", goal="g", acceptance="a")
        )
        wid = await repos.wakeups.start(tid, "leader")
        await repos.wakeups.end(wid, end_status="completed", metering={
            "token_input": 100, "token_output": 30, "wall_clock_ms": 800,
            "tool_call_count": 1, "provider": "anthropic", "model": "x",
        })
        wdir = obj / "wakeups" / wid
        wdir.mkdir(parents=True)
        tp = wdir / "transcript.jsonl"
        tp.write_text("\n".join([
            _json.dumps({"type": "content_delta",
                         "text": "Let me think about this", "ts": 1000}),
            _json.dumps({"type": "content_delta",
                         "text": " before replying.", "ts": 1100}),
            _json.dumps({"type": "tool_use", "id": "t1", "name": "mailbox_send",
                         "input": {"to": "owner", "body": "ack"}, "ts": 1200}),
            _json.dumps({"type": "turn_end", "turn": 1,
                         "stop_reason": "end_turn", "text_len": 35,
                         "tool_count": 1, "ts": 1300}),
        ]) + "\n")
        await repos.wakeups.set_transcript_uri(wid, f"file://{tp}")
        broadcaster = MailboxBroadcaster(
            repos=repos, recipient="owner", poll_interval_s=0.05,
        )
        await broadcaster.prime()
        app = create_app(repos, broadcaster)
        return app, conn

    app, conn = _asyncio.get_event_loop().run_until_complete(_setup())
    try:
        client = TestClient(app)
        # Per-agent drill-down — that's where transcripts live now.
        r = client.get("/agents/leader?minutes=60")
        assert r.status_code == 200
        body = r.text
        # Assistant text rendered as a chat-style "assistant" bubble in
        # the new design.
        assert "bub assistant" in body
        # Both content_delta chunks aggregated together
        assert "Let me think about this before replying." in body
        # And the completed wakeup's tool_use is included
        assert "mailbox_send" in body
    finally:
        _asyncio.get_event_loop().run_until_complete(conn.close())


def test_agent_status_partial_shows_active_count(
    seeded_dashboard: tuple[TestClient, SqliteRepositories, dict],
) -> None:
    """The topnav agent-status badge now shows a live-dot + running count
    (the persona/agent identity moved into Activity's chat-bubble who-column).
    """
    client, *_ = seeded_dashboard
    r = client.get("/partials/agent-status")
    assert r.status_code == 200
    assert "running" in r.text
    assert "live-dot" in r.text


def test_mail_shows_all_urgencies_by_default(
    seeded_dashboard: tuple[TestClient, SqliteRepositories, dict],
) -> None:
    """Mail (merged inbox+feed) shows ALL urgencies by default — the
    earlier hard-coded min_urgency='high' silently hid normal-urgency
    replies (user reported 'reply 到我的邮件的邮件无法看到')."""
    client, *_ = seeded_dashboard
    r = client.get("/mail")
    assert r.status_code == 200
    body = r.text
    assert "STOP, awaiting decision" in body  # blocker
    assert "status update" in body  # normal — was hidden before


def test_mail_filters_to_blocker_band(
    seeded_dashboard: tuple[TestClient, SqliteRepositories, dict],
) -> None:
    """`?u=blocker` filter chip restricts to blocker urgency only."""
    client, *_ = seeded_dashboard
    r = client.get("/mail?u=blocker")
    assert r.status_code == 200
    body = r.text
    assert "STOP, awaiting decision" in body
    # Count actual data rows (not the column-header row which shares
    # the `mail-row` substring via `mail-row-head`). The reply icon's
    # href is unique-per-row, so we can count those instead.
    rows = body.count('class="btn ghost sm mail-reply-btn"')
    assert rows == 1, f"blocker band should leave exactly 1 row, got {rows}"


def test_mail_feed_band_shows_low_and_normal(
    seeded_dashboard: tuple[TestClient, SqliteRepositories, dict],
) -> None:
    """`?u=feed` covers normal + low (the old /feed semantic)."""
    client, *_ = seeded_dashboard
    r = client.get("/mail?u=feed")
    assert r.status_code == 200
    body = r.text
    assert "status update" in body  # normal
    assert "STOP, awaiting decision" not in body.split("<script")[0]  # blocker excluded


def test_runs_tasks_tab_lists_all_with_status_filter(
    seeded_dashboard: tuple[TestClient, SqliteRepositories, dict],
) -> None:
    """The Runs page replaces the old /tasks + /wakeups split. Tasks tab
    shows all tasks by default; ?status=<x> narrows."""
    client, _, ids = seeded_dashboard
    r = client.get("/runs")
    assert r.status_code == 200
    body = r.text
    assert ids["running_task"][:8] in body
    assert ids["done_task"][:8] in body
    assert "demo task" in body
    assert "prior task" in body

    r = client.get("/runs?tab=tasks&status=in_progress")
    assert r.status_code == 200
    body = r.text
    assert "demo task" in body
    assert "prior task" not in body


def test_task_detail_lists_children(
    seeded_dashboard: tuple[TestClient, SqliteRepositories, dict],
) -> None:
    client, _, ids = seeded_dashboard
    r = client.get(f"/tasks/{ids['running_task']}")
    assert r.status_code == 200
    body = r.text
    assert "demo task" in body
    assert "no subagent children" in body


def test_task_detail_404_for_unknown(
    seeded_dashboard: tuple[TestClient, SqliteRepositories, dict],
) -> None:
    client, *_ = seeded_dashboard
    r = client.get("/tasks/ghost-task-id")
    assert r.status_code == 404


def test_runs_wakeups_tab_lists_recent(
    seeded_dashboard: tuple[TestClient, SqliteRepositories, dict],
) -> None:
    client, _, ids = seeded_dashboard
    r = client.get("/runs?tab=wakeups")
    assert r.status_code == 200
    body = r.text
    assert ids["running_wakeup"][:8] in body


def test_send_form_renders(
    seeded_dashboard: tuple[TestClient, SqliteRepositories, dict],
) -> None:
    client, *_ = seeded_dashboard
    r = client.get("/send?to=worker-maintainer")
    assert r.status_code == 200
    assert "worker-maintainer" in r.text
    assert "urgency" in r.text


def test_send_post_writes_message_and_shows_banner(
    seeded_dashboard: tuple[TestClient, SqliteRepositories, dict],
) -> None:
    client, repos, _ = seeded_dashboard
    r = client.post(
        "/send",
        data={"recipient": "leader", "body": "from dashboard",
              "urgency": "high", "sender": "owner"},
    )
    assert r.status_code == 200
    body = r.text
    assert "banner success" in body
    assert "leader" in body
    assert "watch what happens in Activity" in body

    # Side effect: mailbox row exists
    async def _check() -> bool:
        msgs = await repos.mailbox.read_messages("leader")
        return any(m.body == "from dashboard" and m.urgency == "high" for m in msgs)
    assert asyncio.get_event_loop().run_until_complete(_check())


def test_send_post_rejects_unknown_agent_recipient(
    seeded_dashboard: tuple[TestClient, SqliteRepositories, dict],
) -> None:
    """Hallucinated recipient must error out, not silently create a
    phantom mailbox. Mirrors the CLI / tool validation."""
    client, *_ = seeded_dashboard
    r = client.post(
        "/send",
        data={"recipient": "ghost-agent", "body": "x",
              "urgency": "normal", "sender": "owner"},
    )
    assert r.status_code == 400
    assert "unknown agent" in r.text


def test_send_form_with_reply_to_shows_original_message(
    seeded_dashboard: tuple[TestClient, SqliteRepositories, dict],
) -> None:
    """GET /send?reply_to=<id> loads the original mail as context and
    pre-fills `to` with the original sender."""
    client, repos, _ = seeded_dashboard

    # The fixture seeds two messages addressed to owner; pick one and
    # imagine owner clicking 'Reply' on it.
    async def _first_msg_id() -> int:
        msgs = await repos.mailbox.read_messages("owner")
        return msgs[0].id  # type: ignore[return-value]
    msg_id = asyncio.get_event_loop().run_until_complete(_first_msg_id())

    r = client.get(f"/send?reply_to={msg_id}")
    assert r.status_code == 200
    body = r.text
    # Reply context banner
    assert "Replying to mailbox" in body
    assert f"#{msg_id}" in body
    # Original sender pre-filled as recipient (so reply goes back to them)
    assert 'value="leader"' in body or 'value="worker-maintainer"' in body
    # Hidden reply_to field round-trips
    assert f'name="reply_to" value="{msg_id}"' in body


def test_send_post_with_reply_to_threads_parent_msg_id(
    seeded_dashboard: tuple[TestClient, SqliteRepositories, dict],
) -> None:
    """POST with reply_to writes parent_msg_id on the new message."""
    client, repos, _ = seeded_dashboard

    async def _first_msg_id() -> int:
        msgs = await repos.mailbox.read_messages("owner")
        return msgs[0].id  # type: ignore[return-value]
    parent_id = asyncio.get_event_loop().run_until_complete(_first_msg_id())

    r = client.post(
        "/send",
        data={
            "recipient": "leader",
            "body": "thanks for the update",
            "urgency": "normal",
            "sender": "owner",
            "reply_to": str(parent_id),
        },
    )
    assert r.status_code == 200
    assert "banner success" in r.text
    assert f"in reply to #{parent_id}" in r.text

    async def _last_reply() -> object:
        msgs = await repos.mailbox.read_messages("leader")
        return next(
            (m for m in msgs if m.body == "thanks for the update"), None
        )
    sent = asyncio.get_event_loop().run_until_complete(_last_reply())
    assert sent is not None
    assert sent.parent_msg_id == parent_id  # type: ignore[union-attr]


def test_send_post_rejects_bad_urgency(
    seeded_dashboard: tuple[TestClient, SqliteRepositories, dict],
) -> None:
    client, *_ = seeded_dashboard
    r = client.post(
        "/send",
        data={"recipient": "leader", "body": "x",
              "urgency": "panic", "sender": "owner"},
    )
    assert r.status_code == 400
    assert "banner-err" in r.text or "banner error" in r.text


# ---------------------------------------------------------------------------
# Broadcaster pub-sub
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_broadcaster_publishes_new_owner_messages(
    tmp_path: Path,
) -> None:
    """Insert a new owner-bound message; the broadcaster's poll loop must
    push it onto every subscribed queue within a few ticks."""
    db = tmp_path / "lyre.db"
    conn = await init_db(db)
    try:
        repos = SqliteRepositories(conn)
        await repos.mailbox.ensure_mailbox("owner")

        bc = MailboxBroadcaster(
            repos=repos, recipient="owner", poll_interval_s=0.05,
        )
        await bc.prime()
        await bc.start()
        try:
            q = bc.subscribe()
            await repos.mailbox.insert_message(
                MailboxMessage(
                    recipient="owner", external_id="live-1",
                    sender="leader", urgency="normal",
                    body="live update",
                )
            )
            msg = await asyncio.wait_for(q.get(), timeout=2.0)
            assert msg.body == "live update"
            assert msg.recipient == "owner"
        finally:
            await bc.stop()
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_broadcaster_does_not_replay_pre_existing_messages(
    tmp_path: Path,
) -> None:
    """`prime()` advances the watermark; an already-stored message must NOT
    be pushed to subscribers that connect after prime."""
    db = tmp_path / "lyre.db"
    conn = await init_db(db)
    try:
        repos = SqliteRepositories(conn)
        await repos.mailbox.ensure_mailbox("owner")
        await repos.mailbox.insert_message(
            MailboxMessage(
                recipient="owner", external_id="historical",
                sender="x", urgency="normal", body="old",
            )
        )

        bc = MailboxBroadcaster(
            repos=repos, recipient="owner", poll_interval_s=0.05,
        )
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
async def test_broadcaster_filters_by_recipient(
    tmp_path: Path,
) -> None:
    """A broadcaster watching `owner` must ignore messages to other recipients."""
    db = tmp_path / "lyre.db"
    conn = await init_db(db)
    try:
        repos = SqliteRepositories(conn)
        await repos.mailbox.ensure_mailbox("owner")
        await repos.mailbox.ensure_mailbox("leader")
        bc = MailboxBroadcaster(
            repos=repos, recipient="owner", poll_interval_s=0.05,
        )
        await bc.prime()
        await bc.start()
        try:
            q = bc.subscribe()
            # Insert a non-owner message
            await repos.mailbox.insert_message(
                MailboxMessage(
                    recipient="leader", external_id="not-mine",
                    sender="owner", urgency="normal", body="for leader",
                )
            )
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(q.get(), timeout=0.3)
        finally:
            await bc.stop()
    finally:
        await conn.close()
