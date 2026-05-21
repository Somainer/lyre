"""Tests for the YAML model registry loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from lyre.config import ModelEntry as ConfigModelEntry
from lyre.runtime.model_registry import (
    ModelCost,
    ModelEndpoint,
    ModelEntry,
    ModelRegistry,
    default_registry_path,
    load_registry,
    merge_user_entries,
    parse_registry,
)


def test_parse_minimal_entry() -> None:
    yaml_text = """
models:
  - id: anthropic.claude-opus-4-7
    provider: anthropic
    endpoint:
      auth_env: ANTHROPIC_API_KEY
    capabilities: [tool_use, streaming]
    tier: flagship
"""
    reg = parse_registry(yaml_text)
    assert len(reg.entries) == 1
    e = reg.entries[0]
    assert e.id == "anthropic.claude-opus-4-7"
    assert e.provider == "anthropic"
    assert e.endpoint.auth_env == "ANTHROPIC_API_KEY"
    assert e.endpoint.base_url is None
    assert e.capabilities == ("tool_use", "streaming")
    assert e.tier == "flagship"
    assert e.status == "enabled"


def test_parse_full_entry_with_cost_and_base_url() -> None:
    yaml_text = """
models:
  - id: deepseek.deepseek-v4-pro
    provider: anthropic
    endpoint:
      base_url: https://api.deepseek.com/anthropic
      auth_env: DEEPSEEK_API_KEY
    capabilities: [tool_use, streaming]
    tier: workhorse
    cost_per_mtok: { input: 0.27, output: 1.10 }
    context_window: 128000
    status: enabled
"""
    reg = parse_registry(yaml_text)
    e = reg.entries[0]
    assert e.endpoint.base_url == "https://api.deepseek.com/anthropic"
    assert e.endpoint.auth_env == "DEEPSEEK_API_KEY"
    assert e.cost_per_mtok.input == 0.27
    assert e.context_window == 128000


def test_parse_rejects_missing_required_field() -> None:
    yaml_text = """
models:
  - provider: anthropic
    capabilities: [tool_use]
    tier: cheap
"""
    with pytest.raises(ValueError, match="missing field"):
        parse_registry(yaml_text)


def test_parse_rejects_invalid_tier() -> None:
    yaml_text = """
models:
  - id: x.y
    provider: anthropic
    capabilities: [tool_use]
    tier: super-flagship
"""
    with pytest.raises(ValueError, match="invalid tier"):
        parse_registry(yaml_text)


def test_parse_rejects_duplicate_id() -> None:
    yaml_text = """
models:
  - id: a.b
    provider: anthropic
    capabilities: [x]
    tier: flagship
  - id: a.b
    provider: anthropic
    capabilities: [x]
    tier: cheap
"""
    with pytest.raises(ValueError, match="duplicate id"):
        parse_registry(yaml_text)


def test_entry_supports_capability_subset() -> None:
    yaml_text = """
models:
  - id: x
    provider: anthropic
    capabilities: [tool_use, streaming, reasoning]
    tier: flagship
"""
    reg = parse_registry(yaml_text)
    e = reg.entries[0]
    assert e.supports(["tool_use"])
    assert e.supports(["tool_use", "streaming"])
    assert not e.supports(["tool_use", "vision"])
    assert e.supports([])  # empty requires → trivially satisfied


def test_registry_by_id_and_enabled() -> None:
    yaml_text = """
models:
  - id: a
    provider: anthropic
    capabilities: []
    tier: cheap
    status: enabled
  - id: b
    provider: anthropic
    capabilities: []
    tier: cheap
    status: disabled
"""
    reg = parse_registry(yaml_text)
    assert reg.by_id("a") is not None
    assert reg.by_id("b") is not None
    assert reg.by_id("c") is None
    assert [e.id for e in reg.enabled()] == ["a"]


def test_load_default_registry_yaml() -> None:
    """The shipped model_registry.yaml at repo root is valid + contains both
    Anthropic and DeepSeek (anthropic-compat) entries enabled by default.
    OpenAI / DeepSeek-OAI-compat entries ship `status: disabled` so they
    don't activate without owner-set OPENAI_API_KEY (or whatever they use)."""
    path = default_registry_path()
    reg = load_registry(path)
    ids = {e.id for e in reg.entries}
    assert "anthropic.claude-opus-4-7" in ids
    assert "anthropic.claude-sonnet-4-6" in ids
    assert "deepseek.deepseek-v4-pro" in ids
    # All ANTHROPIC entries are enabled (these are the default path the
    # owner uses).
    anth = [e for e in reg.entries if e.provider == "anthropic"]
    assert anth, "expected at least one anthropic entry"
    assert all(e.status == "enabled" for e in anth)
    # Every entry has a recognized status value.
    assert all(e.status in ("enabled", "disabled") for e in reg.entries)


def test_load_registry_raises_on_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_registry(tmp_path / "nope.yaml")


# ---------------------------------------------------------------------------
# merge_user_entries — same-id field-level fallback to shipped defaults.
# Fix for the "ctx 0%, compaction never fires" symptom: a user config that
# REPLACES a shipped entry by id (typical onboard output) used to lose the
# shipped context_window / cost_per_mtok wholesale.
# ---------------------------------------------------------------------------


def _shipped_registry() -> ModelRegistry:
    """Compact shipped registry stub — only what these tests need."""
    return ModelRegistry(entries=[
        ModelEntry(
            id="deepseek.deepseek-v4-pro",
            provider="anthropic",
            endpoint=ModelEndpoint(
                base_url="https://api.deepseek.com/anthropic",
                auth_env="DEEPSEEK_API_KEY",
            ),
            capabilities=("tool_use", "streaming"),
            tier="workhorse",
            cost_per_mtok=ModelCost(input=0.27, output=1.10),
            context_window=128000,
            status="enabled",
        ),
    ])


def test_user_entry_inherits_context_window_from_shipped_on_id_match() -> None:
    """The fix: a user entry that omits context_window AND shares an id
    with a shipped entry inherits the shipped value. Without this the
    auto-compact gate (turn_input >= threshold * ctx_window) silently
    never fires."""
    user = ConfigModelEntry(
        id="deepseek.deepseek-v4-pro",
        provider="anthropic",
        endpoint={
            "base_url": "https://api.deepseek.com/anthropic",
            "auth_env": "DEEPSEEK_API_KEY",
        },
        capabilities=["tool_use", "streaming"],
        tier="workhorse",
        # NOTE: no context_window, no cost_per_mtok — the bug case.
    )
    merged = merge_user_entries(_shipped_registry(), [user])
    e = merged.by_id("deepseek.deepseek-v4-pro")
    assert e is not None
    assert e.context_window == 128000  # inherited!
    assert e.cost_per_mtok.input == 0.27
    assert e.cost_per_mtok.output == 1.10


def test_user_entry_explicit_override_wins_over_shipped() -> None:
    """When the user DOES specify context_window, theirs is used —
    inheritance is fallback-only, not silent merge that the user
    can't escape."""
    user = ConfigModelEntry(
        id="deepseek.deepseek-v4-pro",
        provider="anthropic",
        endpoint={"auth_env": "DEEPSEEK_API_KEY"},
        capabilities=["tool_use"],
        tier="workhorse",
        context_window=64000,  # explicitly halved
        cost_per_mtok={"input": 0.50, "output": 2.00},
    )
    e = merge_user_entries(_shipped_registry(), [user]).by_id(
        "deepseek.deepseek-v4-pro",
    )
    assert e is not None
    assert e.context_window == 64000
    assert e.cost_per_mtok.input == 0.50
    assert e.cost_per_mtok.output == 2.00


def test_user_entry_with_no_shipped_match_keeps_none() -> None:
    """A genuinely new id (no shipped peer) doesn't get magic
    inheritance from elsewhere. context_window stays None and the
    dashboard cleanly shows '—' for ctx%."""
    user = ConfigModelEntry(
        id="my-custom.gpt-fake",
        provider="openai",
        endpoint={"auth_env": "OPENAI_API_KEY"},
        capabilities=["tool_use"],
        tier="workhorse",
    )
    e = merge_user_entries(_shipped_registry(), [user]).by_id(
        "my-custom.gpt-fake",
    )
    assert e is not None
    assert e.context_window is None
    # ModelCost.from_dict(None) yields (None, None), not None — the
    # downstream consumers all handle that shape uniformly. Asserting
    # both leaves are None pins the contract.
    assert e.cost_per_mtok.input is None
    assert e.cost_per_mtok.output is None


def test_user_entry_partial_cost_does_not_inherit_other_half() -> None:
    """If the user specifies cost_per_mtok at all, their dict wins
    wholesale — we don't try to merge per-field (input vs output)
    because that gets surprising fast. Inheritance is "whole field
    or nothing"."""
    user = ConfigModelEntry(
        id="deepseek.deepseek-v4-pro",
        provider="anthropic",
        endpoint={"auth_env": "DEEPSEEK_API_KEY"},
        capabilities=["tool_use"],
        tier="workhorse",
        cost_per_mtok={"input": 0.10},  # only input; output omitted
    )
    e = merge_user_entries(_shipped_registry(), [user]).by_id(
        "deepseek.deepseek-v4-pro",
    )
    assert e is not None
    assert e.cost_per_mtok.input == 0.10
    # Output is NOT 1.10 from shipped — user-intent wins, even partial.
    assert e.cost_per_mtok.output is None


def test_merge_user_entries_empty_returns_shipped_unchanged() -> None:
    """No config.toml [[models]] (fresh install) → shipped registry
    passes through untouched. Regression guard."""
    shipped = _shipped_registry()
    merged = merge_user_entries(shipped, [])
    assert merged is shipped
