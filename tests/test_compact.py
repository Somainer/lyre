"""Context compaction.

Verifies the structural transformation done by `runtime/compact.py`:

  - mailbox_get_message tool_results → synthetic user messages (mail body
    preserved verbatim, owner / peer's words survive across the
    compaction seam)
  - mailbox_send tool_uses → synthetic assistant messages (agent's own
    replies survive, including reply_to / scheduling metadata)
  - idempotent / re-fetchable tool calls → dropped entirely
  - shell_exec / python_exec / read_memory / dispatch_task → folded into
    a one-paragraph work summary by the same model the wakeup is on
  - last K turn pairs remain INTACT (preserves thinking blocks for the
    next API call's `content[].thinking must be passed back` requirement)
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from lyre.adapter.llm_adapter import (
    ContentDelta,
    LyreContentBlock,
    LyreMessage,
    LyreToolSpec,
    StreamEvent,
    TurnComplete,
)
from lyre.runtime.compact import (
    compact_messages,
    find_pivot,
)

# ----------------------------------------------------------------------
# Test helpers
# ----------------------------------------------------------------------


class StubAdapter:
    """Records the summarizer prompt + returns a canned reply. Lets us
    inspect what the work-summary prompt looked like without making a
    real API call."""

    def __init__(self, canned_summary: str = "summary text") -> None:
        self.canned_summary = canned_summary
        self.calls: list[dict[str, Any]] = []

    async def stream_turn(
        self,
        messages: list[LyreMessage],
        tools: list[LyreToolSpec],
        model: str,
        max_tokens: int = 4096,
        temperature: float | None = None,
        system: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.calls.append(
            {"messages": messages, "tools": tools, "model": model,
             "system": system, "max_tokens": max_tokens}
        )
        yield ContentDelta(text=self.canned_summary)
        yield TurnComplete(stop_reason="end_turn")


def _user(text: str) -> LyreMessage:
    return LyreMessage(role="user", content=[LyreContentBlock(type="text", text=text)])


def _assistant_with_tools(*tools: dict[str, Any]) -> LyreMessage:
    blocks = []
    for t in tools:
        blocks.append(
            LyreContentBlock(
                type="tool_use",
                tool_use_id=t["id"],
                tool_name=t["name"],
                tool_input=t["input"],
            )
        )
    return LyreMessage(role="assistant", content=blocks)


def _tool_results(*results: dict[str, Any]) -> LyreMessage:
    blocks = []
    for r in results:
        blocks.append(
            LyreContentBlock(
                type="tool_result",
                tool_use_id=r["id"],
                tool_result=r.get("result", ""),
                is_error=r.get("is_error", False),
            )
        )
    return LyreMessage(role="user", content=blocks)


# ----------------------------------------------------------------------
# find_pivot
# ----------------------------------------------------------------------


def test_find_pivot_returns_index_of_kth_last_assistant() -> None:
    msgs = [
        _user("task"),                           # 0
        _assistant_with_tools({"id": "1", "name": "a", "input": {}}),   # 1
        _tool_results({"id": "1"}),              # 2
        _assistant_with_tools({"id": "2", "name": "a", "input": {}}),   # 3
        _tool_results({"id": "2"}),              # 4
        _assistant_with_tools({"id": "3", "name": "a", "input": {}}),   # 5
        _tool_results({"id": "3"}),              # 6
    ]
    # K=1 → last assistant is at index 5
    assert find_pivot(msgs, 1) == 5
    # K=2 → second-to-last assistant is at index 3
    assert find_pivot(msgs, 2) == 3
    # K=3 → captures the first assistant too
    assert find_pivot(msgs, 3) == 1


def test_find_pivot_returns_1_when_not_enough_assistants() -> None:
    """If we ask for more assistants than exist, pivot stays right after
    the initial user msg so we don't accidentally compact the task goal."""
    msgs = [
        _user("task"),
        _assistant_with_tools({"id": "1", "name": "a", "input": {}}),
        _tool_results({"id": "1"}),
    ]
    assert find_pivot(msgs, 5) == 1


# ----------------------------------------------------------------------
# compact_messages — structure
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compact_preserves_mailbox_get_message_as_user_msg() -> None:
    """The body of a mailbox_get_message tool_result must appear
    verbatim as a synthetic user message after compaction. This is the
    core Lyre invariant: owner / peer's words are quasi-user-input and
    don't get summarized."""
    mail_result = json.dumps({
        "id": 12, "sender": "owner", "urgency": "blocker",
        "title": "stop the bleeding",
        "body": "Please look at the wakeup loop bug ASAP.",
    })
    msgs = [
        _user("Check inbox"),
        _assistant_with_tools(
            {"id": "tu1", "name": "mailbox_get_message", "input": {"msg_id": 12}}
        ),
        _tool_results({"id": "tu1", "result": mail_result}),
        # Build out enough turns so there's something to elide
        _assistant_with_tools(
            {"id": "tu2", "name": "shell_exec", "input": {"argv": ["ls"]}}
        ),
        _tool_results({"id": "tu2", "result": "file1\nfile2"}),
        _assistant_with_tools(
            {"id": "tu3", "name": "shell_exec", "input": {"argv": ["pwd"]}}
        ),
        _tool_results({"id": "tu3", "result": "/tmp"}),
        # Last K=2 turns kept intact:
        _assistant_with_tools(
            {"id": "tu4", "name": "shell_exec", "input": {"argv": ["whoami"]}}
        ),
        _tool_results({"id": "tu4", "result": "root"}),
        _assistant_with_tools(
            {"id": "tu5", "name": "shell_exec", "input": {"argv": ["uptime"]}}
        ),
        _tool_results({"id": "tu5", "result": "12:00 up"}),
    ]
    adapter = StubAdapter(canned_summary="ran shell_exec×2 in /tmp, no failures")
    out = await compact_messages(
        msgs, adapter=adapter, model="x", keep_last_k=2, wakeup_id="wk-1",
    )

    # The owner's mail body must survive verbatim.
    body_texts = " ".join(
        blk.text or "" for m in out for blk in m.content if blk.type == "text"
    )
    assert "Please look at the wakeup loop bug ASAP." in body_texts
    # The synthetic mail message must be a USER role (not assistant)
    mail_msgs = [
        m for m in out
        if m.role == "user" and any(
            "Mail from owner" in (b.text or "") for b in m.content
        )
    ]
    assert len(mail_msgs) == 1, "exactly one synthetic mail user msg expected"
    # Sender / msg_id / urgency captured in the prefix header
    header = mail_msgs[0].content[0].text or ""
    assert "msg #12" in header
    assert "from owner" in header
    assert "blocker" in header


@pytest.mark.asyncio
async def test_compact_preserves_mailbox_send_as_assistant_msg() -> None:
    """mailbox_send body → synthetic assistant text msg. Captures the
    agent's own commitments across the compaction seam."""
    msgs = [
        _user("respond to owner"),
        _assistant_with_tools(
            {
                "id": "tu1",
                "name": "mailbox_send",
                "input": {
                    "to": "owner",
                    "reply_to": 12,
                    "body": "Got it, investigating now.",
                    "urgency": "high",
                },
            }
        ),
        _tool_results({"id": "tu1", "result": '{"status":"queued"}'}),
        _assistant_with_tools(
            {"id": "tu2", "name": "shell_exec", "input": {"argv": ["grep", "bug"]}}
        ),
        _tool_results({"id": "tu2", "result": "no match"}),
        # Kept last K=2:
        _assistant_with_tools(
            {"id": "tu3", "name": "shell_exec", "input": {"argv": ["echo", "hi"]}}
        ),
        _tool_results({"id": "tu3", "result": "hi"}),
        _assistant_with_tools(
            {"id": "tu4", "name": "shell_exec", "input": {"argv": ["echo", "done"]}}
        ),
        _tool_results({"id": "tu4", "result": "done"}),
    ]
    out = await compact_messages(
        msgs, adapter=StubAdapter(), model="x", keep_last_k=2,
    )
    # Find the synthetic assistant msg carrying the send body.
    send_msgs = [
        m for m in out
        if m.role == "assistant" and any(
            "Got it, investigating now." in (b.text or "")
            for b in m.content if b.type == "text"
        )
    ]
    assert len(send_msgs) == 1
    header = send_msgs[0].content[0].text or ""
    assert "to owner" in header
    assert "reply_to=#12" in header
    assert "urgency=high" in header


@pytest.mark.asyncio
async def test_compact_drops_idempotent_and_listing_tools() -> None:
    """list_agents / list_personas / mailbox_read / query_task_status etc.
    are idempotent or re-fetchable. They contribute zero useful info to
    the post-compact history and should be dropped entirely (not even
    appear in the work summary trace)."""
    msgs = [
        _user("get oriented"),
        _assistant_with_tools(
            {"id": "tu1", "name": "list_agents", "input": {}},
            {"id": "tu2", "name": "list_personas", "input": {}},
            {"id": "tu3", "name": "mailbox_read", "input": {}},
        ),
        _tool_results(
            {"id": "tu1", "result": '[{"id":"dispatcher"}]'},
            {"id": "tu2", "result": '[{"name":"dispatcher"}]'},
            {"id": "tu3", "result": '{"messages":[]}'},
        ),
        _assistant_with_tools(
            {"id": "tu4", "name": "shell_exec", "input": {"argv": ["pwd"]}}
        ),
        _tool_results({"id": "tu4", "result": "/tmp"}),
        _assistant_with_tools(
            {"id": "tu5", "name": "shell_exec", "input": {"argv": ["whoami"]}}
        ),
        _tool_results({"id": "tu5", "result": "root"}),
        _assistant_with_tools(
            {"id": "tu6", "name": "shell_exec", "input": {"argv": ["date"]}}
        ),
        _tool_results({"id": "tu6", "result": "now"}),
    ]
    adapter = StubAdapter(canned_summary="ran shell_exec a few times")
    await compact_messages(
        msgs, adapter=adapter, model="x", keep_last_k=2,
    )
    # The work-summary prompt MUST NOT mention the dropped tools.
    summary_prompt = adapter.calls[0]["messages"][0].content[0].text or ""
    assert "list_agents" not in summary_prompt
    assert "list_personas" not in summary_prompt
    assert "mailbox_read" not in summary_prompt


@pytest.mark.asyncio
async def test_compact_quotes_dispatch_task_id_verbatim() -> None:
    """Quoting dispatch_task task_ids verbatim is a contract — without
    them the agent can't query the subagent on the next wakeup."""
    msgs = [
        _user("delegate"),
        _assistant_with_tools(
            {
                "id": "tu1",
                "name": "dispatch_task",
                "input": {"agent": "worker-1", "goal": "fix the typo",
                          "acceptance": "PR merged"},
            }
        ),
        _tool_results({
            "id": "tu1",
            "result": '{"task_id":"019e4200-aaaa-7bcd-9999-feedface1234"}',
        }),
        _assistant_with_tools(
            {"id": "tu2", "name": "shell_exec", "input": {"argv": ["ls"]}}
        ),
        _tool_results({"id": "tu2", "result": "files"}),
        _assistant_with_tools(
            {"id": "tu3", "name": "shell_exec", "input": {"argv": ["pwd"]}}
        ),
        _tool_results({"id": "tu3", "result": "/"}),
    ]
    adapter = StubAdapter()
    await compact_messages(
        msgs, adapter=adapter, model="x", keep_last_k=2,
    )
    summary_prompt = adapter.calls[0]["messages"][0].content[0].text or ""
    assert "019e4200-aaaa-7bcd-9999-feedface1234" in summary_prompt


@pytest.mark.asyncio
async def test_compact_keeps_last_k_turns_intact() -> None:
    """Last K (assistant, tool_result) pairs must be IDENTICAL in the
    output — the model needs intact thinking blocks for the next API
    call's content[].thinking requirement, and last K turns are how we
    preserve them."""
    last_assistant = _assistant_with_tools(
        {"id": "keep_a", "name": "shell_exec", "input": {"argv": ["keep"]}}
    )
    last_result = _tool_results({"id": "keep_a", "result": "kept"})
    msgs = [
        _user("task"),
        _assistant_with_tools(
            {"id": "drop_a", "name": "shell_exec", "input": {"argv": ["drop"]}}
        ),
        _tool_results({"id": "drop_a", "result": "dropped"}),
        _assistant_with_tools(
            {"id": "drop_b", "name": "shell_exec", "input": {"argv": ["drop2"]}}
        ),
        _tool_results({"id": "drop_b", "result": "dropped"}),
        last_assistant,
        last_result,
    ]
    out = await compact_messages(
        msgs, adapter=StubAdapter(), model="x", keep_last_k=1,
    )
    # The last two messages must be byte-identical to the input.
    assert out[-2] is last_assistant
    assert out[-1] is last_result


@pytest.mark.asyncio
async def test_compact_bails_when_too_short_to_be_useful() -> None:
    """If there's not enough history to meaningfully elide anything,
    the function returns the input unchanged."""
    msgs = [
        _user("task"),
        _assistant_with_tools(
            {"id": "1", "name": "x", "input": {}}
        ),
        _tool_results({"id": "1"}),
    ]
    out = await compact_messages(
        msgs, adapter=StubAdapter(), model="x", keep_last_k=3,
    )
    assert out == msgs


@pytest.mark.asyncio
async def test_compact_chronological_order_preserved() -> None:
    """Synthetic mail-in / mail-out messages must appear in the SAME
    chronological order they did in the elided range. The model relies
    on this ordering to understand reply chains."""
    msgs = [
        _user("task"),
        # First mail in
        _assistant_with_tools(
            {"id": "g1", "name": "mailbox_get_message", "input": {"msg_id": 10}}
        ),
        _tool_results({"id": "g1", "result": json.dumps({
            "id": 10, "sender": "owner", "urgency": "high",
            "body": "FIRST mail content"
        })}),
        # Reply
        _assistant_with_tools(
            {"id": "s1", "name": "mailbox_send",
             "input": {"to": "owner", "body": "FIRST reply", "reply_to": 10}}
        ),
        _tool_results({"id": "s1", "result": '{"status":"queued"}'}),
        # Second mail in
        _assistant_with_tools(
            {"id": "g2", "name": "mailbox_get_message", "input": {"msg_id": 11}}
        ),
        _tool_results({"id": "g2", "result": json.dumps({
            "id": 11, "sender": "worker", "urgency": "normal",
            "body": "SECOND mail content"
        })}),
        # Last K=2 kept intact
        _assistant_with_tools(
            {"id": "p1", "name": "shell_exec", "input": {"argv": ["pwd"]}}
        ),
        _tool_results({"id": "p1", "result": "/"}),
        _assistant_with_tools(
            {"id": "p2", "name": "shell_exec", "input": {"argv": ["whoami"]}}
        ),
        _tool_results({"id": "p2", "result": "u"}),
    ]
    out = await compact_messages(
        msgs, adapter=StubAdapter(), model="x", keep_last_k=2,
    )
    flat = " ".join(
        b.text or "" for m in out for b in m.content if b.type == "text"
    )
    # FIRST mail / FIRST reply / SECOND mail must appear in that order
    p1 = flat.index("FIRST mail content")
    p2 = flat.index("FIRST reply")
    p3 = flat.index("SECOND mail content")
    assert p1 < p2 < p3
