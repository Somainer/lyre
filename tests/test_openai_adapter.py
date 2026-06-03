"""OpenAIAdapter tests.

Two layers:

1. **Message conversion** (pure, no API): verify
   `_lyre_to_openai_messages` and `_tool_to_openai` produce exactly the
   shape OpenAI / OAI-compat endpoints expect. Tool results have to
   become `role="tool"` messages; assistant tool_use blocks have to
   become `assistant.tool_calls[]`; thinking blocks must be dropped on
   replay (providers don't sign them and reject if we send them back).

2. **Stream parsing** (mocked SDK): drive `stream_turn` with a fake
   AsyncOpenAI client that yields hand-crafted chunks. Verify the
   adapter emits the canonical Lyre StreamEvent sequence — including
   accumulating fragmented tool_use arguments and surfacing
   reasoning_content as ThinkingDelta + ThinkingBlockComplete.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from lyre.adapter.llm_adapter import (
    ContentDelta,
    LyreContentBlock,
    LyreMessage,
    LyreToolSpec,
    ThinkingBlockComplete,
    ThinkingDelta,
    ToolUseComplete,
    ToolUseDelta,
    ToolUseStart,
    TurnComplete,
    Usage,
)
from lyre.adapter.openai import OpenAIAdapter

# ---------------------------------------------------------------------------
# 1. Conversion: Lyre -> OpenAI
# ---------------------------------------------------------------------------


def test_system_prompt_becomes_role_system_message() -> None:
    out = OpenAIAdapter._lyre_to_openai_messages([], system="be brief")
    assert out == [{"role": "system", "content": "be brief"}]


def test_assistant_text_round_trips_as_string_content() -> None:
    msgs = [
        LyreMessage(role="user", content=[LyreContentBlock(type="text", text="hi")]),
        LyreMessage(
            role="assistant",
            content=[LyreContentBlock(type="text", text="hello back")],
        ),
    ]
    out = OpenAIAdapter._lyre_to_openai_messages(msgs, system=None)
    assert out == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello back"},
    ]


def test_assistant_tool_use_becomes_tool_calls_array() -> None:
    """Lyre's assistant message with tool_use blocks must become an
    `assistant` message with a `tool_calls` array, NOT a content block.
    `arguments` is stringified JSON (OpenAI requirement)."""
    msgs = [
        LyreMessage(
            role="assistant",
            content=[
                LyreContentBlock(type="text", text="calling tool"),
                LyreContentBlock(
                    type="tool_use",
                    tool_use_id="call_abc",
                    tool_name="shell_exec",
                    tool_input={"argv": ["ls", "-la"]},
                ),
            ],
        )
    ]
    out = OpenAIAdapter._lyre_to_openai_messages(msgs, system=None)
    assert len(out) == 1
    msg = out[0]
    assert msg["role"] == "assistant"
    assert msg["content"] == "calling tool"
    assert len(msg["tool_calls"]) == 1
    tc = msg["tool_calls"][0]
    assert tc["id"] == "call_abc"
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "shell_exec"
    # arguments MUST be a string (JSON-encoded) — OpenAI rejects dicts
    assert isinstance(tc["function"]["arguments"], str)
    assert json.loads(tc["function"]["arguments"]) == {"argv": ["ls", "-la"]}


def test_tool_use_only_assistant_has_content_null() -> None:
    """OAI requires `content` to be present (string or null) on
    assistant. With tool_calls but no text, content=null is the
    portable choice (empty string is rejected by some OAI-compat hosts
    like DeepSeek)."""
    msgs = [
        LyreMessage(
            role="assistant",
            content=[
                LyreContentBlock(
                    type="tool_use", tool_use_id="t1",
                    tool_name="x", tool_input={},
                ),
            ],
        )
    ]
    out = OpenAIAdapter._lyre_to_openai_messages(msgs, system=None)
    assert out[0]["content"] is None
    assert out[0]["tool_calls"][0]["function"]["name"] == "x"


def test_tool_result_becomes_separate_role_tool_message() -> None:
    """Lyre encodes tool results as a `user` message with tool_result
    blocks. OpenAI requires a SEPARATE message with role="tool" per
    result, addressed by `tool_call_id`."""
    msgs = [
        LyreMessage(
            role="user",
            content=[
                LyreContentBlock(
                    type="tool_result",
                    tool_use_id="call_abc",
                    tool_result='{"status":"ok"}',
                    is_error=False,
                ),
            ],
        )
    ]
    out = OpenAIAdapter._lyre_to_openai_messages(msgs, system=None)
    assert out == [
        {
            "role": "tool",
            "tool_call_id": "call_abc",
            "content": '{"status":"ok"}',
        }
    ]


def test_mixed_tool_result_and_user_text_emits_tool_first_then_user() -> None:
    """Tool results MUST immediately follow the assistant that called
    them. If a user message contains both tool_result and text, the
    tool messages come first, then a separate user text message."""
    msgs = [
        LyreMessage(
            role="user",
            content=[
                LyreContentBlock(
                    type="tool_result", tool_use_id="t1",
                    tool_result="hello",
                ),
                LyreContentBlock(
                    type="text", text="(silent-turn nudge body here)",
                ),
            ],
        )
    ]
    out = OpenAIAdapter._lyre_to_openai_messages(msgs, system=None)
    assert len(out) == 2
    assert out[0]["role"] == "tool"
    assert out[0]["tool_call_id"] == "t1"
    assert out[1]["role"] == "user"
    assert "nudge" in out[1]["content"]


def test_non_string_tool_result_is_json_serialized() -> None:
    msgs = [
        LyreMessage(
            role="user",
            content=[
                LyreContentBlock(
                    type="tool_result", tool_use_id="t",
                    tool_result={"a": 1, "b": [2, 3]},
                ),
            ],
        )
    ]
    out = OpenAIAdapter._lyre_to_openai_messages(msgs, system=None)
    assert out[0]["content"] == '{"a": 1, "b": [2, 3]}'


def test_thinking_block_is_dropped_on_replay() -> None:
    """OAI-compat providers don't sign thinking. We keep thinking in
    the Lyre transcript for debug, but we MUST NOT echo it back to
    OAI — most upstreams reject unknown fields on assistant messages.
    """
    msgs = [
        LyreMessage(
            role="assistant",
            content=[
                LyreContentBlock(
                    type="thinking",
                    text="Let me reason...",
                    signature="sig-abc",
                ),
                LyreContentBlock(type="text", text="final answer"),
            ],
        )
    ]
    out = OpenAIAdapter._lyre_to_openai_messages(msgs, system=None)
    # Only the assistant message; no thinking field in it.
    assert len(out) == 1
    assert out[0]["role"] == "assistant"
    assert out[0]["content"] == "final answer"
    assert "thinking" not in out[0]
    assert "reasoning_content" not in out[0]


def test_tool_to_openai_uses_function_wrapper() -> None:
    """Lyre's `LyreToolSpec(name, description, input_schema)` must
    become OpenAI's `{type: function, function: {...}}` shape."""
    spec = LyreToolSpec(
        name="mailbox_send",
        description="Email another agent",
        input_schema={
            "type": "object",
            "properties": {"to": {"type": "string"}},
            "required": ["to"],
        },
    )
    out = OpenAIAdapter._tool_to_openai(spec)
    assert out == {
        "type": "function",
        "function": {
            "name": "mailbox_send",
            "description": "Email another agent",
            "parameters": {
                "type": "object",
                "properties": {"to": {"type": "string"}},
                "required": ["to"],
            },
        },
    }


# ---------------------------------------------------------------------------
# 2. Stream parsing
# ---------------------------------------------------------------------------


def _make_chunk(
    *,
    content: str | None = None,
    reasoning: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str | None = None,
    usage: dict[str, int] | None = None,
) -> MagicMock:
    """Build a mock ChatCompletionChunk matching the OpenAI SDK shape."""
    chunk = MagicMock()
    if usage is not None:
        chunk.usage = MagicMock(
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
        )
    else:
        chunk.usage = None

    if content is None and reasoning is None and tool_calls is None and finish_reason is None:
        chunk.choices = []
        return chunk

    delta = MagicMock()
    delta.content = content
    delta.reasoning_content = reasoning
    if tool_calls is not None:
        delta_tool_calls = []
        for tc in tool_calls:
            tc_mock = MagicMock()
            tc_mock.index = tc.get("index", 0)
            tc_mock.id = tc.get("id")
            if "function" in tc:
                fn = MagicMock()
                fn.name = tc["function"].get("name")
                fn.arguments = tc["function"].get("arguments")
                tc_mock.function = fn
            else:
                tc_mock.function = None
            delta_tool_calls.append(tc_mock)
        delta.tool_calls = delta_tool_calls
    else:
        delta.tool_calls = None

    choice = MagicMock()
    choice.delta = delta
    choice.finish_reason = finish_reason
    chunk.choices = [choice]
    return chunk


class _FakeStream:
    def __init__(self, chunks: list[Any]):
        self._chunks = chunks

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._iter()

    async def _iter(self):
        for c in self._chunks:
            yield c

    # Mirror the real openai AsyncStream, which is an async context
    # manager (the adapter now consumes it via `async with`).
    async def __aenter__(self) -> _FakeStream:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


def _attach_fake_stream(adapter: OpenAIAdapter, chunks: list[Any]) -> None:
    """Replace the SDK client with one that returns `_FakeStream(chunks)`."""
    adapter.client = MagicMock()
    adapter.client.chat = MagicMock()
    adapter.client.chat.completions = MagicMock()
    adapter.client.chat.completions.create = AsyncMock(
        return_value=_FakeStream(chunks)
    )


@pytest.mark.asyncio
async def test_stream_emits_content_deltas_and_turn_complete() -> None:
    """Simplest path: text-only response → ContentDelta(s) + Usage +
    TurnComplete(stop_reason=end_turn)."""
    adapter = OpenAIAdapter(api_key="x", base_url=None)
    _attach_fake_stream(
        adapter,
        [
            _make_chunk(content="Hello"),
            _make_chunk(content=" world"),
            _make_chunk(
                finish_reason="stop",
                usage={"prompt_tokens": 5, "completion_tokens": 2},
            ),
        ],
    )
    events = []
    async for evt in adapter.stream_turn(
        messages=[
            LyreMessage(role="user", content=[
                LyreContentBlock(type="text", text="hi"),
            ]),
        ],
        tools=[], model="gpt-x", system=None,
    ):
        events.append(evt)
    deltas = [e for e in events if isinstance(e, ContentDelta)]
    assert [d.text for d in deltas] == ["Hello", " world"]
    assert any(isinstance(e, Usage) and e.input_tokens == 5 and e.output_tokens == 2 for e in events)
    assert isinstance(events[-1], TurnComplete)
    assert events[-1].stop_reason == "end_turn"


@pytest.mark.asyncio
async def test_stream_assembles_fragmented_tool_call_arguments() -> None:
    """OpenAI streams tool args piecewise — must reassemble per-index
    and emit ToolUseStart (once name known) + ToolUseDelta (each
    fragment) + ToolUseComplete (with parsed JSON dict) at finish."""
    adapter = OpenAIAdapter(api_key="x", base_url=None)
    _attach_fake_stream(
        adapter,
        [
            # First fragment: id + name + start of args
            _make_chunk(tool_calls=[{
                "index": 0, "id": "call_xyz",
                "function": {"name": "shell_exec", "arguments": '{"argv"'},
            }]),
            # Mid: more args
            _make_chunk(tool_calls=[{
                "index": 0,
                "function": {"arguments": ': ["ls"'},
            }]),
            # End: closing args
            _make_chunk(tool_calls=[{
                "index": 0,
                "function": {"arguments": ']}'},
            }]),
            # Finish
            _make_chunk(
                finish_reason="tool_calls",
                usage={"prompt_tokens": 10, "completion_tokens": 4},
            ),
        ],
    )
    events = []
    async for evt in adapter.stream_turn(
        messages=[], tools=[], model="gpt-x", system=None,
    ):
        events.append(evt)
    starts = [e for e in events if isinstance(e, ToolUseStart)]
    deltas = [e for e in events if isinstance(e, ToolUseDelta)]
    completes = [e for e in events if isinstance(e, ToolUseComplete)]
    assert len(starts) == 1
    assert starts[0].id == "call_xyz" and starts[0].name == "shell_exec"
    # Three arg fragments → three deltas
    assert len(deltas) == 3
    assert "".join(d.input_partial for d in deltas) == '{"argv": ["ls"]}'
    # One Complete with parsed dict
    assert len(completes) == 1
    assert completes[0].input == {"argv": ["ls"]}
    # Stop reason mapped: tool_calls → tool_use
    assert isinstance(events[-1], TurnComplete)
    assert events[-1].stop_reason == "tool_use"


@pytest.mark.asyncio
async def test_stream_surfaces_reasoning_content_as_thinking_events() -> None:
    """DeepSeek-Reasoner and similar OAI-compat routes emit
    `delta.reasoning_content` for CoT. Adapter must surface each
    fragment as a ThinkingDelta AND emit a single
    ThinkingBlockComplete at the end (parity with Anthropic
    extended-thinking)."""
    adapter = OpenAIAdapter(api_key="x", base_url=None)
    _attach_fake_stream(
        adapter,
        [
            _make_chunk(reasoning="Let me think. "),
            _make_chunk(reasoning="Yes."),
            _make_chunk(content="answer"),
            _make_chunk(
                finish_reason="stop",
                usage={"prompt_tokens": 3, "completion_tokens": 1},
            ),
        ],
    )
    events = []
    async for evt in adapter.stream_turn(
        messages=[], tools=[], model="x", system=None,
    ):
        events.append(evt)
    thinking_deltas = [e for e in events if isinstance(e, ThinkingDelta)]
    blocks = [e for e in events if isinstance(e, ThinkingBlockComplete)]
    assert [t.text for t in thinking_deltas] == ["Let me think. ", "Yes."]
    assert len(blocks) == 1
    assert blocks[0].text == "Let me think. Yes."
    # OAI-compat providers don't sign — signature stays None
    assert blocks[0].signature is None


@pytest.mark.asyncio
async def test_stream_handles_unparseable_tool_args_gracefully() -> None:
    """If the model emits malformed JSON for arguments, we must not
    crash — wrap in `{_raw: <string>}` so the agent loop can still
    feed the result back as a recoverable tool error."""
    adapter = OpenAIAdapter(api_key="x", base_url=None)
    _attach_fake_stream(
        adapter,
        [
            _make_chunk(tool_calls=[{
                "index": 0, "id": "call_bad",
                "function": {"name": "shell_exec", "arguments": "this is not json"},
            }]),
            _make_chunk(finish_reason="tool_calls"),
        ],
    )
    events = []
    async for evt in adapter.stream_turn(
        messages=[], tools=[], model="x", system=None,
    ):
        events.append(evt)
    completes = [e for e in events if isinstance(e, ToolUseComplete)]
    assert len(completes) == 1
    assert completes[0].input == {"_raw": "this is not json"}
