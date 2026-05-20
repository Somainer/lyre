"""Tests for AdapterFactory + model_name_for_provider."""

from __future__ import annotations

import pytest

from lyre.runtime.adapter_factory import (
    AdapterFactory,
    AdapterFactoryError,
    model_name_for_provider,
)

from .helpers import fake_entry


def test_model_name_strips_provider_namespace() -> None:
    e = fake_entry(id="anthropic.claude-opus-4-7")
    assert model_name_for_provider(e) == "claude-opus-4-7"
    e2 = fake_entry(id="deepseek.deepseek-v4-pro")
    assert model_name_for_provider(e2) == "deepseek-v4-pro"
    # No dot → returned as-is.
    e3 = fake_entry(id="standalone")
    assert model_name_for_provider(e3) == "standalone"


def test_factory_make_anthropic_with_env_key(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    entry = fake_entry(
        provider="anthropic", auth_env="ANTHROPIC_API_KEY", base_url=None
    )
    factory = AdapterFactory()
    adapter = factory.make(entry)
    # Sanity: the constructed object is AnthropicAdapter (duck-typed via attr).
    assert hasattr(adapter, "stream_turn")


def test_factory_raises_when_auth_env_missing(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    entry = fake_entry(provider="anthropic", auth_env="DEEPSEEK_API_KEY")
    factory = AdapterFactory()
    with pytest.raises(AdapterFactoryError, match="DEEPSEEK_API_KEY"):
        factory.make(entry)


def test_factory_raises_on_unknown_provider(monkeypatch) -> None:
    # Env var must be set so we get past the auth-key check and reach
    # the provider dispatch (where the unknown-provider error fires).
    monkeypatch.setenv("FAKE_API_KEY", "sk-fake")
    entry = fake_entry(provider="ghost-provider", auth_env="FAKE_API_KEY")
    with pytest.raises(AdapterFactoryError, match="Unknown provider"):
        AdapterFactory().make(entry)


def test_factory_passes_base_url_through(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deep")
    entry = fake_entry(
        provider="anthropic",
        auth_env="DEEPSEEK_API_KEY",
        base_url="https://api.deepseek.com/anthropic",
    )
    factory = AdapterFactory()
    adapter = factory.make(entry)
    # AnthropicAdapter stores the client; we don't peek into private state,
    # just verify it didn't error and produced a stream_turn-capable object.
    assert hasattr(adapter, "stream_turn")


def test_factory_makes_openai_adapter(monkeypatch) -> None:
    """provider='openai' must route to OpenAIAdapter — covers OpenAI
    proper + DeepSeek OAI-compat + OpenRouter etc."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    entry = fake_entry(
        provider="openai", auth_env="OPENAI_API_KEY", base_url=None
    )
    factory = AdapterFactory()
    adapter = factory.make(entry)
    assert hasattr(adapter, "stream_turn")
    # Verify type by import (NOT isinstance check — keeps the test
    # cheap and doesn't pull SDK clients into the test scope unnecessarily).
    from lyre.adapter.openai import OpenAIAdapter
    assert isinstance(adapter, OpenAIAdapter)


def test_factory_passes_base_url_to_openai(monkeypatch) -> None:
    """DeepSeek's OAI-compat endpoint lives at api.deepseek.com/v1 —
    base_url must propagate to the OpenAIAdapter."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deep")
    entry = fake_entry(
        provider="openai",
        auth_env="DEEPSEEK_API_KEY",
        base_url="https://api.deepseek.com/v1",
    )
    adapter = AdapterFactory().make(entry)
    # The AsyncOpenAI client exposes base_url; verify it round-tripped.
    assert str(adapter.client.base_url).rstrip("/") == "https://api.deepseek.com/v1"
