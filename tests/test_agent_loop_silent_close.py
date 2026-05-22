"""Silent-close fallback tests.

Background: in production we saw a dispatcher wakeup spend 11 minutes calling
`mailbox_read` three times, never composing a reply, then exit
status=completed — leaving the user staring at silence (transcript
019e40c5-…). The AgentLoop now detects this pattern (nudge budget
exhausted + no user-facing tool) and:
  - auto-sends an apology mail to each asker
  - sets result.status="silent_close" so the scheduler can flag the
    wakeup separately from clean completion
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lyre.adapter.llm_adapter import (
    LyreContentBlock,
    LyreMessage,
    ToolUseComplete,
    TurnComplete,
    Usage,
)
from lyre.persistence.models import MailboxMessage, Persona, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.agent_loop import AgentLoop, _askers_from_mailbox_read
from lyre.runtime.tools import ToolContext
from lyre.runtime.tools.builtin import build_default_registry
from lyre.runtime.transcript import TranscriptWriter

from .fake_adapter import FakeAdapter
from .helpers import fake_entry


@pytest.fixture
async def silent_setup(
    repos: SqliteRepositories, object_store: Path
) -> tuple[FakeAdapter, ToolContext, TranscriptWriter]:
    for name in ("worker", "owner"):
        await repos.personas.upsert(
            Persona(name=name, role_description=name, system_prompt=name)
        )
        await repos.agents.create(agent_id=name, persona_name=name)
    # Seed unread mail FROM owner TO worker so mailbox_read returns a sender.
    await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="worker",
            external_id="seed-1",
            sender="owner",
            urgency="high",
            title="please look at X",
            body="...",
        )
    )
    task_id = await repos.tasks.create(
        TaskSpec(agent_id="worker", goal="g", acceptance="a")
    )
    wakeup_id = await repos.wakeups.start(task_id, "worker")
    await repos.tasks.claim_lease(task_id, wakeup_id, duration_sec=600)
    return (
        FakeAdapter(),
        ToolContext(
            repos=repos,
            task_id=task_id,
            wakeup_id=wakeup_id,
            persona_name="worker",
            agent_id="worker",
        ),
        TranscriptWriter(object_store, wakeup_id),
    )


@pytest.mark.asyncio
async def test_silent_close_fires_fallback_mail(silent_setup) -> None:
    """Three turns of mailbox_read with stop=end_turn but no mailbox_send →
    nudge budget exhausts → fallback mail goes to the asker."""
    adapter, ctx, transcript = silent_setup

    # Three identical info-gathering turns, all with stop_reason=end_turn
    # (the DeepSeek pathology that triggers the silent-turn nudge path).
    for i in range(3):
        adapter.push_turn(
            [
                ToolUseComplete(
                    id=f"tu_read_{i}", name="mailbox_read", input={}
                ),
                Usage(input_tokens=50, output_tokens=5),
                TurnComplete(stop_reason="end_turn"),
            ]
        )

    loop = AgentLoop(
        candidates=[fake_entry(id="m")],
        adapter_for=lambda e: adapter,
        model_name_for=lambda e: e.id,
        transcript=transcript,
        tool_registry=build_default_registry(),
        tool_context=ctx,
        allowed_tools=["mailbox_read", "mailbox_send", "mark_read"],
    )
    result = await loop.run(
        system_prompt="",
        initial_messages=[
            LyreMessage(role="user", content=[LyreContentBlock(type="text", text="go")])
        ],
    )
    transcript.close()

    assert result.status == "silent_close"
    # Three mailbox_read calls, no mailbox_send from the model itself,
    # but the runtime should have enqueued ONE fallback to owner.
    outbox_rows = await ctx.repos.outbox.dequeue_batch(limit=10)
    fallback_rows = [
        r for r in outbox_rows
        if r.payload.get("metadata", {}).get("silent_close")
    ]
    assert len(fallback_rows) == 1
    fb = fallback_rows[0]
    assert fb.payload["recipient"] == "owner"
    assert fb.payload["sender"] == "worker"
    assert fb.payload["urgency"] == "high"
    # external_id deterministic so retries are idempotent
    assert fb.external_id == f"silent-close:{ctx.wakeup_id}:owner"
    # Body must mention the wakeup id for operator debug + tool summary
    assert ctx.wakeup_id in fb.payload["body"]
    assert "mailbox_read" in fb.payload["body"]


@pytest.mark.asyncio
async def test_mailbox_react_skips_silent_close(silent_setup) -> None:
    """Acking a mail with ``mailbox_react`` is a legitimate response —
    it's specifically the "silent ack" primitive that closes a thread
    without pushing a notification. The silent-close detector USED to
    miss this: ``mailbox_react`` wasn't in ``_USER_FACING_TOOLS``, so a
    wakeup that read mail and reacted (instead of sending) still
    triggered the silent-close fallback, which then sent an unwanted
    "I gathered context but didn't reply" apology mail — undoing the
    whole point of using react instead of send.

    Both the silent-turn NUDGE (mid-loop synthetic user msg asking the
    model to keep going) and the silent-close FALLBACK (wakeup-end
    apology mail) consult the same ``made_user_facing_action`` flag,
    which is set by ``tu['name'] in _USER_FACING_TOOLS``. The FakeAdapter
    below only has 3 turns queued (read / react / end_turn) — if the
    nudge fired after the end_turn it would demand a 4th turn the
    adapter doesn't have and the loop would crash, so a clean
    ``result.status == "completed"`` proves both layers ignored the
    react path correctly.
    """
    adapter, ctx, transcript = silent_setup

    # Reusing the seed mail planted by the fixture (id=1, from owner).
    adapter.push_turn(
        [
            ToolUseComplete(id="tu_r", name="mailbox_read", input={}),
            TurnComplete(stop_reason="tool_use"),
        ]
    )
    adapter.push_turn(
        [
            ToolUseComplete(
                id="tu_react",
                name="mailbox_react",
                input={"msg_id": 1, "kind": "ack"},
            ),
            TurnComplete(stop_reason="tool_use"),
        ]
    )
    adapter.push_turn([TurnComplete(stop_reason="end_turn")])

    loop = AgentLoop(
        candidates=[fake_entry(id="m")],
        adapter_for=lambda e: adapter,
        model_name_for=lambda e: e.id,
        transcript=transcript,
        tool_registry=build_default_registry(),
        tool_context=ctx,
        allowed_tools=["mailbox_read", "mailbox_react"],
    )
    result = await loop.run(
        system_prompt="",
        initial_messages=[
            LyreMessage(role="user", content=[LyreContentBlock(type="text", text="go")])
        ],
    )
    transcript.close()

    assert result.status == "completed"
    # No silent-close apology mail enqueued — the react itself is
    # the response.
    outbox_rows = await ctx.repos.outbox.dequeue_batch(limit=10)
    silent_close_rows = [
        r for r in outbox_rows
        if (r.payload.get("metadata") or {}).get("silent_close")
    ]
    assert not silent_close_rows


@pytest.mark.asyncio
async def test_reply_in_same_wakeup_skips_silent_close(silent_setup) -> None:
    """Happy path: model reads then replies. status=completed, no fallback."""
    adapter, ctx, transcript = silent_setup

    adapter.push_turn(
        [
            ToolUseComplete(id="tu_r", name="mailbox_read", input={}),
            TurnComplete(stop_reason="tool_use"),
        ]
    )
    adapter.push_turn(
        [
            ToolUseComplete(
                id="tu_s",
                name="mailbox_send",
                input={"to": "owner", "body": "looked, here it is"},
            ),
            TurnComplete(stop_reason="tool_use"),
        ]
    )
    adapter.push_turn([TurnComplete(stop_reason="end_turn")])

    loop = AgentLoop(
        candidates=[fake_entry(id="m")],
        adapter_for=lambda e: adapter,
        model_name_for=lambda e: e.id,
        transcript=transcript,
        tool_registry=build_default_registry(),
        tool_context=ctx,
        allowed_tools=["mailbox_read", "mailbox_send"],
    )
    result = await loop.run(
        system_prompt="",
        initial_messages=[
            LyreMessage(role="user", content=[LyreContentBlock(type="text", text="go")])
        ],
    )
    transcript.close()

    assert result.status == "completed"
    outbox_rows = await ctx.repos.outbox.dequeue_batch(limit=10)
    # Exactly the model's own send, no fallback.
    assert len(outbox_rows) == 1
    meta = outbox_rows[0].payload.get("metadata") or {}
    assert not meta.get("silent_close")


def test_askers_helper_extracts_from_inbox_read() -> None:
    """The parser correctly pulls senders from a mailbox_read inbox listing."""
    result = (
        '{"box": "inbox", "recipient": "dispatcher", "auto_marked_read": true, '
        '"messages": [{"id": 1, "sender": "owner", "title": "x"}, '
        '{"id": 2, "sender": "worker-1", "title": "y"}]}'
    )
    assert _askers_from_mailbox_read(result) == {"owner", "worker-1"}


def test_askers_helper_ignores_sent_box() -> None:
    """A sent-box listing has the agent's OWN sends; those aren't askers."""
    result = (
        '{"box": "sent", "sender": "dispatcher", "auto_marked_read": false, '
        '"messages": [{"id": 1, "recipient": "owner", "title": "x"}]}'
    )
    assert _askers_from_mailbox_read(result) == set()


def test_askers_helper_ignores_non_auto_marked() -> None:
    """If we didn't auto-mark, the model already knew about those messages
    from a prior wakeup; not necessarily 'this wakeup's' askers."""
    result = (
        '{"box": "inbox", "recipient": "dispatcher", "auto_marked_read": false, '
        '"messages": [{"id": 1, "sender": "owner"}]}'
    )
    assert _askers_from_mailbox_read(result) == set()


def test_askers_helper_robust_to_garbage() -> None:
    assert _askers_from_mailbox_read("not json") == set()
    assert _askers_from_mailbox_read("null") == set()
    assert _askers_from_mailbox_read("[]") == set()


@pytest.mark.asyncio
async def test_loop_continues_after_tool_use_with_end_turn_stop_reason(
    silent_setup,
) -> None:
    """Regression: every observed ack-then-stop silent failure traced
    back to the AgentLoop's structural bug where `stop_reason=end_turn`
    alongside tool_use blocks caused an immediate `break` after tool
    execution. The model called mailbox_send, the runtime appended the
    "reminder: this doesn't end the wakeup" tool_result, and then the
    loop exited — the model NEVER got to react. This test pins the
    fix: after executing tools, the model must always get one more
    turn regardless of stop_reason.

    User's actual diagnostic question (verbatim): "为什么silent stop每次都发生在
    tool call之后？是不是你的loop从来不会从tool call了之后继续？"
    """
    adapter, ctx, transcript = silent_setup

    # Turn 1: model calls mailbox_send AND signals stop_reason=end_turn
    # alongside it. This is DeepSeek-V4's actual streaming pattern.
    adapter.push_turn(
        [
            ToolUseComplete(
                id="ts",
                name="mailbox_send",
                input={"to": "owner", "body": "here's the result"},
            ),
            TurnComplete(stop_reason="end_turn"),
        ]
    )
    # Turn 2: model sees the mailbox_send tool_result + the reminder
    # field, decides nothing more to do, emits an end_turn with no
    # tools. This is the natural exit.
    adapter.push_turn([TurnComplete(stop_reason="end_turn")])

    loop = AgentLoop(
        candidates=[fake_entry(id="m")],
        adapter_for=lambda e: adapter,
        model_name_for=lambda e: e.id,
        transcript=transcript,
        tool_registry=build_default_registry(),
        tool_context=ctx,
        allowed_tools=["mailbox_read", "mailbox_send"],
    )
    result = await loop.run(
        system_prompt="",
        initial_messages=[
            LyreMessage(role="user", content=[LyreContentBlock(type="text", text="go")])
        ],
    )
    transcript.close()

    # Critical: the adapter MUST have been called twice. Turn 2 is the
    # model's chance to read the tool_result the loop fed it.
    assert len(adapter.calls) == 2, (
        f"loop only called the model {len(adapter.calls)} time(s); "
        f"after a tool_use the model must get ≥1 more turn to react"
    )
    # Turn 2's messages must include the tool_result from turn 1.
    call2_msgs = adapter.calls[1]["messages"]
    has_tool_result = any(
        blk.type == "tool_result"
        for m in call2_msgs
        for blk in m.content
    )
    assert has_tool_result, "turn 2 must show the model the tool_result"
    assert result.status == "completed"
    assert result.turns == 2
