"""Smoke tests for SQLite repositories.

Goal: every method exercised in Sprint 0/1 actually works as advertised on a real
SQLite file. These tests are the safety net before we wire more on top.
"""

from __future__ import annotations

import asyncio

import pytest

from lyre.persistence.models import Blob, MailboxMessage, OutboxRow, Persona, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories


@pytest.mark.asyncio
async def test_persona_upsert_and_get(repos: SqliteRepositories) -> None:
    p = Persona(
        name="dispatcher",
        role_description="boss",
        system_prompt="you lead",
        allowed_lyre_tools=["mailbox_send", "dispatch_task"],
    )
    await repos.personas.upsert(p)
    fetched = await repos.personas.get("dispatcher")
    assert fetched is not None
    assert fetched.name == "dispatcher"
    assert fetched.allowed_lyre_tools == ["mailbox_send", "dispatch_task"]
    assert fetched.status == "approved"

    p2 = Persona(
        name="dispatcher",
        role_description="boss v2",
        system_prompt="you lead v2",
        allowed_lyre_tools=["mailbox_send"],
    )
    await repos.personas.upsert(p2)
    fetched2 = await repos.personas.get("dispatcher")
    assert fetched2 is not None
    assert fetched2.role_description == "boss v2"


@pytest.mark.asyncio
async def test_task_lifecycle_with_lease(repos: SqliteRepositories) -> None:
    await repos.personas.upsert(
        Persona(name="worker", role_description="w", system_prompt="w")
    )

    task_id = await repos.tasks.create(
        TaskSpec(persona_name="worker", goal="g", acceptance="a")
    )
    assert task_id

    task = await repos.tasks.get(task_id)
    assert task is not None
    assert task.status == "pending"
    assert task.lease_holder is None

    # First claim succeeds; second concurrent claim fails.
    wakeup_a = "wakeup-a"
    wakeup_b = "wakeup-b"
    assert await repos.tasks.claim_lease(task_id, wakeup_a, duration_sec=60)
    assert not await repos.tasks.claim_lease(task_id, wakeup_b, duration_sec=60)

    # Owner can renew, non-owner cannot.
    assert await repos.tasks.renew_lease(task_id, wakeup_a, duration_sec=60)
    assert not await repos.tasks.renew_lease(task_id, wakeup_b, duration_sec=60)

    # Updating checkpoint as holder works; as non-holder is a no-op.
    await repos.tasks.update_checkpoint(task_id, {"phase": "edit"}, wakeup_a)
    after = await repos.tasks.get(task_id)
    assert after is not None and after.checkpoint == {"phase": "edit"}

    await repos.tasks.release_lease(task_id, wakeup_a)
    released = await repos.tasks.get(task_id)
    assert released is not None
    assert released.lease_holder is None


@pytest.mark.asyncio
async def test_task_expired_lease_is_reclaimable(
    repos: SqliteRepositories,
) -> None:
    await repos.personas.upsert(
        Persona(name="worker", role_description="w", system_prompt="w")
    )
    task_id = await repos.tasks.create(
        TaskSpec(persona_name="worker", goal="g", acceptance="a")
    )

    assert await repos.tasks.claim_lease(task_id, "wakeup-1", duration_sec=0)
    # Sleep just enough so that strftime('now') > lease_until (zero-duration leases
    # are immediately expired in SQLite's second-resolution arithmetic, but make
    # the assertion robust).
    await asyncio.sleep(1.1)

    expired = await repos.tasks.find_expired_leases(limit=5)
    assert any(t.id == task_id for t in expired)
    # New holder can claim.
    assert await repos.tasks.claim_lease(task_id, "wakeup-2", duration_sec=60)


@pytest.mark.asyncio
async def test_mailbox_insert_read_mark(repos: SqliteRepositories) -> None:
    await repos.mailbox.ensure_mailbox("owner")
    await repos.mailbox.ensure_mailbox("dispatcher")

    msg = MailboxMessage(
        recipient="dispatcher",
        external_id="ext-1",
        sender="owner",
        urgency="normal",
        body="hello dispatcher",
    )
    msg_id = await repos.mailbox.insert_message(msg)
    assert msg_id > 0

    # Re-inserting same external_id is a no-op.
    dup_id = await repos.mailbox.insert_message(msg)
    assert dup_id == -1, "duplicate external_id should not produce a new row"

    msgs = await repos.mailbox.read_messages("dispatcher")
    assert len(msgs) == 1
    assert msgs[0].body == "hello dispatcher"

    # Per-message read state: mark + verify it's filtered from read_unread
    await repos.mailbox.mark_messages_read("dispatcher", [msg_id])
    assert await repos.mailbox.count_unread("dispatcher") == 0
    unread = await repos.mailbox.read_unread("dispatcher")
    assert unread == []

    # since_id filter (system-side read_messages still works)
    msgs2 = await repos.mailbox.read_messages("dispatcher", since_id=msg_id)
    assert msgs2 == []


@pytest.mark.asyncio
async def test_blockers_only_returned_for_urgency_blocker(
    repos: SqliteRepositories,
) -> None:
    await repos.mailbox.ensure_mailbox("worker")
    await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="worker", external_id="n", sender="o",
            urgency="normal", body="ok",
        )
    )
    await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="worker", external_id="b", sender="o",
            urgency="blocker", body="STOP",
        )
    )
    blockers = await repos.mailbox.read_blockers("worker")
    assert [m.body for m in blockers] == ["STOP"]


@pytest.mark.asyncio
async def test_outbox_enqueue_idempotent_and_dequeue(
    repos: SqliteRepositories,
) -> None:
    await repos.personas.upsert(
        Persona(name="w", role_description="w", system_prompt="w")
    )
    task_id = await repos.tasks.create(TaskSpec(persona_name="w", goal="g", acceptance="a"))
    wakeup_id = await repos.wakeups.start(task_id, "w")

    row = OutboxRow(
        task_id=task_id,
        wakeup_id=wakeup_id,
        kind="mailbox_send",
        payload={"to": "owner", "body": "hi"},
        external_id="ext-xyz",
    )
    await repos.outbox.enqueue([row])
    await repos.outbox.enqueue([row])  # idempotent

    batch = await repos.outbox.dequeue_batch(limit=10)
    assert len(batch) == 1
    assert batch[0].payload == {"to": "owner", "body": "hi"}

    await repos.outbox.mark_dispatched(batch[0].id or -1)
    batch2 = await repos.outbox.dequeue_batch(limit=10)
    assert batch2 == []


@pytest.mark.asyncio
async def test_local_hot_round_trip(repos: SqliteRepositories) -> None:
    await repos.personas.upsert(
        Persona(name="w", role_description="w", system_prompt="w")
    )
    task_id = await repos.tasks.create(TaskSpec(persona_name="w", goal="g", acceptance="a"))

    await repos.local_hot.put(task_id, "edits", {"files": ["README.md"]})
    val = await repos.local_hot.get(task_id, "edits")
    assert val == {"files": ["README.md"]}

    await repos.local_hot.clear_task(task_id)
    assert await repos.local_hot.get(task_id, "edits") is None


@pytest.mark.asyncio
async def test_wakeup_start_persists_agent_id(
    repos: SqliteRepositories,
) -> None:
    """Without this column being written, the dashboard's per-agent
    "running" detection misses two-stage agent_ids and shows them as
    queued instead of busy. The column has always existed in schema —
    the regression was that WakeupsRepo.start never set it."""
    await repos.personas.upsert(
        Persona(name="worker-maintainer", role_description="w", system_prompt="w")
    )
    await repos.agents.create(
        agent_id="worker-maintainer/refactor-auth",
        persona_name="worker-maintainer",
    )
    task_id = await repos.tasks.create(
        TaskSpec(
            persona_name="worker-maintainer",
            agent_id="worker-maintainer/refactor-auth",
            goal="g", acceptance="a",
        )
    )
    wakeup_id = await repos.wakeups.start(
        task_id, "worker-maintainer",
        agent_id="worker-maintainer/refactor-auth",
    )

    async with repos.conn.execute(
        "SELECT agent_id, persona_name FROM wakeups WHERE id = ?",
        (wakeup_id,),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["agent_id"] == "worker-maintainer/refactor-auth"
    assert row["persona_name"] == "worker-maintainer"


@pytest.mark.asyncio
async def test_wakeup_start_agent_id_optional_for_backcompat(
    repos: SqliteRepositories,
) -> None:
    """Callers that haven't been updated (most tests, bootstrap paths)
    still work — agent_id defaults to NULL and the row is created."""
    await repos.personas.upsert(
        Persona(name="w", role_description="w", system_prompt="w")
    )
    task_id = await repos.tasks.create(
        TaskSpec(persona_name="w", goal="g", acceptance="a")
    )
    wakeup_id = await repos.wakeups.start(task_id, "w")
    async with repos.conn.execute(
        "SELECT agent_id FROM wakeups WHERE id = ?", (wakeup_id,)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["agent_id"] is None


@pytest.mark.asyncio
async def test_wakeup_end_records_metering(repos: SqliteRepositories) -> None:
    await repos.personas.upsert(
        Persona(name="w", role_description="w", system_prompt="w")
    )
    task_id = await repos.tasks.create(TaskSpec(persona_name="w", goal="g", acceptance="a"))
    wakeup_id = await repos.wakeups.start(task_id, "w")
    await repos.wakeups.end(
        wakeup_id,
        end_status="completed",
        metering={
            "token_input": 100,
            "token_output": 50,
            "wall_clock_ms": 1234,
            "tool_call_count": 2,
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
        },
    )
    await repos.wakeups.set_transcript_uri(wakeup_id, "file:///x")

    # Verify by raw SQL — there's no get() method exposed.
    async with repos.conn.execute(
        "SELECT * FROM wakeups WHERE id = ?", (wakeup_id,)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["end_status"] == "completed"
    assert row["token_input"] == 100
    assert row["transcript_uri"] == "file:///x"


@pytest.mark.asyncio
async def test_wakeup_end_records_compaction_metrics(
    repos: SqliteRepositories,
) -> None:
    """compaction_count + compaction_summary_degraded (RB-2) round-trip
    through end(); absent keys default to 0, not NULL."""
    await repos.personas.upsert(
        Persona(name="w", role_description="w", system_prompt="w")
    )
    task_id = await repos.tasks.create(
        TaskSpec(persona_name="w", goal="g", acceptance="a")
    )
    wakeup_id = await repos.wakeups.start(task_id, "w")
    await repos.wakeups.end(
        wakeup_id,
        end_status="completed",
        metering={"compaction_count": 3, "compaction_summary_degraded": 1},
    )
    async with repos.conn.execute(
        "SELECT compaction_count, compaction_summary_degraded "
        "FROM wakeups WHERE id = ?",
        (wakeup_id,),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["compaction_count"] == 3
    assert row["compaction_summary_degraded"] == 1

    # A wakeup ended without the keys defaults the degraded count to 0.
    w2 = await repos.wakeups.start(task_id, "w")
    await repos.wakeups.end(w2, end_status="completed", metering={})
    async with repos.conn.execute(
        "SELECT compaction_summary_degraded FROM wakeups WHERE id = ?", (w2,)
    ) as cur:
        row2 = await cur.fetchone()
    assert row2 is not None
    assert row2["compaction_summary_degraded"] == 0


# ---------------------------------------------------------------------------
# Blob metadata (multimodal) — content-addressed binary registry.
# ---------------------------------------------------------------------------


def _fake_blob(blob_id: str = "a" * 64, *, media_type: str = "image/png",
               size: int = 4096, filename: str | None = "shot.png",
               source: str = "owner") -> Blob:
    return Blob(
        id=blob_id, media_type=media_type, size_bytes=size,
        filename=filename, source=source,
    )


@pytest.mark.asyncio
async def test_blob_upsert_and_get(repos: SqliteRepositories) -> None:
    b = _fake_blob()
    await repos.blobs.upsert(b)
    got = await repos.blobs.get(b.id)
    assert got is not None
    assert got.id == b.id
    assert got.media_type == "image/png"
    assert got.size_bytes == 4096
    assert got.filename == "shot.png"
    assert got.source == "owner"
    assert got.created_at is not None  # set by DB default


@pytest.mark.asyncio
async def test_blob_upsert_idempotent_on_id_conflict(
    repos: SqliteRepositories,
) -> None:
    """Re-uploading identical bytes resolves to the same sha256, so
    upsert must be a no-op (NOT clobber the existing row's metadata
    with the new one's filename/source — the first writer wins)."""
    first = _fake_blob(filename="original.png", source="owner")
    await repos.blobs.upsert(first)
    second = _fake_blob(filename="renamed.png", source="dispatcher/1")
    await repos.blobs.upsert(second)
    got = await repos.blobs.get(first.id)
    assert got is not None
    assert got.filename == "original.png"
    assert got.source == "owner"


@pytest.mark.asyncio
async def test_blob_exists_and_list_ids(repos: SqliteRepositories) -> None:
    b1 = _fake_blob(blob_id="a" * 64, filename="one.png")
    b2 = _fake_blob(blob_id="b" * 64, filename="two.png")
    await repos.blobs.upsert(b1)
    await repos.blobs.upsert(b2)
    assert await repos.blobs.exists(b1.id) is True
    assert await repos.blobs.exists("c" * 64) is False

    # list_ids preserves request order and silently skips unknowns.
    out = await repos.blobs.list_ids([b2.id, "missing", b1.id])
    assert [b.id for b in out] == [b2.id, b1.id]


@pytest.mark.asyncio
async def test_blob_list_ids_empty_returns_empty(
    repos: SqliteRepositories,
) -> None:
    assert await repos.blobs.list_ids([]) == []


@pytest.mark.asyncio
async def test_mailbox_message_roundtrips_attachments(
    repos: SqliteRepositories,
) -> None:
    """The JSON `attachments` column carries a list of blob ids;
    round-trip through insert_message → get_message must preserve
    order and identity."""
    await repos.personas.upsert(
        Persona(name="dispatcher", role_description="d", system_prompt="d")
    )
    await repos.mailbox.ensure_mailbox("worker-maintainer/x")
    blob_ids = ["a" * 64, "b" * 64]
    mid = await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="worker-maintainer/x",
            external_id="msg-with-attach",
            sender="owner",
            urgency="normal",
            body="look at this",
            attachments=blob_ids,
        )
    )
    got = await repos.mailbox.get_message(mid)
    assert got is not None
    assert got.attachments == blob_ids


@pytest.mark.asyncio
async def test_mailbox_message_without_attachments_is_none(
    repos: SqliteRepositories,
) -> None:
    """Existing mail with no attachments must still load — the column
    is NULLable and pre-0002 rows never set it."""
    await repos.personas.upsert(
        Persona(name="d", role_description="d", system_prompt="d")
    )
    await repos.mailbox.ensure_mailbox("owner")
    mid = await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="owner", external_id="plain",
            sender="d", urgency="normal", body="hi",
        )
    )
    got = await repos.mailbox.get_message(mid)
    assert got is not None
    assert got.attachments is None


@pytest.mark.asyncio
async def test_mailbox_message_roundtrips_delivered_at(
    repos: SqliteRepositories,
) -> None:
    """Regression: `_row_to_msg` used to forget to read `delivered_at`,
    so every MailboxMessage came back with `delivered_at=None`. The
    dashboard activity sort then keyed off `m.delivered_at.isoformat()
    or ""` and slotted every mail event ABOVE everything else (empty
    string lex-sorts to start). Pin the round-trip so this can't
    silently regress."""
    import datetime as _dt

    await repos.personas.upsert(
        Persona(name="d", role_description="d", system_prompt="d")
    )
    await repos.mailbox.ensure_mailbox("owner")
    before = _dt.datetime.now(_dt.UTC)
    mid = await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="owner", external_id="ts-check",
            sender="d", urgency="normal", body="hi",
        )
    )
    after = _dt.datetime.now(_dt.UTC)

    got = await repos.mailbox.get_message(mid)
    assert got is not None
    assert got.delivered_at is not None, (
        "delivered_at must be populated on read-back; if this fails "
        "the dashboard timeline ordering will be wrong"
    )
    # Pydantic parses the sqlite ISO string into an aware datetime.
    assert isinstance(got.delivered_at, _dt.datetime)
    # And the value is sensible — within the window we inserted in.
    # A few seconds of slack on each side covers clock skew and the
    # sqlite vs python time gap.
    assert before - _dt.timedelta(seconds=5) <= got.delivered_at
    assert got.delivered_at <= after + _dt.timedelta(seconds=5)
