"""Tests for the mailbox-reaction mechanism.

A reaction is a lightweight ack signal on an existing mail message. The
defining property: it does NOT behave like mail — no new mailbox row,
no unread-count change, no Phase 0 auto-wake. Exists specifically to
break the handshake-storm pattern where two polite agents bounce
"收到 / closing" off each other indefinitely.

Tests below pin down all four properties (persistence layer + tool layer
+ Phase 0 invisibility + persona allowlist + json schema).
"""

from __future__ import annotations

import pytest

from lyre.persistence.models import MailboxMessage, Persona, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.tools import ToolContext
from lyre.runtime.tools.mailbox import _mailbox_react


@pytest.fixture
async def ctx(repos: SqliteRepositories) -> ToolContext:
    """Minimal ToolContext: dispatcher + analyst-1 personas/agents + a
    leader-side task & wakeup so the agent loop's lease-holder semantic
    is consistent with production."""
    for persona_name, agent_id in (
        ("dispatcher", "dispatcher"),
        ("analyst", "analyst-1"),
    ):
        await repos.personas.upsert(
            Persona(
                name=persona_name,
                role_description=persona_name,
                system_prompt=persona_name,
            )
        )
        await repos.agents.create(agent_id=agent_id, persona_name=persona_name)
    await repos.mailbox.ensure_mailbox("dispatcher")
    await repos.mailbox.ensure_mailbox("analyst-1")

    task_id = await repos.tasks.create(
        TaskSpec(agent_id="dispatcher", goal="g", acceptance="a")
    )
    wakeup_id = await repos.wakeups.start(task_id, "dispatcher", agent_id="dispatcher")
    await repos.tasks.claim_lease(task_id, wakeup_id, duration_sec=600)
    return ToolContext(
        repos=repos,
        task_id=task_id,
        wakeup_id=wakeup_id,
        persona_name="dispatcher",
        agent_id="dispatcher",
        extras={},
    )


# ---------------------------------------------------------------------------
# Repository layer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_reaction_persists_and_is_idempotent(
    repos: SqliteRepositories,
) -> None:
    await repos.personas.upsert(
        Persona(name="dispatcher", role_description="d", system_prompt="d")
    )
    await repos.personas.upsert(
        Persona(name="analyst", role_description="a", system_prompt="a")
    )
    await repos.agents.create(agent_id="dispatcher", persona_name="dispatcher")
    await repos.agents.create(agent_id="analyst-1", persona_name="analyst")
    await repos.mailbox.ensure_mailbox("dispatcher")

    msg_id = await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="dispatcher", external_id="ext-1",
            sender="analyst-1", urgency="normal",
            body="closing this thread, no further action needed",
        )
    )

    # First react: new row written.
    inserted = await repos.mailbox.add_reaction(
        msg_id=msg_id, reactor="dispatcher", kind="ack",
    )
    assert inserted is True

    # Same (msg_id, reactor, kind) again: no-op, returns False.
    inserted_again = await repos.mailbox.add_reaction(
        msg_id=msg_id, reactor="dispatcher", kind="ack",
    )
    assert inserted_again is False

    reactions = await repos.mailbox.list_reactions(msg_id)
    assert len(reactions) == 1
    assert reactions[0].reactor == "dispatcher"
    assert reactions[0].kind == "ack"


@pytest.mark.asyncio
async def test_reaction_does_not_change_unread_state(
    repos: SqliteRepositories,
) -> None:
    """The key Phase-0 invariant: reactions are out-of-band. They do NOT
    insert a mailbox_messages row, so count_unread / read_unread on the
    REACTOR's mailbox stays unchanged. This is what prevents the
    handshake-loop the feature was designed to break."""
    await repos.personas.upsert(
        Persona(name="dispatcher", role_description="d", system_prompt="d")
    )
    await repos.personas.upsert(
        Persona(name="analyst", role_description="a", system_prompt="a")
    )
    await repos.agents.create(agent_id="dispatcher", persona_name="dispatcher")
    await repos.agents.create(agent_id="analyst-1", persona_name="analyst")
    await repos.mailbox.ensure_mailbox("dispatcher")
    await repos.mailbox.ensure_mailbox("analyst-1")

    msg_id = await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="dispatcher", external_id="ext-1",
            sender="analyst-1", urgency="normal", body="closing",
        )
    )

    before_dispatcher = await repos.mailbox.count_unread("dispatcher")
    before_analyst = await repos.mailbox.count_unread("analyst-1")

    await repos.mailbox.add_reaction(
        msg_id=msg_id, reactor="dispatcher", kind="ack",
    )

    # Neither mailbox's unread count moves.
    assert await repos.mailbox.count_unread("dispatcher") == before_dispatcher
    assert await repos.mailbox.count_unread("analyst-1") == before_analyst


@pytest.mark.asyncio
async def test_get_message_hydrates_reactions(
    repos: SqliteRepositories,
) -> None:
    """`mailbox_get_message` is how the original sender sees that someone
    reacted. The reactions list must be attached on read."""
    await repos.personas.upsert(
        Persona(name="dispatcher", role_description="d", system_prompt="d")
    )
    await repos.personas.upsert(
        Persona(name="analyst", role_description="a", system_prompt="a")
    )
    await repos.agents.create(agent_id="dispatcher", persona_name="dispatcher")
    await repos.agents.create(agent_id="analyst-1", persona_name="analyst")
    await repos.mailbox.ensure_mailbox("dispatcher")

    msg_id = await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="dispatcher", external_id="ext-1",
            sender="analyst-1", urgency="normal", body="closing",
        )
    )

    msg = await repos.mailbox.get_message(msg_id)
    assert msg is not None
    assert msg.reactions == []  # hydrated, just empty

    await repos.mailbox.add_reaction(
        msg_id=msg_id, reactor="dispatcher", kind="ack",
    )
    msg = await repos.mailbox.get_message(msg_id)
    assert msg is not None and msg.reactions is not None
    assert len(msg.reactions) == 1
    assert msg.reactions[0].reactor == "dispatcher"


# ---------------------------------------------------------------------------
# Tool layer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mailbox_react_tool_happy_path(ctx) -> None:
    """Wired through the tool surface end-to-end: dispatcher reacts to
    analyst's mail with `ack`, gets `status="ok"`."""
    repos = ctx.repos
    msg_id = await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="dispatcher", external_id="ext-1",
            sender="analyst-1", urgency="normal", body="closing",
        )
    )

    result = await _mailbox_react(ctx, {"msg_id": msg_id, "kind": "ack"})
    assert result["status"] == "ok"
    assert result["msg_id"] == msg_id
    assert result["reactor"] == "dispatcher"


@pytest.mark.asyncio
async def test_mailbox_react_tool_default_kind_is_ack(ctx) -> None:
    """Omitting `kind` defaults to ack — the only allowed value today."""
    repos = ctx.repos
    msg_id = await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="dispatcher", external_id="ext-1",
            sender="analyst-1", urgency="normal", body="closing",
        )
    )
    result = await _mailbox_react(ctx, {"msg_id": msg_id})
    assert result["status"] == "ok" and result["kind"] == "ack"


@pytest.mark.asyncio
async def test_mailbox_react_tool_repeat_is_already_reacted(ctx) -> None:
    """Same (msg_id, reactor, kind) twice: surfaces an idempotency
    signal so the model can tell its retry was a no-op."""
    repos = ctx.repos
    msg_id = await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="dispatcher", external_id="ext-1",
            sender="analyst-1", urgency="normal", body="closing",
        )
    )
    await _mailbox_react(ctx, {"msg_id": msg_id, "kind": "ack"})
    second = await _mailbox_react(ctx, {"msg_id": msg_id, "kind": "ack"})
    assert second["status"] == "already_reacted"


@pytest.mark.asyncio
async def test_mailbox_react_tool_rejects_unknown_kind(ctx) -> None:
    """The kind enum is restricted at the tool layer too — defense in
    depth against agents inventing 'thanks' / 'lgtm' before the
    vocabulary is formally expanded."""
    from lyre.runtime.tools import ToolError

    repos = ctx.repos
    msg_id = await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="dispatcher", external_id="ext-1",
            sender="analyst-1", urgency="normal", body="closing",
        )
    )
    with pytest.raises(ToolError, match="kind must be"):
        await _mailbox_react(ctx, {"msg_id": msg_id, "kind": "lgtm"})


@pytest.mark.asyncio
async def test_mailbox_react_tool_rejects_unknown_msg(ctx) -> None:
    """Reacting to a non-existent msg_id surfaces an immediate error
    rather than silently creating a dangling row."""
    from lyre.runtime.tools import ToolError

    with pytest.raises(ToolError, match="no mail with id"):
        await _mailbox_react(ctx, {"msg_id": 999_999, "kind": "ack"})


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def test_mailbox_react_is_registered_in_default_registry() -> None:
    """Smoke-check that build_default_registry exposes the new tool."""
    from lyre.runtime.tools.builtin import build_default_registry
    reg = build_default_registry()
    assert reg.get("mailbox_react") is not None


def test_mailbox_react_in_persona_allowlists() -> None:
    """Every persona that has mailbox_send must also have mailbox_react —
    the two are designed to be picked from in tandem (send for new info,
    react to break ack loops)."""
    from lyre.personas.seed import discover_persona_files, load_persona_from_file

    paths = discover_persona_files(None)
    by_name = {p.stem: load_persona_from_file(p) for p in paths}

    for name, persona in by_name.items():
        allowed = set(persona.allowed_lyre_tools)
        if "mailbox_send" in allowed:
            assert "mailbox_react" in allowed, (
                f"persona {name!r} has mailbox_send but not mailbox_react; "
                f"agents need both — react is the loop-breaker for ack mail"
            )
