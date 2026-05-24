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
    ToolUseComplete,
    TurnComplete,
    Usage,
)


class FakeAdapter:
    """Push lists of StreamEvents; each stream_turn() yields one list."""

    def __init__(self) -> None:
        self._turns: deque[list[StreamEvent]] = deque()
        self.calls: list[dict[str, Any]] = []
        self._end_wakeup_counter = 0

    def push_turn(self, events: list[StreamEvent]) -> None:
        """Queue a sequence of events. Auto-appends TurnComplete if missing."""
        if not any(isinstance(e, TurnComplete) for e in events):
            events = [*events, TurnComplete(stop_reason="end_turn")]
        self._turns.append(events)

    def push_done(
        self,
        summary: str = "ok",
        *,
        input_tokens: int = 10,
        output_tokens: int = 5,
        prefix_events: list[StreamEvent] | None = None,
    ) -> None:
        """Convenience: queue the canonical "wakeup is done" turn.

        Every wakeup must terminate with end_wakeup(...) per the
        WAKEUP_END_CONTRACT. Tests that don't care about the exact
        terminal shape can use this helper instead of repeating the
        boilerplate ToolUseComplete + TurnComplete pair.

        ``prefix_events`` lets the test prepend e.g. ContentDelta /
        ToolUseComplete for upstream work the wakeup does before
        ending. Pass other tools/text first, then close with done.
        """
        self._end_wakeup_counter += 1
        events: list[StreamEvent] = list(prefix_events or [])
        events.extend(
            [
                ToolUseComplete(
                    id=f"_fake_end_wakeup_{self._end_wakeup_counter}",
                    name="end_wakeup",
                    input={"status": "done", "summary": summary},
                ),
                Usage(
                    input_tokens=input_tokens, output_tokens=output_tokens,
                ),
                TurnComplete(stop_reason="end_turn"),
            ]
        )
        self._turns.append(events)

    def push_awaiting(
        self,
        awaiting_on: str,
        summary: str = "ok",
        *,
        awaiting_ref: str | None = None,
        prefix_events: list[StreamEvent] | None = None,
    ) -> None:
        """Convenience: queue an `end_wakeup(awaiting, ...)` terminal turn."""
        self._end_wakeup_counter += 1
        input_args: dict[str, Any] = {
            "status": "awaiting",
            "summary": summary,
            "awaiting_on": awaiting_on,
        }
        if awaiting_ref is not None:
            input_args["awaiting_ref"] = awaiting_ref
        events: list[StreamEvent] = list(prefix_events or [])
        events.extend(
            [
                ToolUseComplete(
                    id=f"_fake_end_wakeup_{self._end_wakeup_counter}",
                    name="end_wakeup",
                    input=input_args,
                ),
                Usage(input_tokens=10, output_tokens=5),
                TurnComplete(stop_reason="end_turn"),
            ]
        )
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
