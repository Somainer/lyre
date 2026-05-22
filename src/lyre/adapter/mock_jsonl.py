"""MockJsonlAdapter — for testing the subprocess path without burning tokens.

Reads scripted stream events from a JSONL file, one turn per JSON array:
    [{"type":"content_delta","text":"hi"},{"type":"turn_complete","stop_reason":"end_turn"}]
    [{"type":"tool_use_complete","id":"t1","name":"shell_exec","input":{"argv":["ls"]}}, ...]

Each line = one turn. The adapter pops the next turn on each `stream_turn`
call and yields its events. When the script runs out, yields a single
end_turn.

Activation: pass `LYRE_MOCK_ADAPTER_SCRIPT=/path/to/script.jsonl` as env to
the subprocess. The subprocess CLI checks for it and wires this adapter
into the Scheduler via `adapter_for_test`.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from .llm_adapter import (
    ContentDelta,
    LyreMessage,
    LyreToolSpec,
    StreamEvent,
    ToolUseComplete,
    TurnComplete,
    Usage,
)


def _parse_event(obj: dict[str, Any]) -> StreamEvent | None:
    t = obj.get("type")
    if t == "content_delta":
        return ContentDelta(text=obj.get("text", ""))
    if t == "tool_use_complete":
        return ToolUseComplete(
            id=obj.get("id", ""),
            name=obj.get("name", ""),
            input=obj.get("input") or {},
        )
    if t == "usage":
        return Usage(
            input_tokens=int(obj.get("input_tokens", 0)),
            output_tokens=int(obj.get("output_tokens", 0)),
        )
    if t == "turn_complete":
        return TurnComplete(stop_reason=obj.get("stop_reason", "end_turn"))
    return None


def _load_script(path: Path) -> deque[list[StreamEvent]]:
    turns: deque[list[StreamEvent]] = deque()
    text = path.read_text(encoding="utf-8")
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        objs = json.loads(s)
        events: list[StreamEvent] = []
        for o in objs:
            evt = _parse_event(o)
            if evt is not None:
                events.append(evt)
        if not any(isinstance(e, TurnComplete) for e in events):
            events.append(TurnComplete(stop_reason="end_turn"))
        turns.append(events)
    return turns


class MockJsonlAdapter:
    """Yields the next pre-scripted turn each time `stream_turn` is called."""

    def __init__(self, script_path: Path):
        self._turns = _load_script(script_path)

    async def stream_turn(
        self,
        messages: list[LyreMessage],
        tools: list[LyreToolSpec],
        model: str,
        max_tokens: int = 4096,
        temperature: float | None = None,
        system: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        if not self._turns:
            yield TurnComplete(stop_reason="end_turn")
            return
        for evt in self._turns.popleft():
            yield evt
