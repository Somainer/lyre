"""Tests for the openai_responses adapter.

The Responses API has its own event shape (`type=response.*.delta` etc.)
distinct from chat-completions chunks, so it gets a separate test file
rather than piggy-backing on test_openai_adapter.py's helpers.

Regression: an early version of this adapter yielded ToolUseDelta with
kwarg `args_chunk=...` instead of the dataclass field `input_partial=...`,
which crashed the wakeup at runtime (TypeError) but slipped through
because no test exercised the function_call_arguments.delta path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from lyre.adapter.llm_adapter import (
    ContentDelta,
    ToolUseComplete,
    ToolUseDelta,
    ToolUseStart,
    TurnComplete,
)
from lyre.adapter.openai_responses import OpenAIResponsesAdapter


class _FakeStream:
    def __init__(self, events: list[Any]):
        self._events = events

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._iter()

    async def _iter(self):
        for e in self._events:
            yield e


def _attach(adapter: OpenAIResponsesAdapter, events: list[Any]) -> None:
    adapter.client = MagicMock()
    adapter.client.responses = MagicMock()
    adapter.client.responses.create = AsyncMock(return_value=_FakeStream(events))


def _evt(type_: str, **fields: Any) -> SimpleNamespace:
    """Build a SimpleNamespace mimicking an openai responses stream event."""
    return SimpleNamespace(type=type_, **fields)


@pytest.mark.asyncio
async def test_function_call_arguments_delta_yields_tool_use_delta() -> None:
    """The function_call_arguments.delta event MUST translate into a
    ToolUseDelta(id=..., input_partial=...). This is the exact path that
    crashed when the adapter passed `args_chunk=` instead — it's worth
    a dedicated test."""
    adapter = OpenAIResponsesAdapter(api_key="x")
    _attach(adapter, [
        _evt(
            "response.output_item.added",
            item=SimpleNamespace(
                type="function_call",
                id="item_1",
                call_id="call_abc",
                name="shell_exec",
            ),
        ),
        _evt("response.function_call_arguments.delta", item_id="item_1", delta='{"a":'),
        _evt("response.function_call_arguments.delta", item_id="item_1", delta=' 1}'),
        _evt(
            "response.function_call_arguments.done",
            item_id="item_1",
            arguments='{"a": 1}',
        ),
        _evt("response.completed", response=SimpleNamespace(
            usage=SimpleNamespace(input_tokens=3, output_tokens=2),
        )),
    ])

    events = [evt async for evt in adapter.stream_turn(
        messages=[], tools=[], model="gpt-x", system=None,
    )]

    starts = [e for e in events if isinstance(e, ToolUseStart)]
    deltas = [e for e in events if isinstance(e, ToolUseDelta)]
    completes = [e for e in events if isinstance(e, ToolUseComplete)]

    assert len(starts) == 1
    assert starts[0].id == "call_abc"
    assert starts[0].name == "shell_exec"

    assert len(deltas) == 2
    # Critical regression check: the dataclass field is `input_partial`.
    # If anyone reverts to `args_chunk=`, this line raises AttributeError.
    assert "".join(d.input_partial for d in deltas) == '{"a": 1}'

    assert len(completes) == 1
    assert completes[0].id == "call_abc"
    assert completes[0].name == "shell_exec"
    assert completes[0].input == {"a": 1}


@pytest.mark.asyncio
async def test_text_delta_yields_content_delta() -> None:
    """Sanity: plain text streaming still works (baseline path that the
    other tests exercise via chat-completions)."""
    adapter = OpenAIResponsesAdapter(api_key="x")
    _attach(adapter, [
        _evt("response.output_text.delta", delta="Hello "),
        _evt("response.output_text.delta", delta="world"),
        _evt("response.completed", response=SimpleNamespace(
            usage=SimpleNamespace(input_tokens=5, output_tokens=2),
        )),
    ])

    events = [evt async for evt in adapter.stream_turn(
        messages=[], tools=[], model="gpt-x", system=None,
    )]
    text = "".join(e.text for e in events if isinstance(e, ContentDelta))
    assert text == "Hello world"
    assert any(isinstance(e, TurnComplete) for e in events)
