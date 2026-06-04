"""Transcript writer — streams agent events to object_store/wakeups/{id}/transcript.jsonl."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def _now_ms() -> int:
    return int(time.time() * 1000)


class TranscriptWriter:
    """Append-only JSONL writer for one wakeup's transcript."""

    def __init__(self, object_store_root: Path, wakeup_id: str):
        self.dir = object_store_root / "wakeups" / wakeup_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "transcript.jsonl"
        self._fp = self.path.open("a", encoding="utf-8")

    def _write(self, obj: dict[str, Any]) -> None:
        obj["ts"] = _now_ms()
        self._fp.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self._fp.flush()

    def write_delta(self, text: str) -> None:
        self._write({"type": "content_delta", "text": text})

    def write_thinking_delta(self, text: str) -> None:
        """Model's reasoning / thinking. Never delivered, but kept for
        operator debug (especially DeepSeek-V4-pro, which emits long
        reasoning blocks before deciding what tools to call)."""
        self._write({"type": "thinking_delta", "text": text})

    def write_tool_use(self, id: str, name: str, input: dict[str, Any]) -> None:
        self._write({"type": "tool_use", "id": id, "name": name, "input": input})

    def write_tool_result(self, id: str, result: Any, is_error: bool) -> None:
        self._write(
            {"type": "tool_result", "id": id, "result": result, "is_error": is_error}
        )

    def write_system(
        self, system_prompt: str, tool_names: list[str], allowed_tools: list[str]
    ) -> None:
        """Snapshot of the LLM's *input* — the system prompt and the tools we
        advertised. Logged once per wakeup so an auditor can reproduce exactly
        what the model saw before deciding what to do (or not do).
        """
        self._write(
            {
                "type": "system",
                "system_prompt": system_prompt,
                "tool_names": tool_names,
                "allowed_tools": allowed_tools,
            }
        )

    def write_turn_end(
        self,
        turn_idx: int,
        stop_reason: str | None,
        text_len: int,
        tool_count: int,
        model_id: str,
    ) -> None:
        """End-of-turn marker so audits can see 'silent turns' (text_len=0,
        tool_count=0). Cheap, structured, doesn't bloat the file."""
        self._write(
            {
                "type": "turn_end",
                "turn": turn_idx,
                "stop_reason": stop_reason,
                "text_len": text_len,
                "tool_count": tool_count,
                "model_id": model_id,
            }
        )

    def note(self, text: str) -> None:
        self._write({"type": "note", "text": text})

    def close(self) -> None:
        # flush()/write() only reach the OS page cache, which survives a
        # process SIGKILL but not a host power loss / kernel panic. The
        # transcript is the durable cold-tier audit trail the kill-test
        # relies on, so fsync once at finalization to make the completed
        # file power-loss-durable without paying fsync per streamed delta.
        try:
            self._fp.flush()
            os.fsync(self._fp.fileno())
        except OSError:
            # Non-seekable/already-detached fd, or fs that can't fsync —
            # degrade to flush-only rather than crash the scheduler's
            # end-of-wakeup cleanup.
            pass
        finally:
            self._fp.close()

    @property
    def uri(self) -> str:
        return f"file://{self.path}"
