"""Per-turn model fallback tests for AgentLoop."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from lyre.adapter.llm_adapter import (
    ContentDelta,
    LyreContentBlock,
    LyreMessage,
    LyreToolSpec,
    StreamEvent,
    TurnComplete,
)
from lyre.runtime.agent_loop import AgentLoop, AllCandidatesFailedError
from lyre.runtime.health_tracker import HealthTracker
from lyre.runtime.transcript import TranscriptWriter

from .fake_adapter import FakeAdapter
from .helpers import fake_entry


class _RaisingAdapter:
    """Adapter that raises before yielding (pre-stream error)."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.called = 0

    async def stream_turn(
        self,
        messages: list[LyreMessage],
        tools: list[LyreToolSpec],
        model: str,
        max_tokens: int = 4096,
        temperature: float | None = None,
        system: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.called += 1
        raise self.exc
        yield  # pragma: no cover


class _MidStreamRaisingAdapter:
    """Adapter that yields one event then raises."""

    async def stream_turn(self, **kwargs) -> AsyncIterator[StreamEvent]:
        yield ContentDelta(text="partial")
        raise RuntimeError("mid-stream boom")


def _user_msg(text: str = "hi") -> LyreMessage:
    return LyreMessage(role="user", content=[LyreContentBlock(type="text", text=text)])


@pytest.mark.asyncio
async def test_falls_back_to_next_candidate_on_pre_stream_error(
    object_store: Path,
) -> None:
    raising = _RaisingAdapter(RuntimeError("rate_limited"))
    healthy = FakeAdapter()
    healthy.push_turn([ContentDelta(text="ok"), TurnComplete(stop_reason="end_turn")])

    adapters = {"a.bad": raising, "b.good": healthy}
    candidates = [
        fake_entry(id="a.bad", tier="flagship"),
        fake_entry(id="b.good", tier="flagship"),
    ]

    health = HealthTracker()
    transcript = TranscriptWriter(object_store, "wakeup-fallback")
    loop = AgentLoop(
        candidates=candidates,
        adapter_for=lambda e: adapters[e.id],
        model_name_for=lambda e: e.id,
        transcript=transcript,
        health=health,
    )
    result = await loop.run("", [_user_msg()])
    transcript.close()

    assert result.text == "ok"
    assert result.status == "completed"
    assert result.model_id == "b.good"
    assert len(result.fallback_events) == 1
    assert result.fallback_events[0]["model_id"] == "a.bad"
    assert raising.called == 1

    # Health tracker recorded the failure.
    assert health.snapshot()["a.bad"]["recent_failures"] == 1


@pytest.mark.asyncio
async def test_raises_when_all_candidates_fail(object_store: Path) -> None:
    bad1 = _RaisingAdapter(RuntimeError("boom1"))
    bad2 = _RaisingAdapter(RuntimeError("boom2"))
    adapters = {"a": bad1, "b": bad2}
    candidates = [fake_entry(id="a"), fake_entry(id="b")]
    transcript = TranscriptWriter(object_store, "wakeup-all-fail")
    loop = AgentLoop(
        candidates=candidates,
        adapter_for=lambda e: adapters[e.id],
        model_name_for=lambda e: e.id,
        transcript=transcript,
    )
    with pytest.raises(AllCandidatesFailedError):
        await loop.run("", [_user_msg()])
    transcript.close()


@pytest.mark.asyncio
async def test_skips_unhealthy_candidate_before_call(object_store: Path) -> None:
    raising = _RaisingAdapter(RuntimeError("should not be reached"))
    healthy = FakeAdapter()
    healthy.push_turn([ContentDelta(text="ok"), TurnComplete(stop_reason="end_turn")])
    adapters = {"sick": raising, "well": healthy}
    candidates = [fake_entry(id="sick"), fake_entry(id="well")]

    health = HealthTracker()
    # Force the circuit open for "sick".
    for _ in range(3):
        health.mark_failure("sick")

    transcript = TranscriptWriter(object_store, "wakeup-skip")
    loop = AgentLoop(
        candidates=candidates,
        adapter_for=lambda e: adapters[e.id],
        model_name_for=lambda e: e.id,
        transcript=transcript,
        health=health,
    )
    result = await loop.run("", [_user_msg()])
    transcript.close()

    assert raising.called == 0
    assert result.model_id == "well"
    assert any(ev["reason"] == "circuit_open" for ev in result.fallback_events)


@pytest.mark.asyncio
async def test_midstream_error_propagates_and_no_retry(object_store: Path) -> None:
    bad = _MidStreamRaisingAdapter()
    healthy = FakeAdapter()
    healthy.push_turn([ContentDelta(text="never"), TurnComplete(stop_reason="end_turn")])
    adapters = {"a": bad, "b": healthy}
    candidates = [fake_entry(id="a"), fake_entry(id="b")]

    transcript = TranscriptWriter(object_store, "wakeup-mid")
    loop = AgentLoop(
        candidates=candidates,
        adapter_for=lambda e: adapters[e.id],
        model_name_for=lambda e: e.id,
        transcript=transcript,
    )
    with pytest.raises(RuntimeError, match="mid-stream"):
        await loop.run("", [_user_msg()])
    transcript.close()
