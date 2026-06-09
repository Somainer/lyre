"""Tests for individual Lyre tools (called directly, no LLM)."""

from __future__ import annotations

import pytest

from lyre.persistence.models import Persona, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.tools import ToolContext, ToolError
from lyre.runtime.tools.builtin import build_default_registry
from lyre.runtime.tools.mailbox import MAILBOX_READ, MAILBOX_SEND, MARK_READ
from lyre.runtime.tools.progress import REPORT_PROGRESS, REPORT_SIDE_EFFECT
from lyre.runtime.tools.tasks import DISPATCH_TASK, QUERY_TASK_STATUS


@pytest.fixture
async def ctx(repos: SqliteRepositories) -> ToolContext:
    """Seed personas + matching default agents + task + wakeup.

    After A3 mailbox/dispatch validation goes against the agents table,
    so we seed one agent per persona with id == persona name (matches the
    bootstrap behaviour for owner/dispatcher).
    """
    # owner/dispatcher are kind="singleton" (mirrors the shipped personas):
    # create_agent must refuse to spawn duplicates of those roles.
    persona_kinds: dict[str, str] = {
        "owner": "singleton",
        "dispatcher": "singleton",
        "worker": "spawn_only",
    }
    for name, kind in persona_kinds.items():
        await repos.personas.upsert(
            Persona(
                name=name, kind=kind,  # type: ignore[arg-type]
                role_description=name, system_prompt=name,
            )
        )
        await repos.agents.create(agent_id=name, persona_name=name)
    task_id = await repos.tasks.create(
        TaskSpec(agent_id="worker", goal="g", acceptance="a")
    )
    wakeup_id = await repos.wakeups.start(task_id, "worker")
    await repos.tasks.claim_lease(task_id, wakeup_id, duration_sec=600)
    return ToolContext(
        repos=repos,
        task_id=task_id,
        wakeup_id=wakeup_id,
        persona_name="worker",
        agent_id="worker",
    )


@pytest.mark.asyncio
async def test_mailbox_send_writes_outbox(ctx: ToolContext) -> None:
    result = await MAILBOX_SEND.handler(
        ctx,
        {
            "to": "owner",
            "body": "PR ready",
            "urgency": "normal",
            "_tool_use_id": "tu_abc",
        },
    )
    assert result["status"] == "queued"
    batch = await ctx.repos.outbox.dequeue_batch(limit=10)
    assert len(batch) == 1
    assert batch[0].kind == "mailbox_send"
    assert batch[0].payload["recipient"] == "owner"
    # external_id now includes recipient suffix (per-delivery idempotency
    # so retries of a broadcast don't double-deliver to others).
    assert batch[0].external_id == f"{ctx.wakeup_id}:tu_abc:owner"


@pytest.mark.asyncio
async def test_mailbox_send_idempotent_on_retry(ctx: ToolContext) -> None:
    args = {"to": "owner", "body": "x", "_tool_use_id": "tu_same"}
    await MAILBOX_SEND.handler(ctx, args)
    await MAILBOX_SEND.handler(ctx, args)  # retry-same external_id
    batch = await ctx.repos.outbox.dequeue_batch(limit=10)
    assert len(batch) == 1


@pytest.mark.asyncio
async def test_mailbox_send_rejects_unknown_recipient(ctx: ToolContext) -> None:
    """Sister to mailbox_read's hallucination check — sends to an invented
    name must error so the model retries on the next turn rather than
    quietly dropping mail nobody will read."""
    with pytest.raises(ToolError, match="unknown recipient"):
        await MAILBOX_SEND.handler(
            ctx,
            {
                "to": "dispatcher-scheduler",
                "body": "x",
                "_tool_use_id": "tu_hallucinated",
            },
        )
    # Mixed valid + invalid → still rejects (no partial delivery).
    with pytest.raises(ToolError, match="unknown recipient"):
        await MAILBOX_SEND.handler(
            ctx,
            {
                "to": ["owner", "ghost-persona"],
                "body": "x",
                "_tool_use_id": "tu_mixed",
            },
        )


@pytest.mark.asyncio
async def test_mailbox_send_to_self_immediate_rejected(
    ctx: ToolContext,
) -> None:
    """Immediate self-send is still blocked — auto-wake-on-mail would
    fire instantly and "kick myself awake" is a trivial loop. Error
    message points at the scheduled-mail workaround."""
    with pytest.raises(ToolError, match="immediate mail to self"):
        await MAILBOX_SEND.handler(
            ctx,
            {
                "to": ctx.self_mailbox,
                "body": "check on X",
                "_tool_use_id": "tu_self_now",
            },
        )


@pytest.mark.asyncio
async def test_mailbox_send_to_self_scheduled_allowed(
    ctx: ToolContext,
) -> None:
    """Scheduled self-send IS allowed — the delivery delay breaks the
    instant-loop class of bugs, and "remind future-me about X" is a
    legit long-running-agent pattern that the identity preamble
    already documents."""
    out = await MAILBOX_SEND.handler(
        ctx,
        {
            "to": ctx.self_mailbox,
            "body": "remind me to check PR #142",
            "title": "self-reminder: PR #142",
            "deliver_in": "1h",
            "_tool_use_id": "tu_self_later",
        },
    )
    assert out["status"] == "scheduled"
    assert out["recipients"] == [ctx.self_mailbox]
    assert len(out["scheduled_ids"]) == 1
    # Row landed in scheduled_mail with the agent as both sender and
    # recipient. The scheduler's Phase -1 will deliver when due.
    sid = out["scheduled_ids"][0]
    spec = await ctx.repos.scheduled_mail.get(sid)
    assert spec is not None
    assert spec.recipient == ctx.self_mailbox
    assert spec.sender == ctx.self_mailbox


@pytest.mark.asyncio
async def test_mailbox_send_to_self_recurring_allowed(
    ctx: ToolContext,
) -> None:
    """Recurring self-send (the standing-commission pattern: 'every
    weekday morning, scan the queue and remind me') is also allowed."""
    out = await MAILBOX_SEND.handler(
        ctx,
        {
            "to": ctx.self_mailbox,
            "body": "weekday morning scan",
            "title": "standing: morning scan",
            "recur_cron": "0 9 * * 1-5",
            "_tool_use_id": "tu_self_recur",
        },
    )
    assert out["status"] == "scheduled"
    assert out["recur_kind"] == "cron"
    assert out["recur_value"] == "0 9 * * 1-5"


@pytest.mark.asyncio
async def test_mailbox_send_mixed_self_and_other_immediate_rejected(
    ctx: ToolContext,
) -> None:
    """A broadcast that includes self alongside others is still rejected
    when immediate — preserve the "no immediate self-touch" invariant
    even if the model thinks it can sneak self in via a list."""
    with pytest.raises(ToolError, match="immediate mail to self"):
        await MAILBOX_SEND.handler(
            ctx,
            {
                "to": [ctx.self_mailbox, "owner"],
                "body": "status",
                "_tool_use_id": "tu_mixed_self",
            },
        )


@pytest.mark.asyncio
async def test_mailbox_send_validates_args(ctx: ToolContext) -> None:
    with pytest.raises(ToolError):
        await MAILBOX_SEND.handler(ctx, {"body": "x", "_tool_use_id": "t1"})
    with pytest.raises(ToolError):
        await MAILBOX_SEND.handler(ctx, {"to": "o", "_tool_use_id": "t1"})
    with pytest.raises(ToolError):
        await MAILBOX_SEND.handler(
            ctx, {"to": "o", "body": "x", "urgency": "panic", "_tool_use_id": "t1"}
        )


@pytest.mark.asyncio
async def test_mailbox_read_rejects_hallucinated_recipient(
    ctx: ToolContext,
) -> None:
    """Regression: DeepSeek invented `recipient='dispatcher-scheduler'`, and
    because we auto-ensured the mailbox, it read 0 messages and silently
    ended the turn. The tool must error so the model is forced to recover
    instead of swallowing the bug."""
    with pytest.raises(ToolError, match="unknown recipient"):
        await MAILBOX_READ.handler(
            ctx, {"recipient": "dispatcher-scheduler"}
        )


@pytest.mark.asyncio
async def test_mailbox_read_allows_self_and_owner(ctx: ToolContext) -> None:
    # Self (default) — fine even if no messages
    out = await MAILBOX_READ.handler(ctx, {})
    assert out["recipient"] == ctx.persona_name
    # Owner — also fine
    out_owner = await MAILBOX_READ.handler(ctx, {"recipient": "owner"})
    assert out_owner["recipient"] == "owner"
    # Known persona — fine
    out_dispatcher = await MAILBOX_READ.handler(ctx, {"recipient": "dispatcher"})
    assert out_dispatcher["recipient"] == "dispatcher"


@pytest.mark.asyncio
async def test_mailbox_read_auto_marks_and_listing_shape(
    ctx: ToolContext,
) -> None:
    """mailbox_read returns listing-only (title + body_chars, no body)
    and auto-marks the returned rows as read."""
    from lyre.persistence.models import MailboxMessage

    # Reading own mailbox; insert via system path (no auto-mark by
    # insert_message itself) so we control state.
    await ctx.repos.mailbox.insert_message(
        MailboxMessage(
            recipient=ctx.self_mailbox,
            external_id="e1",
            sender="owner",
            urgency="normal",
            title="say hi",
            body="hi there, longer body content here.",
        )
    )
    result = await MAILBOX_READ.handler(ctx, {})
    assert len(result["messages"]) == 1
    msg = result["messages"][0]
    # Listing fields present
    assert msg["title"] == "say hi"
    assert msg["body_chars"] == len("hi there, longer body content here.")
    # Body NOT in listing
    assert "body" not in msg
    # Auto-mark side effect
    assert result["auto_marked_read"] is True
    # Re-read → empty (was just marked)
    again = await MAILBOX_READ.handler(ctx, {})
    assert again["messages"] == []
    assert again["unread_remaining"] == 0


@pytest.mark.asyncio
async def test_mailbox_send_derives_title_from_body_first_line(
    ctx: ToolContext,
) -> None:
    """No LLM call; title = first non-empty line of body, truncated to 140."""
    res = await MAILBOX_SEND.handler(
        ctx,
        {
            "to": "owner",
            "body": "First line is the subject.\n\nLonger body follows.",
            "_tool_use_id": "t1",
        },
    )
    assert res["status"] == "queued"
    # Process the outbox so the mailbox row materializes.
    batch = await ctx.repos.outbox.dequeue_batch(limit=10)
    assert len(batch) == 1
    # Insert into mailbox the way OutboxDispatcher would (uses payload).
    from lyre.persistence.models import MailboxMessage
    payload = batch[0].payload
    msg = MailboxMessage(**{
        k: payload.get(k)
        for k in (
            "recipient", "external_id", "sender", "urgency", "title",
            "body", "task_id", "parent_msg_id", "broadcast_id",
            "recipients_all", "metadata",
        )
    })
    msg_id = await ctx.repos.mailbox.insert_message(msg)
    stored = await ctx.repos.mailbox.get_message(msg_id)
    assert stored is not None
    assert stored.title == "First line is the subject."


@pytest.mark.asyncio
async def test_mailbox_send_explicit_title_is_used(
    ctx: ToolContext,
) -> None:
    await MAILBOX_SEND.handler(
        ctx,
        {
            "to": "owner",
            "title": "Fix typo in README",
            "body": "Some longer explanation",
            "_tool_use_id": "t1",
        },
    )
    batch = await ctx.repos.outbox.dequeue_batch(limit=10)
    payload = batch[0].payload
    assert payload["title"] == "Fix typo in README"


@pytest.mark.asyncio
async def test_mailbox_send_rejects_too_long_title(
    ctx: ToolContext,
) -> None:
    with pytest.raises(ToolError, match="title exceeds 140"):
        await MAILBOX_SEND.handler(
            ctx,
            {
                "to": "owner",
                "title": "x" * 200,
                "body": "...",
                "_tool_use_id": "t1",
            },
        )


@pytest.mark.asyncio
async def test_mailbox_read_blocker_first_then_id_asc(
    ctx: ToolContext,
) -> None:
    """Listing returns blocker → high → normal → low; id-asc within bucket."""
    from lyre.persistence.models import MailboxMessage
    # Insert in id-asc but mixed urgencies — read order should reorder.
    for ext, urg, body in [
        ("a", "low", "low1"),
        ("b", "blocker", "block1"),
        ("c", "high", "high1"),
        ("d", "blocker", "block2"),
        ("e", "normal", "norm1"),
    ]:
        await ctx.repos.mailbox.insert_message(
            MailboxMessage(
                recipient=ctx.self_mailbox, external_id=ext,
                sender="owner", urgency=urg, body=body,
            )
        )
    out = await MAILBOX_READ.handler(ctx, {})
    urgencies = [m["urgency"] for m in out["messages"]]
    # blocker (2) → high (1) → normal (1) → low (1)
    assert urgencies == ["blocker", "blocker", "high", "normal", "low"]


@pytest.mark.asyncio
async def test_mailbox_read_include_read_does_not_mark(
    ctx: ToolContext,
) -> None:
    from lyre.persistence.models import MailboxMessage
    msg_id = await ctx.repos.mailbox.insert_message(
        MailboxMessage(
            recipient=ctx.self_mailbox, external_id="archive-1",
            sender="owner", urgency="normal", body="hi",
        )
    )
    # Archive view: returns everything but does NOT mark.
    out = await MAILBOX_READ.handler(ctx, {"include_read": True})
    assert len(out["messages"]) == 1
    assert out["auto_marked_read"] is False
    # Re-read with default → still unread → returns the same row.
    out2 = await MAILBOX_READ.handler(ctx, {})
    assert any(m["id"] == msg_id for m in out2["messages"])


@pytest.mark.asyncio
async def test_initial_user_message_pushes_recent_sends(
    ctx: ToolContext,
) -> None:
    """T1 reverses the old trust-the-model call: recent sends ARE pushed into
    the wakeup. RCA 019e8d7d showed a stateless model won't reliably pull them
    via mailbox_read(box="sent"), so it forgets what it already promised.
    """
    from lyre.persistence.models import MailboxMessage
    from lyre.runtime.context import assemble_initial_user_message
    await ctx.repos.mailbox.insert_message(
        MailboxMessage(
            recipient="owner",
            external_id="prior-1",
            sender=ctx.self_mailbox,
            urgency="high",
            title="I'll investigate /pi",
            body="I'll go look at /pi and report back.",
        )
    )
    task = await ctx.repos.tasks.get(ctx.task_id)
    assert task is not None
    init_msg = await assemble_initial_user_message(
        task,
        tasks_repo=ctx.repos.tasks,
        mailbox_repo=ctx.repos.mailbox,
        agent_id=ctx.self_mailbox,
    )
    text = init_msg.content[0].text
    # The prior send now appears so the agent doesn't re-send / forget it.
    assert "I'll investigate /pi" in text
    assert "你最近" in text


@pytest.mark.asyncio
async def test_mailbox_read_box_sent_returns_self_sends_newest_first(
    ctx: ToolContext,
) -> None:
    """`mailbox_read(box="sent")` lets the agent self-recall outgoing
    mail (newest-first, no auto-mark, no body in listing)."""
    from lyre.persistence.models import MailboxMessage
    for i in range(3):
        await ctx.repos.mailbox.insert_message(
            MailboxMessage(
                recipient="owner", external_id=f"s{i}",
                sender=ctx.self_mailbox, urgency="normal",
                title=f"send #{i}", body=f"body for send {i}",
            )
        )
    out = await MAILBOX_READ.handler(ctx, {"box": "sent"})
    assert out["box"] == "sent"
    assert out["sender"] == ctx.self_mailbox
    assert out["auto_marked_read"] is False
    # Newest-first: send #2 before #1 before #0.
    titles = [m["title"] for m in out["messages"]]
    assert titles[:3] == ["send #2", "send #1", "send #0"]
    # Listing only: body itself is not present, but body_chars is.
    for m in out["messages"]:
        assert "body" not in m
        assert m["body_chars"] > 0


@pytest.mark.asyncio
async def test_mailbox_read_box_sent_filters_by_recipient(
    ctx: ToolContext,
) -> None:
    from lyre.persistence.models import MailboxMessage
    await ctx.repos.mailbox.insert_message(
        MailboxMessage(
            recipient="owner", external_id="s-a",
            sender=ctx.self_mailbox, urgency="normal",
            title="to owner", body="b",
        )
    )
    await ctx.repos.mailbox.insert_message(
        MailboxMessage(
            recipient="dispatcher", external_id="s-b",
            sender=ctx.self_mailbox, urgency="normal",
            title="to dispatcher", body="b",
        )
    )
    out = await MAILBOX_READ.handler(
        ctx, {"box": "sent", "recipient": "owner"}
    )
    assert out["recipient_filter"] == "owner"
    titles = [m["title"] for m in out["messages"]]
    assert titles == ["to owner"]


@pytest.mark.asyncio
async def test_mark_read_handler(ctx: ToolContext) -> None:
    from lyre.persistence.models import MailboxMessage
    msg_id = await ctx.repos.mailbox.insert_message(
        MailboxMessage(
            recipient=ctx.self_mailbox, external_id="e2",
            sender="owner", urgency="low", body="fyi",
        )
    )
    await MARK_READ.handler(ctx, {"msg_id": msg_id})
    # After mark_read, mailbox_read returns nothing (it was the only msg).
    out = await MAILBOX_READ.handler(ctx, {})
    assert out["messages"] == []


@pytest.mark.asyncio
async def test_report_progress_updates_checkpoint(ctx: ToolContext) -> None:
    await REPORT_PROGRESS.handler(
        ctx,
        {"checkpoint": {"phase": "edit", "files": ["README.md"]}},
    )
    t = await ctx.repos.tasks.get(ctx.task_id)
    assert t is not None
    assert t.checkpoint == {"phase": "edit", "files": ["README.md"]}


@pytest.mark.asyncio
async def test_report_side_effect_writes_outbox(ctx: ToolContext) -> None:
    result = await REPORT_SIDE_EFFECT.handler(
        ctx,
        {
            "kind": "opened_pr",
            "payload": {"url": "https://github.com/x/y/pull/1"},
            "_tool_use_id": "tu_se",
        },
    )
    assert result["kind"] == "opened_pr"
    batch = await ctx.repos.outbox.dequeue_batch(limit=10)
    assert len(batch) == 1
    assert batch[0].kind == "tier1_notification"
    assert batch[0].payload["details"]["url"] == "https://github.com/x/y/pull/1"


@pytest.mark.asyncio
async def test_dispatch_task_creates_child_task(ctx: ToolContext) -> None:
    result = await DISPATCH_TASK.handler(
        ctx,
        {
            "persona": "worker",
            "goal": "edit README",
            "acceptance": "PR is open",
        },
    )
    new_id = result["task_id"]
    child = await ctx.repos.tasks.get(new_id)
    assert child is not None
    assert child.parent_task_id == ctx.task_id
    assert child.persona_name == "worker"


@pytest.mark.asyncio
async def test_dispatch_task_rejects_unknown_persona(ctx: ToolContext) -> None:
    with pytest.raises(ToolError):
        await DISPATCH_TASK.handler(
            ctx,
            {"persona": "ghost", "goal": "x", "acceptance": "y"},
        )


@pytest.mark.asyncio
async def test_dispatch_task_max_turns_rides_tier_overrides(ctx: ToolContext) -> None:
    """O3a: an explicit per-task turn budget lands in the (until now unused)
    tier_overrides bag, where the scheduler resolves it at the AgentLoop
    build site."""
    result = await DISPATCH_TASK.handler(
        ctx,
        {
            "persona": "worker",
            "goal": "deep research",
            "acceptance": "spec written",
            "max_turns": 40,
        },
    )
    child = await ctx.repos.tasks.get(result["task_id"])
    assert child is not None
    assert child.tier_overrides == {"max_turns": 40}


@pytest.mark.asyncio
async def test_dispatch_task_without_max_turns_leaves_tier_overrides_unset(
    ctx: ToolContext,
) -> None:
    result = await DISPATCH_TASK.handler(
        ctx,
        {"persona": "worker", "goal": "g", "acceptance": "a"},
    )
    child = await ctx.repos.tasks.get(result["task_id"])
    assert child is not None
    assert child.tier_overrides is None


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [0, -1, "40", 3.5, True])
async def test_dispatch_task_rejects_non_positive_int_max_turns(
    ctx: ToolContext, bad: object,
) -> None:
    """No silent drop: a non-positive / non-int max_turns (incl. bool, an int
    subclass in Python) is a loud ToolError, not an ignored over-budget intent."""
    with pytest.raises(ToolError):
        await DISPATCH_TASK.handler(
            ctx,
            {"persona": "worker", "goal": "g", "acceptance": "a", "max_turns": bad},
        )


@pytest.mark.asyncio
async def test_query_task_status_returns_state(ctx: ToolContext) -> None:
    result = await QUERY_TASK_STATUS.handler(ctx, {"task_id": ctx.task_id})
    assert result["id"] == ctx.task_id
    assert result["status"] == "in_progress"


def test_builtin_registry_contains_expected_tools() -> None:
    reg = build_default_registry()
    names = set(reg.all_names())
    assert {
        "mailbox_send",
        "mailbox_read",
        "mark_read",
        "report_progress",
        "report_side_effect",
        "dispatch_task",
        "query_task_status",
        "read_memory",
        "list_personas",
        "list_agents",
        "list_models",
        "list_tasks",
        "create_agent",
        "archive_agent",
    } <= names


def test_registry_specs_for_filters_by_allowlist() -> None:
    reg = build_default_registry()
    specs = reg.specs_for(["mailbox_send", "ghost", "report_progress"])
    names = {s.name for s in specs}
    assert names == {"mailbox_send", "report_progress"}


# ---------------------------------------------------------------------------
# Introspection tools — read_memory / list_agents / list_tasks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_memory_reads_sandboxed_file(
    ctx: ToolContext, tmp_path
) -> None:
    from lyre.runtime.tools.introspect import READ_MEMORY

    mem = tmp_path / "memory"
    (mem / "personas").mkdir(parents=True)
    target = mem / "personas" / "owner.md"
    target.write_text("hello body", encoding="utf-8")
    ctx.extras["memory_root"] = str(mem)

    out = await READ_MEMORY.handler(ctx, {"rel_path": "personas/owner.md"})
    assert out["body"] == "hello body"
    assert out["rel_path"] == "personas/owner.md"
    assert "truncated" not in out


@pytest.mark.asyncio
async def test_read_memory_rejects_path_traversal(
    ctx: ToolContext, tmp_path
) -> None:
    from lyre.runtime.tools.introspect import READ_MEMORY

    mem = tmp_path / "memory"
    mem.mkdir()
    # Place a file outside memory_root that traversal would target.
    outside = tmp_path / "secret.txt"
    outside.write_text("nope", encoding="utf-8")
    ctx.extras["memory_root"] = str(mem)

    with pytest.raises(ToolError, match="rel_path"):
        await READ_MEMORY.handler(ctx, {"rel_path": "../secret.txt"})
    with pytest.raises(ToolError, match="rel_path"):
        await READ_MEMORY.handler(ctx, {"rel_path": "/etc/passwd"})


@pytest.mark.asyncio
async def test_read_memory_truncates_giant_file(
    ctx: ToolContext, tmp_path
) -> None:
    from lyre.runtime.tools.introspect import _MAX_BYTES, READ_MEMORY

    mem = tmp_path / "memory"
    mem.mkdir()
    big = mem / "huge.md"
    big.write_text("x" * (_MAX_BYTES + 100), encoding="utf-8")
    ctx.extras["memory_root"] = str(mem)

    out = await READ_MEMORY.handler(ctx, {"rel_path": "huge.md"})
    assert out["truncated"] is True
    assert len(out["body"]) == _MAX_BYTES


@pytest.mark.asyncio
async def test_read_memory_errors_without_memory_root(ctx: ToolContext) -> None:
    from lyre.runtime.tools.introspect import READ_MEMORY

    # ctx fixture leaves extras empty; no memory_root => fail with clear error
    ctx.extras.pop("memory_root", None)
    with pytest.raises(ToolError, match="memory_root not configured"):
        await READ_MEMORY.handler(ctx, {"rel_path": "anything.md"})


@pytest.mark.asyncio
async def test_list_personas_returns_active_role_definitions(
    ctx: ToolContext,
) -> None:
    from lyre.runtime.tools.introspect import LIST_PERSONAS

    out = await LIST_PERSONAS.handler(ctx, {})
    names = {p["name"] for p in out["personas"]}
    # fixture seeds worker, dispatcher, owner
    assert {"worker", "dispatcher", "owner"} <= names
    assert out["count"] == len(out["personas"])
    # The note must make it explicit that personas ≠ live agents
    assert "agent" in out["note"].lower()


@pytest.mark.asyncio
async def test_list_tasks_filters_by_persona_and_status(
    ctx: ToolContext,
) -> None:
    from lyre.runtime.tools.introspect import LIST_TASKS

    # ctx fixture already created one task for "worker" in_progress.
    # Seed a second task for "dispatcher", pending.
    await ctx.repos.tasks.create(
        TaskSpec(persona_name="dispatcher", goal="lead", acceptance="ok")
    )

    out_all = await LIST_TASKS.handler(ctx, {})
    assert out_all["count"] >= 2

    out_dispatcher = await LIST_TASKS.handler(ctx, {"persona": "dispatcher"})
    assert out_dispatcher["count"] == 1
    assert out_dispatcher["tasks"][0]["persona"] == "dispatcher"

    out_progress = await LIST_TASKS.handler(ctx, {"status": "in_progress"})
    assert all(t["status"] == "in_progress" for t in out_progress["tasks"])
    # the worker task from the fixture should be there
    assert any(t["persona"] == "worker" for t in out_progress["tasks"])


@pytest.mark.asyncio
async def test_list_tasks_validates_status(ctx: ToolContext) -> None:
    from lyre.runtime.tools.introspect import LIST_TASKS

    with pytest.raises(ToolError, match="status must be one of"):
        await LIST_TASKS.handler(ctx, {"status": "bogus"})
    with pytest.raises(ToolError, match="limit"):
        await LIST_TASKS.handler(ctx, {"limit": 0})


# ---------------------------------------------------------------------------
# Agent CRUD tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_agent_auto_names(ctx: ToolContext) -> None:
    """Auto-name uses `<persona>/<n>` for the smallest unused n.

    Models are expected to supply a meaningful `name` in practice; the
    numeric fallback only fires in degenerate cases (model didn't
    bother / migrated from older spec). The `/n` form keeps the
    persona/name grammar intact."""
    from lyre.runtime.tools.introspect import CREATE_AGENT

    out = await CREATE_AGENT.handler(ctx, {"persona": "worker"})
    assert out["agent_id"] == "worker/1"
    out2 = await CREATE_AGENT.handler(ctx, {"persona": "worker"})
    assert out2["agent_id"] == "worker/2"


@pytest.mark.asyncio
async def test_create_agent_explicit_name(ctx: ToolContext) -> None:
    """Explicit name composes into `<persona>/<name>` — that's now the
    canonical addressing form for spawned agents (bootstrap stays bare)."""
    from lyre.runtime.tools.introspect import CREATE_AGENT

    out = await CREATE_AGENT.handler(
        ctx, {"persona": "worker", "name": "alice", "description": "test"}
    )
    assert out["agent_id"] == "worker/alice"
    # Duplicate explicit name fails
    with pytest.raises(ToolError, match="already exists"):
        await CREATE_AGENT.handler(ctx, {"persona": "worker", "name": "alice"})


@pytest.mark.asyncio
async def test_create_agent_validates_inputs(ctx: ToolContext) -> None:
    from lyre.runtime.tools.introspect import CREATE_AGENT

    with pytest.raises(ToolError, match="persona"):
        await CREATE_AGENT.handler(ctx, {"persona": "ghost-persona"})
    with pytest.raises(ToolError, match="invalid agent name"):
        await CREATE_AGENT.handler(
            ctx, {"persona": "worker", "name": "Has Spaces"}
        )
    # Singleton personas can't be respawned with this tool.
    with pytest.raises(ToolError, match="singleton role"):
        await CREATE_AGENT.handler(ctx, {"persona": "dispatcher", "name": "x"})


@pytest.mark.asyncio
async def test_create_agent_records_parent_from_caller(
    ctx: ToolContext,
) -> None:
    """The new agent's `parent_agent_id` should be the caller's
    self_mailbox — establishing lineage automatically without the model
    having to remember to pass it."""
    from lyre.runtime.tools.introspect import CREATE_AGENT

    out = await CREATE_AGENT.handler(
        ctx, {"persona": "worker", "name": "refactor-auth"}
    )
    assert out["parent_agent_id"] == ctx.self_mailbox

    row = await ctx.repos.agents.get("worker/refactor-auth")
    assert row is not None
    assert row.parent_agent_id == ctx.self_mailbox


@pytest.mark.asyncio
async def test_create_agent_pre_creates_notes_file(
    ctx: ToolContext, tmp_path: pytest.TempPathFactory,
) -> None:
    """The Codex-style 'pre-create the path, agent self-discovers'
    pattern: every create_agent (with memory_root in ctx.extras) drops
    a markdown notes file at facts/agent-<id>-notes.md so the agent's
    first call to read_memory hits a real file instead of a 404."""
    from pathlib import Path

    from lyre.runtime.tools.introspect import CREATE_AGENT

    memory_root = Path(str(tmp_path)) / "memory"
    memory_root.mkdir()
    ctx.extras["memory_root"] = str(memory_root)

    out = await CREATE_AGENT.handler(
        ctx, {"persona": "worker", "name": "scout"}
    )
    notes_path = Path(out["notes_file"])
    assert notes_path.exists()
    # Notes path mirrors the agent_id with `/` flattened to `-` so the
    # filename remains a single segment under facts/.
    assert "agent-worker" in notes_path.name and "scout" in notes_path.name
    text = notes_path.read_text(encoding="utf-8")
    # frontmatter + a "this is YOUR notebook" preamble
    assert "type: agent_notes" in text
    assert "agent_id: worker/scout" in text
    assert "private notebook" in text


@pytest.mark.asyncio
async def test_seed_default_agents_pre_creates_notes_files(
    tmp_path,
) -> None:
    """`lyre onboard` path: owner + dispatcher notes files materialize
    next to the memory skeleton on first bootstrap. Seeding walks the
    personas table — singleton + seeded kinds get bootstrap agents
    (id = display_name fallback to name), spawn_only is skipped."""
    from pathlib import Path

    from lyre.persistence.db import init_db
    from lyre.persistence.models import Persona
    from lyre.persistence.sqlite_impl import SqliteRepositories
    from lyre.personas.seed import seed_default_agents
    memory_root = Path(str(tmp_path)) / "memory"
    memory_root.mkdir()
    conn = await init_db(":memory:")
    try:
        repos = SqliteRepositories(conn)
        persona_setup = [
            ("owner",     "singleton", "owner"),
            ("dispatcher","singleton", "dispatcher"),
            ("analyst",   "seeded",    "analyst-1"),
            ("reviewer",  "seeded",    "reviewer-1"),
            ("worker",    "spawn_only", None),
        ]
        for name, kind, display in persona_setup:
            await repos.personas.upsert(
                Persona(
                    name=name, kind=kind, display_name=display,  # type: ignore[arg-type]
                    role_description=name, system_prompt=name,
                )
            )
        created = await seed_default_agents(
            repos.personas, repos.agents, memory_root=memory_root,
        )
        assert {"owner", "dispatcher", "analyst-1", "reviewer-1"} == set(created)
        for aid in ("owner", "dispatcher", "analyst-1", "reviewer-1"):
            p = memory_root / "facts" / f"agent-{aid}-notes.md"
            assert p.exists(), f"missing notes file for {aid}"
            assert f"agent_id: {aid}" in p.read_text(encoding="utf-8")
        # Idempotent on re-run — notes content not clobbered.
        custom = memory_root / "facts" / "agent-owner-notes.md"
        custom.write_text("custom content", encoding="utf-8")
        await seed_default_agents(
            repos.personas, repos.agents, memory_root=memory_root,
        )
        assert custom.read_text(encoding="utf-8") == "custom content"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_archive_agent_refuses_bootstrap(ctx: ToolContext) -> None:
    from lyre.runtime.tools.introspect import ARCHIVE_AGENT, CREATE_AGENT

    await CREATE_AGENT.handler(
        ctx, {"persona": "worker", "name": "scratch"}
    )
    out = await ARCHIVE_AGENT.handler(ctx, {"agent_id": "worker/scratch"})
    assert out["archived"] is True

    with pytest.raises(ToolError, match="bootstrap-seeded agent"):
        await ARCHIVE_AGENT.handler(ctx, {"agent_id": "dispatcher"})
    with pytest.raises(ToolError, match="not found"):
        await ARCHIVE_AGENT.handler(ctx, {"agent_id": "no-such-agent"})


@pytest.mark.asyncio
async def test_list_agents_excludes_archived_by_default(
    ctx: ToolContext,
) -> None:
    from lyre.runtime.tools.introspect import (
        ARCHIVE_AGENT,
        CREATE_AGENT,
        LIST_AGENTS,
    )

    await CREATE_AGENT.handler(ctx, {"persona": "worker", "name": "live"})
    await CREATE_AGENT.handler(ctx, {"persona": "worker", "name": "dead"})
    await ARCHIVE_AGENT.handler(ctx, {"agent_id": "worker/dead"})

    out = await LIST_AGENTS.handler(ctx, {})
    ids = {a["id"] for a in out["agents"]}
    assert "worker/live" in ids
    assert "worker/dead" not in ids

    out_all = await LIST_AGENTS.handler(ctx, {"include_archived": True})
    ids_all = {a["id"] for a in out_all["agents"]}
    assert "worker/dead" in ids_all
    assert "worker/live" in ids_all
    # Enrichment fields land in every entry.
    sample = next(a for a in out_all["agents"] if a["id"] == "worker/live")
    assert "occupancy" in sample
    assert "parent_agent_id" in sample
    assert sample["occupancy"] in ("available", "queued", "busy", "archived")


@pytest.mark.asyncio
async def test_list_models_requires_registry_in_extras(ctx: ToolContext) -> None:
    from lyre.runtime.tools.introspect import LIST_MODELS

    # ctx fixture doesn't set extras["model_registry"]; tool should error
    with pytest.raises(ToolError, match="model_registry not available"):
        await LIST_MODELS.handler(ctx, {})


@pytest.mark.asyncio
async def test_list_models_returns_registry_with_auth_state(
    ctx: ToolContext, monkeypatch
) -> None:
    from lyre.runtime.tools.introspect import LIST_MODELS

    class _FakeEndpoint:
        auth_env = "TEST_API_KEY"

    class _FakeEntry:
        id = "fake.fast"
        provider = "fake"
        tier = "cheap"
        capabilities = ("tool_use",)
        status = "enabled"
        context_window = 32000
        endpoint = _FakeEndpoint()

    class _FakeRegistry:
        entries = [_FakeEntry()]

    ctx.extras["model_registry"] = _FakeRegistry()
    monkeypatch.setenv("TEST_API_KEY", "yes")

    out = await LIST_MODELS.handler(ctx, {})
    assert out["count"] == 1
    m = out["models"][0]
    assert m["id"] == "fake.fast"
    assert m["auth_ok"] is True
    assert m["healthy"] is None  # no HealthTracker in this ctx


@pytest.mark.asyncio
async def test_list_models_handles_header_only_entries(
    ctx: ToolContext,
) -> None:
    """``auth_env=None`` is the header-only auth shape (internal
    gateway with custom headers, no env-var-backed API key). Used
    to crash list_models() with
    ``TypeError: str expected, not NoneType`` because the impl
    called ``os.environ.get(None)`` directly. After the fix
    header-only entries report ``auth_ok=True`` and the call
    succeeds."""
    from lyre.runtime.tools.introspect import LIST_MODELS

    class _HeaderOnlyEndpoint:
        auth_env = None  # the header-only signal

    class _HeaderOnlyEntry:
        id = "internal.gateway"
        provider = "openai"
        tier = "workhorse"
        capabilities = ("tool_use",)
        status = "enabled"
        context_window = 128_000
        endpoint = _HeaderOnlyEndpoint()

    class _Registry:
        entries = [_HeaderOnlyEntry()]

    ctx.extras["model_registry"] = _Registry()

    out = await LIST_MODELS.handler(ctx, {})
    assert out["count"] == 1
    m = out["models"][0]
    assert m["id"] == "internal.gateway"
    # Header-only is treated as authenticated — startup already
    # validated the configured headers.
    assert m["auth_ok"] is True


@pytest.mark.asyncio
async def test_create_agent_empty_string_model_is_treated_as_unset(
    ctx: ToolContext,
) -> None:
    """The model emits ``model=""`` sometimes to mean "no override —
    use persona default". Without coercion the empty string fell
    through to a confusing ``model_id '' not in registry`` error
    that prevented the dispatcher from recovering (a contributing
    factor in the phantom-delegation failure report). After the
    fix, empty string is normalised to "use persona default" —
    same as omitting ``model`` entirely."""
    from lyre.runtime.tools.introspect import CREATE_AGENT

    out = await CREATE_AGENT.handler(
        ctx, {"persona": "worker", "name": "alice-empty", "model": ""},
    )
    assert out["agent_id"] == "worker/alice-empty"
    # No model_id should have been recorded (we want persona default).
    assert out.get("metadata", {}).get("model_id") is None


@pytest.mark.asyncio
async def test_create_agent_invalid_model_hints_at_unset_fallback(
    ctx: ToolContext,
) -> None:
    """An unknown ``model`` should not just say 'not in registry' —
    the error needs to point the model at the right escape
    hatch (omit `model` to use persona default), otherwise the
    dispatcher tries to guess a model id, fails again, and burns
    turns. Verify the hint text is present."""
    from lyre.runtime.tools.introspect import CREATE_AGENT

    class _Registry:
        def by_id(self, _id: str) -> None:
            return None

    ctx.extras["model_registry"] = _Registry()

    with pytest.raises(ToolError) as exc_info:
        await CREATE_AGENT.handler(
            ctx,
            {
                "persona": "worker", "name": "fred",
                "model": "anthropic:made-up-model",
            },
        )
    msg = str(exc_info.value)
    assert "not in registry" in msg
    # The recovery hint is the loop-breaker for a model trying to
    # guess model ids.
    assert "omit" in msg.lower()
