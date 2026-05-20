"""Tests for the YAML model registry loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from lyre.runtime.model_registry import (
    default_registry_path,
    load_registry,
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
