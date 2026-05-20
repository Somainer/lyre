"""Shared test helpers for building AgentLoop / Router fixtures."""

from __future__ import annotations

from lyre.adapter.llm_adapter import LLMAdapter
from lyre.runtime.agent_loop import AgentLoop
from lyre.runtime.model_registry import (
    ModelCost,
    ModelEndpoint,
    ModelEntry,
    ModelRegistry,
)
from lyre.runtime.transcript import TranscriptWriter


def fake_entry(
    id: str = "fake.test-model",
    *,
    tier: str = "workhorse",
    capabilities: tuple[str, ...] = ("tool_use", "streaming"),
    provider: str = "fake",
    base_url: str | None = None,
    auth_env: str = "FAKE_API_KEY",
    status: str = "enabled",
) -> ModelEntry:
    return ModelEntry(
        id=id,
        provider=provider,
        endpoint=ModelEndpoint(base_url=base_url, auth_env=auth_env),
        capabilities=capabilities,
        tier=tier,  # type: ignore[arg-type]
        cost_per_mtok=ModelCost(None, None),
        context_window=None,
        status=status,  # type: ignore[arg-type]
    )


def fake_registry(*entries: ModelEntry) -> ModelRegistry:
    return ModelRegistry(entries=list(entries))


def build_single_candidate_loop(
    adapter: LLMAdapter,
    transcript: TranscriptWriter,
    *,
    tool_registry=None,
    tool_context=None,
    allowed_tools=None,
    max_turns: int = 24,
    model_id: str = "fake.test-model",
) -> AgentLoop:
    """Convenience: wrap a single adapter into the new candidate-list shape."""
    entry = fake_entry(id=model_id)

    def _adapter_for(_entry):
        return adapter

    return AgentLoop(
        candidates=[entry],
        adapter_for=_adapter_for,
        model_name_for=lambda e: e.id,
        transcript=transcript,
        tool_registry=tool_registry,
        tool_context=tool_context,
        allowed_tools=allowed_tools,
        max_turns=max_turns,
    )
