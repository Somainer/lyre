"""A scripted LLMAdapter for tests — yields the events you hand it.

Each call to stream_turn pops one "turn script" (list of events) from a queue.
This lets a test drive a multi-turn agent loop without an API key.
"""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator
from typing import Any

from lyre.adapter.llm_adapter import (
    LyreMessage,
    LyreToolSpec,
    StreamEvent,
    TurnComplete,
)


class FakeAdapter:
    """Push lists of StreamEvents; each stream_turn() yields one list."""

    def __init__(self) -> None:
        self._turns: deque[list[StreamEvent]] = deque()
        self.calls: list[dict[str, Any]] = []

    def push_turn(self, events: list[StreamEvent]) -> None:
        """Queue a sequence of events. Auto-appends TurnComplete if missing."""
        if not any(isinstance(e, TurnComplete) for e in events):
            events = [*events, TurnComplete(stop_reason="end_turn")]
        self._turns.append(events)

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
            {
                "messages": messages,
                "tools": tools,
                "model": model,
                "system": system,
            }
        )
        if not self._turns:
            yield TurnComplete(stop_reason="end_turn")
            return
        for evt in self._turns.popleft():
            yield evt
