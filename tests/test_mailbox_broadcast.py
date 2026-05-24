"""Tests for the broadcast / reply / forward / agents-directory upgrades."""

from __future__ import annotations

from pathlib import Path

import pytest

from lyre.outbox.dispatcher import OutboxDispatcher
from lyre.persistence.db import init_db
from lyre.persistence.models import MailboxMessage, Persona, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.context import _format_agents_directory, assemble_system_prompt
from lyre.runtime.tools import ToolContext, ToolError
from lyre.runtime.tools.builtin import build_default_registry
from lyre.runtime.tools.mailbox import MAILBOX_GET_MESSAGE, MAILBOX_SEND

# ---------------------------------------------------------------------------
# Migration runner: initial schema includes broadcast columns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initial_schema_has_broadcast_columns(tmp_path: Path) -> None:
    db = tmp_path / "lyre.db"
    conn = await init_db(db)
    try:
        async with conn.execute(
            "PRAGMA table_info(mailbox_messages)"
        ) as cur:
            rows = await cur.fetchall()
        cols = {r["name"] for r in rows}
        assert "broadcast_id" in cols
        assert "recipients_all" in cols

        async with conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ) as cur:
            versions = [r["version"] for r in await cur.fetchall()]
        # Fresh init runs every shipped migration. Assert presence,
        # not exact ordinal — keeps the test stable as new migrations
        # land (0002_blobs, etc.).
        assert 1 in versions
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_migration_runner_idempotent_on_reinit(tmp_path: Path) -> None:
    db = tmp_path / "lyre.db"
    c1 = await init_db(db)
    async with c1.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ) as cur:
        first_versions = [r["version"] for r in await cur.fetchall()]
    await c1.close()
    c2 = await init_db(db)
    try:
        async with c2.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ) as cur:
            second_versions = [r["version"] for r in await cur.fetchall()]
        # Re-init must NOT add or duplicate rows — the schema_migrations
        # table is a fixed-point set per init_db call.
        assert second_versions == first_versions
    finally:
        await c2.close()


# ---------------------------------------------------------------------------
# Tool: mailbox_send — multi-recipient, reply, forward
# ---------------------------------------------------------------------------


@pytest.fixture
async def ctx(repos: SqliteRepositories) -> ToolContext:
    for name in ("dispatcher", "worker-maintainer", "reviewer", "owner"):
        await repos.personas.upsert(
            Persona(name=name, role_description=f"{name} role", system_prompt=name)
        )
        # Seed a matching agent so post-A3 recipient validation accepts
        # mail addressed to these names (the seeded owner/dispatcher follow
        # the same id == persona pattern in production).
        await repos.agents.create(agent_id=name, persona_name=name)
    task_id = await repos.tasks.create(
        TaskSpec(agent_id="dispatcher", goal="g", acceptance="a")
    )
    wakeup_id = await repos.wakeups.start(task_id, "dispatcher")
    await repos.tasks.claim_lease(task_id, wakeup_id, duration_sec=600)
    return ToolContext(
        repos=repos, task_id=task_id, wakeup_id=wakeup_id,
        persona_name="dispatcher", agent_id="dispatcher",
    )


@pytest.mark.asyncio
async def test_broadcast_creates_n_outbox_rows_with_shared_broadcast_id(
    ctx: ToolContext,
) -> None:
    res = await MAILBOX_SEND.handler(
        ctx,
        {
            "to": ["worker-maintainer", "reviewer"],
            "body": "kickoff",
            "_tool_use_id": "tu_bc",
        },
    )
    assert res["status"] == "queued"
    assert res["broadcast_id"] is not None
    assert set(res["recipients"]) == {"worker-maintainer", "reviewer"}

    batch = await ctx.repos.outbox.dequeue_batch(limit=10)
    assert len(batch) == 2
    bc_ids = {r.payload["broadcast_id"] for r in batch}
    assert len(bc_ids) == 1
    recipients_all_sets = {
        tuple(r.payload["recipients_all"]) for r in batch
    }
    # Both copies must list the SAME full recipient set.
    assert len(recipients_all_sets) == 1
    full = next(iter(recipients_all_sets))
    assert set(full) == {"worker-maintainer", "reviewer"}


@pytest.mark.asyncio
async def test_single_recipient_has_no_broadcast_id(ctx: ToolContext) -> None:
    res = await MAILBOX_SEND.handler(
        ctx,
        {"to": "owner", "body": "fyi", "_tool_use_id": "tu_solo"},
    )
    assert res["broadcast_id"] is None
    batch = await ctx.repos.outbox.dequeue_batch(limit=10)
    assert len(batch) == 1
    assert batch[0].payload["broadcast_id"] is None
    assert batch[0].payload["recipients_all"] is None


@pytest.mark.asyncio
async def test_broadcast_delivered_via_dispatcher_yields_n_rows(
    ctx: ToolContext,
) -> None:
    await MAILBOX_SEND.handler(
        ctx,
        {
            "to": ["worker-maintainer", "reviewer"],
            "body": "go",
            "_tool_use_id": "tu_d",
        },
    )
    disp = OutboxDispatcher(ctx.repos)
    delivered = await disp.tick()
    assert delivered == 2

    a = await ctx.repos.mailbox.read_messages("worker-maintainer")
    b = await ctx.repos.mailbox.read_messages("reviewer")
    assert [m.body for m in a] == ["go"]
    assert [m.body for m in b] == ["go"]
    # Each delivered row carries the shared broadcast_id + recipients_all.
    assert a[0].broadcast_id == b[0].broadcast_id
    assert set(a[0].recipients_all or []) == {"worker-maintainer", "reviewer"}


@pytest.mark.asyncio
async def test_reply_to_sets_parent_msg_id(ctx: ToolContext) -> None:
    # Seed a parent message in dispatcher's mailbox
    await ctx.repos.mailbox.ensure_mailbox("dispatcher")
    parent_id = await ctx.repos.mailbox.insert_message(
        MailboxMessage(
            recipient="dispatcher", external_id="px",
            sender="owner", urgency="normal", body="please report",
        )
    )

    await MAILBOX_SEND.handler(
        ctx,
        {
            "to": "owner",
            "body": "report attached",
            "reply_to": parent_id,
            "_tool_use_id": "tu_reply",
        },
    )
    disp = OutboxDispatcher(ctx.repos)
    await disp.tick()
    msgs = await ctx.repos.mailbox.read_messages("owner")
    assert msgs[0].parent_msg_id == parent_id


@pytest.mark.asyncio
async def test_mailbox_send_with_attachment_lands_in_recipient_inbox(
    ctx: ToolContext,
) -> None:
    """Owner uploads an image (blob in DB), then mail is sent with
    that blob_id in attachments. The dispatched mail must carry the
    same blob_id through to recipient.mailbox_messages.attachments."""
    from lyre.persistence.models import Blob
    blob = Blob(
        id="a" * 64, media_type="image/png", size_bytes=100,
        filename="shot.png", source="owner",
    )
    await ctx.repos.blobs.upsert(blob)

    await MAILBOX_SEND.handler(
        ctx,
        {
            "to": "worker-maintainer",
            "body": "see this screenshot",
            "attachments": ["a" * 64],
            "_tool_use_id": "tu_attach",
        },
    )
    disp = OutboxDispatcher(ctx.repos)
    delivered = await disp.tick()
    assert delivered == 1
    received = await ctx.repos.mailbox.read_messages("worker-maintainer")
    assert received[0].attachments == ["a" * 64]


@pytest.mark.asyncio
async def test_mailbox_send_rejects_unknown_blob_id(
    ctx: ToolContext,
) -> None:
    """Forwarding-only invariant: an attachment must reference a blob
    the agent has seen. An unknown blob_id is the failure surface for
    a model that tried to fabricate one."""
    with pytest.raises(ToolError, match="unknown attachment"):
        await MAILBOX_SEND.handler(
            ctx,
            {
                "to": "worker-maintainer",
                "body": "look",
                "attachments": ["f" * 64],
                "_tool_use_id": "tu_fake",
            },
        )


@pytest.mark.asyncio
async def test_mailbox_get_message_returns_view_blocks_for_image(
    ctx: ToolContext,
) -> None:
    """A received mail with image attachments must surface them via
    the magic `_lyre_view_blocks` key so the agent_loop can hydrate
    them into LyreContentBlock(type='image', ...) entries on the
    user message that carries the tool_result."""
    from lyre.persistence.models import Blob
    from lyre.persistence.models import MailboxMessage as MM
    await ctx.repos.blobs.upsert(Blob(
        id="b" * 64, media_type="image/png", size_bytes=50,
        filename="diagram.png", source="owner",
    ))
    await ctx.repos.mailbox.ensure_mailbox("dispatcher")
    mid = await ctx.repos.mailbox.insert_message(MM(
        recipient="dispatcher", external_id="m-img",
        sender="owner", urgency="normal", body="see attached",
        attachments=["b" * 64],
    ))
    out = await MAILBOX_GET_MESSAGE.handler(ctx, {"msg_id": mid})
    # Top-level structured attachments — visible to the model as JSON.
    assert out["attachments"] == [
        {
            "blob_id": "b" * 64, "media_type": "image/png",
            "filename": "diagram.png", "size_bytes": 50,
        }
    ]
    # The magic key the agent_loop pops to build image LyreContentBlocks.
    assert out["_lyre_view_blocks"] == [
        {
            "type": "image", "blob_id": "b" * 64,
            "media_type": "image/png", "filename": "diagram.png",
        }
    ]


@pytest.mark.asyncio
async def test_forward_msg_id_lands_in_metadata(ctx: ToolContext) -> None:
    await ctx.repos.mailbox.ensure_mailbox("dispatcher")
    original_id = await ctx.repos.mailbox.insert_message(
        MailboxMessage(
            recipient="dispatcher", external_id="o",
            sender="owner", urgency="normal", body="big intent",
        )
    )
    await MAILBOX_SEND.handler(
        ctx,
        {
            "to": ["worker-maintainer", "reviewer"],
            "body": "owner wants this — context in attachment",
            "forward_msg_id": original_id,
            "_tool_use_id": "tu_fwd",
        },
    )
    disp = OutboxDispatcher(ctx.repos)
    delivered = await disp.tick()
    assert delivered == 2
    a = (await ctx.repos.mailbox.read_messages("worker-maintainer"))[0]
    assert a.metadata is not None
    assert a.metadata["forwarded_from_msg_id"] == original_id


@pytest.mark.asyncio
async def test_refuse_to_send_to_self(ctx: ToolContext) -> None:
    """Immediate self-send is blocked (auto-wake-on-mail would
    instantly fire and trivial self-loops would be too easy). The
    scheduled-mail path has its own test that confirms self-send is
    allowed when delivery is delayed."""
    with pytest.raises(ToolError, match="immediate mail to self"):
        await MAILBOX_SEND.handler(
            ctx,
            {"to": ["dispatcher", "owner"], "body": "x", "_tool_use_id": "t"},
        )


@pytest.mark.asyncio
async def test_to_must_be_str_or_list_of_str(ctx: ToolContext) -> None:
    with pytest.raises(ToolError):
        await MAILBOX_SEND.handler(
            ctx, {"to": 42, "body": "x", "_tool_use_id": "t"},
        )
    with pytest.raises(ToolError):
        await MAILBOX_SEND.handler(
            ctx, {"to": [], "body": "x", "_tool_use_id": "t"},
        )


# ---------------------------------------------------------------------------
# mailbox_get_message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mailbox_get_message_fetches_any_recipient(
    ctx: ToolContext,
) -> None:
    await ctx.repos.mailbox.ensure_mailbox("worker-maintainer")
    mid = await ctx.repos.mailbox.insert_message(
        MailboxMessage(
            recipient="worker-maintainer",
            external_id="e",
            sender="dispatcher",
            urgency="normal",
            body="hi worker",
            broadcast_id="bc-x",
            recipients_all=["worker-maintainer", "reviewer"],
        )
    )
    res = await MAILBOX_GET_MESSAGE.handler(ctx, {"msg_id": mid})
    assert res["recipient"] == "worker-maintainer"
    assert res["broadcast_id"] == "bc-x"
    assert set(res["recipients_all"]) == {"worker-maintainer", "reviewer"}


@pytest.mark.asyncio
async def test_mailbox_get_message_rejects_unknown(ctx: ToolContext) -> None:
    with pytest.raises(ToolError, match="not found"):
        await MAILBOX_GET_MESSAGE.handler(ctx, {"msg_id": 99999})


def test_registry_exposes_get_message() -> None:
    reg = build_default_registry()
    assert "mailbox_get_message" in reg.all_names()


# ---------------------------------------------------------------------------
# Agents directory injection in system prompt
# ---------------------------------------------------------------------------


class _FakeAgent:
    def __init__(self, id_: str, persona_name: str, description: str | None = None):
        self.id = id_
        self.persona_name = persona_name
        self.status = "idle"
        self.description = description


def test_format_agents_directory_excludes_self() -> None:
    personas = [
        Persona(name="dispatcher", role_description="lead", system_prompt=""),
        Persona(name="worker-maintainer", role_description="works", system_prompt=""),
        Persona(name="owner", role_description="the human", system_prompt=""),
    ]
    agents = [
        _FakeAgent("dispatcher", "dispatcher"),
        _FakeAgent("worker-1", "worker-maintainer"),
        _FakeAgent("owner", "owner"),
    ]
    out = _format_agents_directory(agents, self_id="dispatcher", personas=personas)
    # Self never appears in own directory
    assert "dispatcher (persona=" not in out
    assert "worker-1 (persona=worker-maintainer)" in out
    assert "owner (persona=owner)" in out
    # The header now talks about agent ids, not persona names
    assert "AGENT IDs" in out


def test_assemble_system_prompt_no_longer_lists_agents_in_prompt() -> None:
    """Cache-friendliness: every create_agent / archive_agent would
    otherwise invalidate every other agent's system prompt. Agents
    directory removed from the prompt — model calls list_agents() on
    demand instead."""
    personas = [
        Persona(name="dispatcher", role_description="lead", system_prompt=""),
        Persona(name="worker-maintainer", role_description="works", system_prompt=""),
    ]
    me = Persona(name="dispatcher", role_description="lead", system_prompt="be terse")
    prompt = assemble_system_prompt(me, other_personas=personas)
    # The static directory block is gone — no header, no per-agent lines.
    assert "## Agents you can mailbox_send to" not in prompt
    assert "worker-maintainer (persona=" not in prompt
    # But the preamble must POINT the agent at list_agents() so it
    # knows where to look.
    assert "list_agents()" in prompt


def test_assemble_system_prompt_includes_identity_preamble() -> None:
    """Regression for the 'dispatcher-scheduler' hallucination: the model
    needs an unambiguous declaration of its own agent id as the very
    first thing in the prompt so it doesn't synthesize variants."""
    me = Persona(name="dispatcher", role_description="lead", system_prompt="be terse")
    prompt = assemble_system_prompt(me, agent_id="dispatcher")
    assert prompt.lstrip().startswith("You are agent **dispatcher**")
    assert "mailbox key is `dispatcher`" in prompt
    assert "synthesize variants" in prompt


def test_assemble_system_prompt_uses_explicit_agent_id() -> None:
    """When agent_id differs from persona name (e.g. worker-maintainer-1
    running the worker-maintainer role), identity must say the agent id."""
    me = Persona(
        name="worker-maintainer", role_description="builds", system_prompt="be terse"
    )
    prompt = assemble_system_prompt(me, agent_id="worker-maintainer-1")
    assert "You are agent **worker-maintainer-1**" in prompt
    assert "(persona: `worker-maintainer`)" in prompt
    assert "mailbox key is `worker-maintainer-1`" in prompt


def test_identity_preamble_states_text_is_not_delivered() -> None:
    """Regression for the silent-turn root cause: models believed plain
    text WAS the reply (Claude API convention) and ended turn without
    mailbox_send. The preamble must explicitly say only tool calls
    deliver anything — text is internal monologue."""
    me = Persona(name="dispatcher", role_description="lead", system_prompt="b")
    prompt = assemble_system_prompt(me, agent_id="dispatcher")
    # Must clarify text != delivery
    assert "internal monologue" in prompt
    # Must name the canonical reply tool
    assert "mailbox_send" in prompt
    # Must explicitly say plain text isn't delivered
    assert "reaches no one" in prompt or "reaches nothing" in prompt


def test_identity_preamble_teaches_stateless_wakeups() -> None:
    """The 'I'll go look at X' bug ('I don't have context next wakeup')
    is rooted in the model not knowing wakeups are stateless. The
    preamble must explicitly call out statelessness AND list the
    canonical persistence channels: sent-box recall, future mail to
    self, the notes file, and report_progress (crash-recovery only)."""
    me = Persona(name="dispatcher", role_description="lead", system_prompt="b")
    prompt = assemble_system_prompt(me, agent_id="dispatcher")
    assert "STATELESS WAKEUPS" in prompt
    # Canonical persistence channels
    assert 'mailbox_read(box="sent")' in prompt
    assert "deliver_in" in prompt and 'to="dispatcher"' in prompt
    # Notes file path must be in the preamble, plugged with agent id
    assert "facts/agent-dispatcher-notes.md" in prompt
    # report_progress repositioned as crash-recovery only
    assert "crash" in prompt.lower()


def test_identity_preamble_teaches_ack_and_stop_anti_pattern() -> None:
    """Preamble must name the ack-and-stop failure mode and list the
    legitimate \"later\" paths (real dispatch_task + future-mail). The
    section was renamed from 'MAILBOX_SEND DOES NOT END YOUR WAKEUP'
    to 'ACK-AND-STOP IS A LIE' once the agent_loop bug was fixed —
    the loop now enforces the mechanics, so the prompt only needs to
    teach the behavioral preference."""
    me = Persona(name="dispatcher", role_description="lead", system_prompt="b")
    prompt = assemble_system_prompt(me, agent_id="dispatcher")
    # Anti-pattern named with the exact phrases models tend to emit
    assert "background task" in prompt
    assert "IOU" in prompt or "稍后回复" in prompt
    # The legitimate "later" paths
    assert "dispatch_task" in prompt and "future-mail" in prompt.lower()
    # The mechanics ("no end_turn tool; wakeup ends when you stop
    # calling tools") still live in HOW WAKEUPS END
    assert "no `end_turn` tool" in prompt.lower() or "tool_use blocks" in prompt


def test_identity_preamble_blocks_phantom_delegation() -> None:
    """If the model says 'I started a background task' WITHOUT calling
    dispatch_task, that's a hallucination. The preamble must name
    this failure mode explicitly so the model recognises it."""
    me = Persona(name="dispatcher", role_description="lead", system_prompt="b")
    prompt = assemble_system_prompt(me, agent_id="dispatcher")
    assert "No phantom delegation" in prompt or "phantom delegation" in prompt.lower()
    assert "task_id" in prompt  # must require a concrete artifact


def test_identity_preamble_teaches_delegation_and_progress_via_mail() -> None:
    """Delegating to subagents must come with the 'always report before
    idling' invariant; long-running work reports progress via mail
    (no special tool)."""
    me = Persona(name="dispatcher", role_description="lead", system_prompt="b")
    prompt = assemble_system_prompt(me, agent_id="dispatcher")
    assert "DELEGATING WORK" in prompt
    assert "PROGRESS VIA MAIL" in prompt
    # Subagent → idle without reporting is the failure mode we warn about
    assert "report before idling" in prompt.lower() or "dropped the ball" in prompt.lower()


# ---------------------------------------------------------------------------
# B5: SYSTEM.md + persona APPEND.md  /  B6: AGENTS.md walking
# ---------------------------------------------------------------------------


def test_global_system_md_is_appended(tmp_path):
    """`~/.lyre/SYSTEM.md` content is concatenated last so it acts as the
    org-wide instruction layer."""
    (tmp_path / "SYSTEM.md").write_text("ORG_RULE: always say 'hi' first")
    me = Persona(name="dispatcher", role_description="lead", system_prompt="body")
    prompt = assemble_system_prompt(me, lyre_home=tmp_path)
    assert "ORG_RULE" in prompt
    # SYSTEM.md is the tail
    assert prompt.rstrip().endswith("always say 'hi' first")


def test_global_system_md_absent_is_fine(tmp_path):
    me = Persona(name="dispatcher", role_description="lead", system_prompt="body")
    prompt = assemble_system_prompt(me, lyre_home=tmp_path)
    assert "body" in prompt  # base content still present


def test_persona_append_md_is_appended(tmp_path):
    """Per-persona `~/.lyre/personas/<name>/APPEND.md` content joins after
    the persona body."""
    persona_dir = tmp_path / "personas" / "dispatcher"
    persona_dir.mkdir(parents=True)
    (persona_dir / "APPEND.md").write_text("LEADER_EXTRA: be terse")
    me = Persona(name="dispatcher", role_description="lead", system_prompt="body")
    prompt = assemble_system_prompt(me, lyre_home=tmp_path)
    assert "LEADER_EXTRA" in prompt
    # Comes after the persona body
    assert prompt.index("body") < prompt.index("LEADER_EXTRA")


def test_persona_append_md_only_applies_to_matching_persona(tmp_path):
    persona_dir = tmp_path / "personas" / "worker-maintainer"
    persona_dir.mkdir(parents=True)
    (persona_dir / "APPEND.md").write_text("FOR_WORKER_ONLY")
    dispatcher = Persona(name="dispatcher", role_description="lead", system_prompt="b")
    prompt = assemble_system_prompt(dispatcher, lyre_home=tmp_path)
    assert "FOR_WORKER_ONLY" not in prompt


def test_agents_md_walk_picks_up_cwd_and_parent(tmp_path):
    """B6: walking AGENTS.md from cwd upward concatenates everything found."""
    parent_dir = tmp_path / "repo"
    parent_dir.mkdir()
    (parent_dir / "AGENTS.md").write_text("REPO_LEVEL: code style = strict")
    sub_dir = parent_dir / "subproject"
    sub_dir.mkdir()
    (sub_dir / "CLAUDE.md").write_text("SUB_LEVEL: also handle X")

    me = Persona(name="worker", role_description="w", system_prompt="b")
    prompt = assemble_system_prompt(me, worktree_cwd=sub_dir)
    assert "REPO_LEVEL" in prompt
    assert "SUB_LEVEL" in prompt
    # Leaf (most-specific) appears before parent — model reads close
    # context first
    assert prompt.index("SUB_LEVEL") < prompt.index("REPO_LEVEL")
    assert "AGENTS.md / CLAUDE.md walk" in prompt


def test_agents_md_walk_no_files_means_no_section(tmp_path):
    me = Persona(name="w", role_description="w", system_prompt="b")
    prompt = assemble_system_prompt(me, worktree_cwd=tmp_path)
    assert "AGENTS.md / CLAUDE.md walk" not in prompt
