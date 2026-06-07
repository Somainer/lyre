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


async def _compact(*args: Any, **kwargs: Any) -> list[LyreMessage]:
    """Shim: most tests only assert on the resulting message list, not the
    degraded flag — unwrap the CompactionOutcome for them."""
    return (await compact_messages(*args, **kwargs)).messages


class FailingAdapter:
    """An adapter whose summarizer turn always raises — exercises the
    work-summary LLM failure → raw-trace fallback (degraded) path."""

    async def stream_turn(
        self,
        messages: list[LyreMessage],
        tools: list[LyreToolSpec],
        model: str,
        max_tokens: int = 4096,
        temperature: float | None = None,
        system: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        raise RuntimeError("summarizer down")
        yield TurnComplete(stop_reason="end_turn")  # pragma: no cover


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
    out = await _compact(
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
    out = await _compact(
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
    await _compact(
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
    await _compact(
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
    out = await _compact(
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
    out = await _compact(
        msgs, adapter=StubAdapter(), model="x", keep_last_k=3,
    )
    assert out == msgs


@pytest.mark.asyncio
async def test_compaction_marks_its_own_output_as_artifact() -> None:
    """Synthetic mail messages + the work-summary seam must be flagged
    `compaction_artifact=True` so a later compaction recognizes its own
    output. The kept head/tail (real turns) must NOT be flagged."""
    mail_result = json.dumps({
        "id": 5, "sender": "owner", "urgency": "high", "body": "keep me",
    })
    msgs = [
        _user("task"),
        _assistant_with_tools(
            {"id": "g1", "name": "mailbox_get_message", "input": {"msg_id": 5}}
        ),
        _tool_results({"id": "g1", "result": mail_result}),
        _assistant_with_tools(
            {"id": "a1", "name": "shell_exec", "input": {"argv": ["ls"]}}
        ),
        _tool_results({"id": "a1", "result": "x"}),
        _assistant_with_tools(
            {"id": "a2", "name": "shell_exec", "input": {"argv": ["pwd"]}}
        ),
        _tool_results({"id": "a2", "result": "/"}),
        _assistant_with_tools(
            {"id": "a3", "name": "shell_exec", "input": {"argv": ["id"]}}
        ),
        _tool_results({"id": "a3", "result": "u"}),
    ]
    out = await _compact(
        msgs, adapter=StubAdapter(), model="x", keep_last_k=2,
    )
    artifacts = [m for m in out if m.compaction_artifact]
    # The synthetic mail (user) + the summary seam (user) are artifacts.
    assert len(artifacts) >= 2
    assert all(m.compaction_artifact for m in artifacts)
    # The initial task msg and the kept-tail real turns are NOT artifacts.
    assert out[0].compaction_artifact is False
    assert out[-1].compaction_artifact is False
    assert out[-2].compaction_artifact is False


@pytest.mark.asyncio
async def test_recompaction_preserves_mail_verbatim() -> None:
    """REGRESSION: a SECOND compaction must not destroy mail that the
    FIRST compaction preserved. The synthetic mail message carries no
    tool_use block, so the naive elision walk produces nothing for it and
    silently drops the owner's words — exactly the kill-test / 铁律五
    violation this guards against."""
    mail_result = json.dumps({
        "id": 7, "sender": "owner", "urgency": "blocker",
        "body": "DO NOT LOSE THIS owner instruction.",
    })
    msgs = [
        _user("start"),
        _assistant_with_tools(
            {"id": "g1", "name": "mailbox_get_message", "input": {"msg_id": 7}}
        ),
        _tool_results({"id": "g1", "result": mail_result}),
        _assistant_with_tools(
            {"id": "a1", "name": "shell_exec", "input": {"argv": ["ls"]}}
        ),
        _tool_results({"id": "a1", "result": "x"}),
        _assistant_with_tools(
            {"id": "a2", "name": "shell_exec", "input": {"argv": ["pwd"]}}
        ),
        _tool_results({"id": "a2", "result": "/"}),
        _assistant_with_tools(
            {"id": "a3", "name": "shell_exec", "input": {"argv": ["id"]}}
        ),
        _tool_results({"id": "a3", "result": "u"}),
    ]
    first = await _compact(
        msgs, adapter=StubAdapter(), model="x", keep_last_k=2,
    )
    flat1 = " ".join(
        b.text or "" for m in first for b in m.content if b.type == "text"
    )
    assert "DO NOT LOSE THIS owner instruction." in flat1  # sanity

    # Simulate more turns accruing after the first compaction, then a
    # second compaction (the case `_MAX_COMPACTIONS` used to paper over).
    extended = first + [
        _assistant_with_tools(
            {"id": "b1", "name": "shell_exec", "input": {"argv": ["echo", "1"]}}
        ),
        _tool_results({"id": "b1", "result": "1"}),
        _assistant_with_tools(
            {"id": "b2", "name": "shell_exec", "input": {"argv": ["echo", "2"]}}
        ),
        _tool_results({"id": "b2", "result": "2"}),
    ]
    second = await _compact(
        extended, adapter=StubAdapter(), model="x", keep_last_k=2,
    )
    flat2 = " ".join(
        b.text or "" for m in second for b in m.content if b.type == "text"
    )
    assert "DO NOT LOSE THIS owner instruction." in flat2, (
        "recompaction destroyed mail preserved by the first compaction"
    )


@pytest.mark.asyncio
async def test_recompaction_does_not_accrete_empty_summary_seams() -> None:
    """A recompaction whose only fresh content is mail (no tool work to
    fold) should carry the prior seam forward, NOT append a fresh empty
    'no substantive tool work' marker on top — otherwise every compaction
    grows the history by one useless seam. Built from mail-only turns so
    the fresh range has zero trace-policy tool work."""
    def _mail_in(msg_id: int, body: str) -> tuple[LyreMessage, LyreMessage]:
        return (
            _assistant_with_tools(
                {"id": f"g{msg_id}", "name": "mailbox_get_message",
                 "input": {"msg_id": msg_id}}
            ),
            _tool_results({"id": f"g{msg_id}", "result": json.dumps(
                {"id": msg_id, "sender": "owner", "urgency": "normal",
                 "body": body}
            )}),
        )

    def _mail_out(send_id: str, body: str) -> tuple[LyreMessage, LyreMessage]:
        return (
            _assistant_with_tools(
                {"id": send_id, "name": "mailbox_send",
                 "input": {"to": "owner", "body": body}}
            ),
            _tool_results({"id": send_id, "result": '{"status":"queued"}'}),
        )

    g1a, g1r = _mail_in(1, "first")
    s1a, s1r = _mail_out("s1", "ack first")
    g2a, g2r = _mail_in(2, "second")
    msgs = [_user("task"), g1a, g1r, s1a, s1r, g2a, g2r]
    first = await _compact(
        msgs, adapter=StubAdapter(), model="x", keep_last_k=1,
    )
    seams_first = [
        m for m in first
        if any("[Compact summary" in (b.text or "") for b in m.content)
    ]
    assert len(seams_first) == 1

    # Extend with ONLY new mail turns (no shell/python work), recompact.
    s2a, s2r = _mail_out("s2", "ack second")
    g3a, g3r = _mail_in(3, "third")
    extended = first + [s2a, s2r, g3a, g3r]
    second = await _compact(
        extended, adapter=StubAdapter(), model="x", keep_last_k=1,
    )
    seams_second = [
        m for m in second
        if any("[Compact summary" in (b.text or "") for b in m.content)
    ]
    # The single prior seam is carried forward; no fresh empty seam added.
    assert len(seams_second) == 1


@pytest.mark.asyncio
async def test_compaction_flags_summary_degraded_when_llm_call_fails() -> None:
    """When the work-summary LLM call fails, compaction still returns a
    usable (shorter) history via the raw-trace fallback — but flags
    `summary_degraded` so the lossy compaction is observable (RB-2). A
    successful summary leaves the flag False."""
    msgs = [
        _user("task"),
        _assistant_with_tools(
            {"id": "a1", "name": "shell_exec", "input": {"argv": ["make"]}}
        ),
        _tool_results({"id": "a1", "result": "built OK"}),
        _assistant_with_tools(
            {"id": "a2", "name": "shell_exec", "input": {"argv": ["pytest"]}}
        ),
        _tool_results({"id": "a2", "result": "42 passed"}),
        _assistant_with_tools(
            {"id": "a3", "name": "shell_exec", "input": {"argv": ["ls"]}}
        ),
        _tool_results({"id": "a3", "result": "files"}),
    ]
    degraded = await compact_messages(
        msgs, adapter=FailingAdapter(), model="x", keep_last_k=2,
    )
    assert degraded.summary_degraded is True
    # The raw trace is still inlined so the work isn't lost outright.
    flat = " ".join(
        b.text or "" for m in degraded.messages
        for b in m.content if b.type == "text"
    )
    assert "Tool actions during elided turns" in flat

    ok = await compact_messages(
        msgs, adapter=StubAdapter(canned_summary="built + tested clean"),
        model="x", keep_last_k=2,
    )
    assert ok.summary_degraded is False


@pytest.mark.asyncio
async def test_compaction_not_degraded_when_only_mail_elided() -> None:
    """A compaction whose elided range has no tool work emits the minimal
    seam without an LLM call — that's not a degradation."""
    msgs = [
        _user("task"),
        _assistant_with_tools(
            {"id": "g1", "name": "mailbox_get_message", "input": {"msg_id": 1}}
        ),
        _tool_results({"id": "g1", "result": json.dumps(
            {"id": 1, "sender": "owner", "urgency": "high", "body": "hi"}
        )}),
        _assistant_with_tools(
            {"id": "s1", "name": "mailbox_send",
             "input": {"to": "owner", "body": "ok"}}
        ),
        _tool_results({"id": "s1", "result": '{"status":"queued"}'}),
        _assistant_with_tools(
            {"id": "g2", "name": "mailbox_get_message", "input": {"msg_id": 2}}
        ),
        _tool_results({"id": "g2", "result": json.dumps(
            {"id": 2, "sender": "owner", "urgency": "high", "body": "more"}
        )}),
    ]
    out = await compact_messages(
        msgs, adapter=FailingAdapter(), model="x", keep_last_k=1,
    )
    assert out.summary_degraded is False


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
    out = await _compact(
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
