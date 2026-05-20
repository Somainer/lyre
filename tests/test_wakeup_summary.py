"""Tests for runtime.wakeup_summary — the inline finalize-hook that
replaces the deleted summary-agent persona."""

from __future__ import annotations

from pathlib import Path

import pytest

from lyre.adapter.llm_adapter import ContentDelta, TurnComplete
from lyre.runtime.agent_loop import AgentLoopResult
from lyre.runtime.health_tracker import HealthTracker
from lyre.runtime.model_router import ModelRouter
from lyre.runtime.wakeup_summary import (
    SUMMARY_SECTION_HEADER,
    summarize_and_append,
)

from .fake_adapter import FakeAdapter
from .helpers import fake_entry, fake_registry


def _result_with_mailbox_send() -> AgentLoopResult:
    return AgentLoopResult(
        status="completed",
        text="",
        tool_calls=[
            {
                "name": "mailbox_send",
                "input": {
                    "to": "owner",
                    "title": "status",
                    "body": "Investigation done; PR url: https://example/x/1",
                },
            }
        ],
        stop_reason="end_turn",
        turns=2,
    )


def _noop_result() -> AgentLoopResult:
    return AgentLoopResult(
        status="completed",
        text="",
        tool_calls=[],
        stop_reason="end_turn",
        turns=1,
    )


def _router_with_cheap() -> tuple[ModelRouter, FakeAdapter]:
    """Registry with a cheap entry + FakeAdapter that returns a fixed bullet."""
    registry = fake_registry(
        fake_entry(id="m-cheap", tier="cheap"),
        fake_entry(id="m-workhorse", tier="workhorse"),
    )
    router = ModelRouter(registry=registry, health=HealthTracker())
    adapter = FakeAdapter()
    adapter.push_turn(
        [
            ContentDelta(text="- replied to owner with PR url\n"),
            ContentDelta(text="- no open thread"),
            TurnComplete(stop_reason="end_turn"),
        ]
    )
    return router, adapter


@pytest.mark.asyncio
async def test_summary_appended_to_notes_when_cheap_model_available(
    tmp_path: Path,
) -> None:
    memory = tmp_path / "memory"
    (memory / "facts").mkdir(parents=True)
    router, adapter = _router_with_cheap()

    out = await summarize_and_append(
        wakeup_id="0190abcdef00",
        agent_id="dispatcher",
        persona_name="dispatcher",
        result=_result_with_mailbox_send(),
        memory_path=memory,
        router=router,
        adapter_for_entry=lambda _e: adapter,
    )

    assert out is not None and "replied to owner" in out
    notes = (memory / "facts" / "agent-dispatcher-notes.md").read_text(
        encoding="utf-8"
    )
    assert SUMMARY_SECTION_HEADER in notes
    # short_id = wakeup_id[:8] — see wakeup_summary._append_to_notes.
    assert "wakeup 0190abcd" in notes
    assert "replied to owner" in notes


@pytest.mark.asyncio
async def test_summary_skipped_when_no_cheap_model_registered(
    tmp_path: Path,
) -> None:
    memory = tmp_path / "memory"
    (memory / "facts").mkdir(parents=True)
    # Registry has workhorse + flagship but NO cheap. The strict cheap
    # filter means we must not consume the workhorse adapter slot.
    registry = fake_registry(
        fake_entry(id="m-flag", tier="flagship"),
        fake_entry(id="m-work", tier="workhorse"),
    )
    router = ModelRouter(registry=registry, health=HealthTracker())
    # An adapter that would fail noisily if called.
    calls = []

    def _adapter(_e):
        calls.append(_e.id)
        return FakeAdapter()

    out = await summarize_and_append(
        wakeup_id="0190deadbeef",
        agent_id="dispatcher",
        persona_name="dispatcher",
        result=_result_with_mailbox_send(),
        memory_path=memory,
        router=router,
        adapter_for_entry=_adapter,
    )

    assert out is None
    assert calls == []  # never invoked adapter
    assert not (memory / "facts" / "agent-dispatcher-notes.md").exists()


@pytest.mark.asyncio
async def test_summary_skipped_for_noop_wakeup(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    (memory / "facts").mkdir(parents=True)
    router, adapter = _router_with_cheap()

    out = await summarize_and_append(
        wakeup_id="0190noopnoop",
        agent_id="dispatcher",
        persona_name="dispatcher",
        result=_noop_result(),
        memory_path=memory,
        router=router,
        adapter_for_entry=lambda _e: adapter,
    )
    assert out is None
    assert not (memory / "facts" / "agent-dispatcher-notes.md").exists()


@pytest.mark.asyncio
async def test_summary_failure_is_swallowed(tmp_path: Path) -> None:
    """If the adapter raises, summary returns None and the caller's
    wakeup-finalize path is unaffected."""
    memory = tmp_path / "memory"
    (memory / "facts").mkdir(parents=True)

    class _BoomAdapter:
        async def stream_turn(
            self, messages, tools, model, max_tokens=4096, temperature=None,
            system=None,
        ):
            raise RuntimeError("network down")
            yield  # pragma: no cover (unreachable, makes this an async gen)

    registry = fake_registry(fake_entry(id="m-cheap", tier="cheap"))
    router = ModelRouter(registry=registry, health=HealthTracker())

    out = await summarize_and_append(
        wakeup_id="0190boomboom",
        agent_id="dispatcher",
        persona_name="dispatcher",
        result=_result_with_mailbox_send(),
        memory_path=memory,
        router=router,
        adapter_for_entry=lambda _e: _BoomAdapter(),
    )
    assert out is None  # never raised
    assert not (memory / "facts" / "agent-dispatcher-notes.md").exists()


@pytest.mark.asyncio
async def test_summary_inserts_newest_first_in_existing_section(
    tmp_path: Path,
) -> None:
    memory = tmp_path / "memory"
    (memory / "facts").mkdir(parents=True)
    notes = memory / "facts" / "agent-dispatcher-notes.md"
    notes.write_text(
        "# Leader notes\n\nSome content.\n\n"
        f"{SUMMARY_SECTION_HEADER}\n"
        "\n### 2026-01-01T00:00:00Z · wakeup older123\n- old summary\n",
        encoding="utf-8",
    )

    router, adapter = _router_with_cheap()
    out = await summarize_and_append(
        wakeup_id="0190newer000",
        agent_id="dispatcher",
        persona_name="dispatcher",
        result=_result_with_mailbox_send(),
        memory_path=memory,
        router=router,
        adapter_for_entry=lambda _e: adapter,
    )

    assert out is not None
    final = notes.read_text(encoding="utf-8")
    # Older entry preserved.
    assert "old summary" in final
    # New entry appears BEFORE the older one (newest-first).
    # short_id = wakeup_id[:8] in wakeup_summary._append_to_notes.
    new_pos = final.find("wakeup 0190newe")
    old_pos = final.find("wakeup older12")
    assert 0 <= new_pos < old_pos
    # Original file content preserved.
    assert "# Leader notes" in final
    assert "Some content." in final
