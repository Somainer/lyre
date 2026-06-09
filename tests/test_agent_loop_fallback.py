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
    ToolUseComplete,
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


# ---------------------------------------------------------------------------
# R2: a mid-stream failure is no longer fatal — it fails over to the next
# candidate, bounded by max_midstream_retries. Safe because tools dispatch only
# AFTER a turn returns, so a discarded partial leaves no durable side effect.
# (Replaces the old test_midstream_error_propagates_and_no_retry.)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_midstream_error_falls_back_to_next_candidate(
    object_store: Path,
) -> None:
    bad = _MidStreamRaisingAdapter()  # yields "partial" then raises
    healthy = FakeAdapter()
    healthy.push_turn([ContentDelta(text="ok"), TurnComplete(stop_reason="end_turn")])
    adapters = {"a": bad, "b": healthy}
    candidates = [fake_entry(id="a"), fake_entry(id="b")]

    transcript = TranscriptWriter(object_store, "wakeup-mid-fo")
    loop = AgentLoop(  # default max_midstream_retries=1
        candidates=candidates,
        adapter_for=lambda e: adapters[e.id],
        model_name_for=lambda e: e.id,
        transcript=transcript,
    )
    result = await loop.run("", [_user_msg()])
    transcript.close()

    assert result.status == "completed"
    assert result.model_id == "b"
    # The discarded partial ("partial") never leaked into the result — only b's.
    assert result.text == "ok"
    assert any(ev["reason"] == "midstream_error" for ev in result.fallback_events)


@pytest.mark.asyncio
async def test_midstream_cap_bounds_failover_independent_of_candidate_count(
    object_store: Path,
) -> None:
    """max_midstream_retries=1 → at most ONE mid-stream failover, so a third
    candidate is never reached even though it's healthy. a (attempt 1 →
    failover) → b (attempt 2 → exceed → raise); c is never called."""
    a = _MidStreamRaisingAdapter()
    b = _MidStreamRaisingAdapter()
    c = _RaisingAdapter(RuntimeError("should not reach c"))
    adapters: dict[str, object] = {"a": a, "b": b, "c": c}
    candidates = [fake_entry(id="a"), fake_entry(id="b"), fake_entry(id="c")]

    transcript = TranscriptWriter(object_store, "wakeup-mid-cap")
    loop = AgentLoop(
        candidates=candidates,
        adapter_for=lambda e: adapters[e.id],
        model_name_for=lambda e: e.id,
        transcript=transcript,
    )
    with pytest.raises(RuntimeError, match="mid-stream"):
        await loop.run("", [_user_msg()])
    transcript.close()
    assert c.called == 0  # the cap stopped failover before reaching 'c'


@pytest.mark.asyncio
async def test_midstream_partial_tool_use_is_never_dispatched(
    object_store: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The safety invariant the whole design rests on: a tool_use that was
    FULLY streamed before the stream died is discarded with the partial — it
    must NEVER be dispatched (tools run strictly post-turn)."""

    class _ToolThenRaise:
        async def stream_turn(self, **kwargs) -> AsyncIterator[StreamEvent]:
            yield ToolUseComplete(id="t1", name="danger", input={})
            raise RuntimeError("mid-stream after a complete tool_use")

    healthy = FakeAdapter()
    healthy.push_turn([ContentDelta(text="ok"), TurnComplete(stop_reason="end_turn")])
    adapters: dict[str, object] = {"a": _ToolThenRaise(), "b": healthy}
    candidates = [fake_entry(id="a"), fake_entry(id="b")]

    transcript = TranscriptWriter(object_store, "wakeup-mid-tool")
    loop = AgentLoop(
        candidates=candidates,
        adapter_for=lambda e: adapters[e.id],
        model_name_for=lambda e: e.id,
        transcript=transcript,
    )
    dispatched: list[str] = []

    async def _spy(name, tool_use_id, tool_input):  # type: ignore[no-untyped-def]
        dispatched.append(name)
        return ("", False, [])

    monkeypatch.setattr(loop, "_dispatch_tool", _spy)
    result = await loop.run("", [_user_msg()])
    transcript.close()

    assert result.model_id == "b" and result.text == "ok"  # failed over cleanly
    assert dispatched == []  # the partial tool_use from 'a' was NEVER run


@pytest.mark.asyncio
async def test_midstream_max_retries_zero_keeps_fatal(object_store: Path) -> None:
    """0 disables R2 → the old behavior: a mid-stream failure is fatal and does
    NOT fall over (b is never tried)."""
    bad = _MidStreamRaisingAdapter()
    healthy = FakeAdapter()
    healthy.push_turn([ContentDelta(text="ok"), TurnComplete(stop_reason="end_turn")])
    adapters = {"a": bad, "b": healthy}
    candidates = [fake_entry(id="a"), fake_entry(id="b")]

    transcript = TranscriptWriter(object_store, "wakeup-mid-zero")
    loop = AgentLoop(
        candidates=candidates,
        adapter_for=lambda e: adapters[e.id],
        model_name_for=lambda e: e.id,
        transcript=transcript,
        max_midstream_retries=0,
    )
    with pytest.raises(RuntimeError, match="mid-stream"):
        await loop.run("", [_user_msg()])
    transcript.close()
