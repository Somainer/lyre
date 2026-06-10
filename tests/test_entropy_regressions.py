"""Regression tests for the entropy-reduction stability fixes.

Each test pins a behavioral guarantee that an audit found was silently
broken (or untested). All offline — no provider keys, no network.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from anthropic.types import MessageDeltaEvent, MessageStartEvent, MessageStopEvent

from lyre.adapter.anthropic import AnthropicAdapter
from lyre.adapter.llm_adapter import TurnComplete, Usage
from lyre.outbox.dispatcher import OutboxDispatcher
from lyre.persistence.models import OutboxRow, Persona, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.agent_loop import AgentLoop
from lyre.runtime.health_tracker import HealthTracker
from lyre.runtime.tools import Tool, ToolContext, ToolRegistry
from lyre.runtime.transcript import TranscriptWriter

from .helpers import fake_entry

# ---------------------------------------------------------------------------
# #1 (critical): Anthropic adapter must read input_tokens from message_start.
#   The per-message_delta usage carries input_tokens=None on the wire, so
#   without this every turn reported input_tokens=0 and auto-compaction (and
#   context_peak_tokens) silently never fired.
# ---------------------------------------------------------------------------


def _fake_event(cls: type) -> MagicMock:
    """A MagicMock whose ``__class__`` is a real anthropic SDK event type so
    the adapter's ``isinstance`` dispatch matches, while leaving every (pydantic
    instance) field freely settable — ``spec=`` can't, since pydantic fields are
    instance attributes, not class attributes."""
    m = MagicMock()
    m.__class__ = cls
    return m


def test_anthropic_input_tokens_come_from_message_start() -> None:
    holder: dict[str, int] = {"input_tokens": 0}

    start = _fake_event(MessageStartEvent)
    start.message.usage.input_tokens = 12345
    emitted = AnthropicAdapter._anthropic_to_lyre(start, {}, {}, holder)
    # message_start only records the count; it emits no stream event.
    assert emitted is None
    assert holder["input_tokens"] == 12345

    delta = _fake_event(MessageDeltaEvent)
    delta.delta.stop_reason = "end_turn"
    delta.usage.output_tokens = 50
    delta.usage.input_tokens = None  # the actual wire value
    usage = AnthropicAdapter._anthropic_to_lyre(delta, {}, {}, holder)
    assert isinstance(usage, Usage)
    assert usage.input_tokens == 12345  # from message_start, NOT 0
    assert usage.output_tokens == 50


def test_anthropic_usage_falls_back_to_delta_then_zero_without_message_start() -> None:
    """No regression for compat endpoints that omit a usable message_start:
    a delta whose own usage carries input_tokens is honored as a fallback;
    if neither source has a value it degrades to 0 (today's behavior)."""
    # (a) fallback to the delta-supplied count when there was no message_start.
    holder: dict[str, int] = {"input_tokens": 0}
    delta = _fake_event(MessageDeltaEvent)
    delta.delta.stop_reason = None
    delta.usage.output_tokens = 7
    delta.usage.input_tokens = 99  # compat endpoint populated the delta
    usage = AnthropicAdapter._anthropic_to_lyre(delta, {}, {}, holder)
    assert isinstance(usage, Usage)
    assert usage.input_tokens == 99
    assert usage.output_tokens == 7

    # (b) the actual zero floor: no message_start AND the delta's input_tokens
    # is None (the real wire value) -> 0, never None.
    holder2: dict[str, int] = {"input_tokens": 0}
    delta2 = _fake_event(MessageDeltaEvent)
    delta2.delta.stop_reason = None
    delta2.usage.output_tokens = 3
    delta2.usage.input_tokens = None
    usage2 = AnthropicAdapter._anthropic_to_lyre(delta2, {}, {}, holder2)
    assert isinstance(usage2, Usage)
    assert usage2.input_tokens == 0
    assert usage2.output_tokens == 3


# ---------------------------------------------------------------------------
# Anthropic adapter must surface the REAL stop_reason. The SDK's
# message_delta usage is a required field, so the Usage branch always wins
# and the stop_reason can only travel via the stash → message_stop path.
# Before the stash existed every Anthropic turn surfaced as end_turn and a
# max_tokens-truncated final response was classified 'completed' (A2 defeat).
# ---------------------------------------------------------------------------


def test_anthropic_stop_reason_survives_to_message_stop() -> None:
    holder: dict[str, Any] = {"input_tokens": 0}

    delta = _fake_event(MessageDeltaEvent)
    delta.delta.stop_reason = "max_tokens"
    delta.usage.output_tokens = 11
    delta.usage.input_tokens = None
    emitted = AnthropicAdapter._anthropic_to_lyre(delta, {}, {}, holder)
    # The delta still emits Usage (required field wins) — never TurnComplete.
    assert isinstance(emitted, Usage)

    stop = _fake_event(MessageStopEvent)
    done = AnthropicAdapter._anthropic_to_lyre(stop, {}, {}, holder)
    assert isinstance(done, TurnComplete)
    assert done.stop_reason == "max_tokens"


def test_anthropic_stop_reason_defaults_to_end_turn_when_never_sent() -> None:
    holder: dict[str, Any] = {"input_tokens": 0}
    stop = _fake_event(MessageStopEvent)
    done = AnthropicAdapter._anthropic_to_lyre(stop, {}, {}, holder)
    assert isinstance(done, TurnComplete)
    assert done.stop_reason == "end_turn"


def test_anthropic_stop_reason_maps_provider_vocabulary() -> None:
    """stop_sequence is a clean stop; refusal is error-shaped; unknown
    compat-endpoint values degrade to end_turn instead of leaking raw
    strings into the loop's StopReason literal."""
    for wire, expected in [
        ("stop_sequence", "end_turn"),
        ("refusal", "error"),
        ("tool_use", "tool_use"),
        ("model_context_window_exceeded", "max_tokens"),
        ("some-future-compat-value", "end_turn"),
    ]:
        holder: dict[str, Any] = {"input_tokens": 0}
        delta = _fake_event(MessageDeltaEvent)
        delta.delta.stop_reason = wire
        delta.usage.output_tokens = 1
        delta.usage.input_tokens = None
        AnthropicAdapter._anthropic_to_lyre(delta, {}, {}, holder)
        stop = _fake_event(MessageStopEvent)
        done = AnthropicAdapter._anthropic_to_lyre(stop, {}, {}, holder)
        assert isinstance(done, TurnComplete)
        assert done.stop_reason == expected, wire


# ---------------------------------------------------------------------------
# #2 (high): _dispatch_tool must drain `_lyre_view_blocks` off a dict result
#   BEFORE serializing, so (a) the image/document blocks are hydrated and
#   (b) the internal magic key never leaks into the JSON the model reads.
# ---------------------------------------------------------------------------


def _loop_with_tool(tmp_path: Path, tool: Tool) -> AgentLoop:
    object_store = tmp_path / "objstore"
    object_store.mkdir()
    transcript = TranscriptWriter(object_store, "wakeup-view")
    registry = ToolRegistry()
    registry.register(tool)
    ctx = ToolContext(
        repos=None,  # type: ignore[arg-type]
        task_id="t", wakeup_id="w",
        persona_name="worker-maintainer", agent_id="worker-maintainer/x",
    )
    return AgentLoop(
        candidates=[fake_entry(id="a.flagship", tier="flagship")],
        adapter_for=lambda e: None,  # type: ignore[arg-type, return-value]
        model_name_for=lambda e: e.id,
        transcript=transcript,
        tool_registry=registry,
        tool_context=ctx,
        allowed_tools=[tool.name],
        health=HealthTracker(),
    )


@pytest.mark.asyncio
async def test_dispatch_tool_drains_view_blocks_and_strips_magic_key(
    tmp_path: Path,
) -> None:
    async def handler(_ctx: ToolContext, _args: dict[str, Any]) -> dict[str, Any]:
        return {
            "body": "see attached",
            "_lyre_view_blocks": [
                {
                    "type": "image",
                    "blob_id": "blob-1",
                    "media_type": "image/png",
                    "filename": "shot.png",
                }
            ],
        }

    tool = Tool(
        name="fake_mail_get",
        description="returns a dict with view blocks",
        input_schema={"type": "object", "properties": {}},
        handler=handler,
    )
    loop = _loop_with_tool(tmp_path, tool)

    result, is_error, view_blocks = await loop._dispatch_tool(
        "fake_mail_get", "tu_1", {}
    )

    assert is_error is False
    # The magic key must NOT leak into the model-visible JSON tool_result.
    assert "_lyre_view_blocks" not in result
    assert "see attached" in result
    # The image block is hydrated and handed back to the loop.
    assert len(view_blocks) == 1
    assert view_blocks[0].type == "image"
    assert view_blocks[0].blob_id == "blob-1"


# ---------------------------------------------------------------------------
# #12 (medium): a poison (repeatedly-failing) outbox row must sink behind
#   fresh deliverable mail so it can't starve newer rows out of the batch.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dequeue_orders_failed_rows_behind_fresh_mail(
    repos: SqliteRepositories,
) -> None:
    # An OLDER poison row that has already failed several times...
    poison = OutboxRow(
        task_id=None, wakeup_id=None, kind="channel_publish",
        payload={"to": "dead-channel"}, external_id="poison-1",
    )
    await repos.outbox.enqueue([poison])
    poison_id = (await repos.outbox.dequeue_batch(limit=1))[0].id
    assert poison_id is not None
    for _ in range(3):
        await repos.outbox.mark_failed(poison_id, "boom")

    # ...and a NEWER, never-tried, deliverable row.
    fresh = OutboxRow(
        task_id=None, wakeup_id=None, kind="mailbox_send",
        payload={"recipient": "x", "body": "hi"}, external_id="fresh-1",
    )
    await repos.outbox.enqueue([fresh])

    # With a batch limit of 1, the fresh (0-attempt) row must come first even
    # though the poison row is older, because dispatch_attempts now leads the
    # sort order — otherwise the poison row starves the batch forever.
    batch = await repos.outbox.dequeue_batch(limit=1)
    assert [r.external_id for r in batch] == ["fresh-1"]


# ---------------------------------------------------------------------------
# git_context._run timeout: a stalled subprocess must surface as a
#   GitContextError quickly, not pin the single-threaded event loop on an
#   unbounded post-kill reap (cross-review follow-up to the timeout fix).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_git_context_run_times_out_without_hanging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("sleep") is None:
        pytest.skip("requires /bin/sleep")
    from lyre.runtime import git_context

    monkeypatch.setattr(git_context, "_SUBPROCESS_TIMEOUT_S", 0.2)
    started = time.monotonic()
    with pytest.raises(git_context.GitContextError):
        await git_context._run(["sleep", "5"])
    # Surfaced as a clean error well within the child's 5s sleep — i.e. the
    # timeout + bounded reap fired instead of blocking on communicate().
    assert time.monotonic() - started < 3.0


# ---------------------------------------------------------------------------
# #11: the outbox dispatcher must carry the agent-supplied mail title onto the
#   delivered message (it used to drop it, so insert_message re-derived the
#   title from the body — the recipient saw the wrong subject).
# ---------------------------------------------------------------------------


async def _seed_worker(repos: SqliteRepositories) -> tuple[str, str]:
    await repos.personas.upsert(
        Persona(name="worker", role_description="w", system_prompt="w")
    )
    task_id = await repos.tasks.create(
        TaskSpec(persona_name="worker", goal="g", acceptance="a")
    )
    wakeup_id = await repos.wakeups.start(task_id, "worker")
    await repos.mailbox.ensure_mailbox("owner")
    return task_id, wakeup_id


@pytest.mark.asyncio
async def test_dispatcher_preserves_explicit_title(
    repos: SqliteRepositories,
) -> None:
    task_id, wakeup_id = await _seed_worker(repos)
    await repos.outbox.enqueue([
        OutboxRow(
            task_id=task_id, wakeup_id=wakeup_id, kind="mailbox_send",
            payload={
                "recipient": "owner", "sender": "worker", "urgency": "normal",
                "title": "PR #123 ready",
                "body": "a completely different first line\nmore body",
                "external_id": f"{wakeup_id}:tu_1",
            },
            external_id=f"{wakeup_id}:tu_1",
        )
    ])
    assert await OutboxDispatcher(repos, poll_interval_s=0.01).tick() == 1
    msgs = await repos.mailbox.read_messages("owner")
    assert len(msgs) == 1
    # Explicit title preserved — NOT re-derived from the body's first line.
    assert msgs[0].title == "PR #123 ready"


@pytest.mark.asyncio
async def test_dispatcher_derives_title_from_body_when_absent(
    repos: SqliteRepositories,
) -> None:
    task_id, wakeup_id = await _seed_worker(repos)
    await repos.outbox.enqueue([
        OutboxRow(
            task_id=task_id, wakeup_id=wakeup_id, kind="mailbox_send",
            payload={
                "recipient": "owner", "sender": "worker", "urgency": "normal",
                "body": "first line becomes the title\nrest",
                "external_id": f"{wakeup_id}:tu_2",
            },
            external_id=f"{wakeup_id}:tu_2",
        )
    ])
    assert await OutboxDispatcher(repos, poll_interval_s=0.01).tick() == 1
    msgs = await repos.mailbox.read_messages("owner")
    # None title still derives from the body's first non-empty line (unchanged).
    assert msgs[0].title == "first line becomes the title"
