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


# ---------------------------------------------------------------------------
# Custom-header auth mode — for proxies / gateways that authenticate via
# a non-standard scheme (signed JWT, internal SSO token, mTLS-passthrough)
# instead of (or in addition to) the provider's native API key.
# ---------------------------------------------------------------------------


def test_factory_header_only_mode_builds_adapter(monkeypatch) -> None:
    """auth_env=None + headers set should build a working adapter
    without an API key. The SDK still requires a non-empty api_key
    string, so the factory passes a sentinel — the actual auth is
    supplied by the custom headers."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    entry = fake_entry(
        provider="openai",
        auth_env=None,
        headers=(("X-Custom-Auth", "static-token"),),
        base_url="https://my-proxy.internal/v1",
    )
    adapter = AdapterFactory().make(entry)
    # The OpenAI SDK exposes default_headers on the client; verify our
    # header was registered.
    from lyre.adapter.openai import OpenAIAdapter
    assert isinstance(adapter, OpenAIAdapter)
    assert adapter.client.default_headers.get("X-Custom-Auth") == "static-token"


def test_factory_header_value_interpolates_env_var(monkeypatch) -> None:
    """Header values may use ${ENV_VAR} so secrets stay out of
    config.toml. Interpolation runs at registry-load time (in
    ModelEndpoint.from_dict)."""
    monkeypatch.setenv("PROXY_TOKEN", "secret-jwt")
    from lyre.runtime.model_registry import ModelEndpoint
    ep = ModelEndpoint.from_dict({
        "headers": {"Authorization": "Bearer ${PROXY_TOKEN}"},
    })
    # The whole value must be the placeholder for interpolation to fire
    # — `"Bearer ${PROXY_TOKEN}"` is a partial placeholder, so it
    # stays as-is. Test the pure form (`"${PROXY_TOKEN}"`).
    ep_pure = ModelEndpoint.from_dict({
        "headers": {"X-Auth": "${PROXY_TOKEN}"},
    })
    assert dict(ep_pure.headers)["X-Auth"] == "secret-jwt"
    # And the partial form survives unchanged.
    assert dict(ep.headers)["Authorization"] == "Bearer ${PROXY_TOKEN}"


def test_factory_header_interpolation_missing_env_is_empty(
    monkeypatch,
) -> None:
    """Referencing an unset env var resolves to empty string, not an
    error — caller learns about it via auth failure at request time."""
    monkeypatch.delenv("NEVER_SET", raising=False)
    from lyre.runtime.model_registry import ModelEndpoint
    ep = ModelEndpoint.from_dict({
        "headers": {"X-Token": "${NEVER_SET}"},
    })
    assert dict(ep.headers)["X-Token"] == ""


def test_factory_both_modes_can_stack(monkeypatch) -> None:
    """API key + extra org/project headers is the OpenAI / Anthropic
    enterprise pattern — both must coexist."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    entry = fake_entry(
        provider="openai",
        auth_env="OPENAI_API_KEY",
        headers=(
            ("OpenAI-Organization", "org-abc"),
            ("OpenAI-Project", "proj-123"),
        ),
    )
    adapter = AdapterFactory().make(entry)
    assert adapter.client.default_headers.get("OpenAI-Organization") == "org-abc"
    assert adapter.client.default_headers.get("OpenAI-Project") == "proj-123"


def test_factory_rejects_when_neither_auth_env_nor_headers(
    monkeypatch,
) -> None:
    """auth_env=None AND headers=() must error — there's no way to
    authenticate. This is the loud failure that catches a misconfigured
    config.toml early."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    entry = fake_entry(provider="anthropic", auth_env=None, headers=())
    with pytest.raises(AdapterFactoryError, match="no auth configured"):
        AdapterFactory().make(entry)


def test_factory_header_only_with_anthropic_provider(
    monkeypatch,
) -> None:
    """Same path works for the Anthropic adapter (for users fronting
    Claude via a custom proxy)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    entry = fake_entry(
        provider="anthropic",
        auth_env=None,
        headers=(("X-Proxy-Token", "abc123"),),
    )
    adapter = AdapterFactory().make(entry)
    from lyre.adapter.anthropic import AnthropicAdapter
    assert isinstance(adapter, AnthropicAdapter)
    assert adapter.client.default_headers.get("X-Proxy-Token") == "abc123"


# ---------------------------------------------------------------------------
# `lyre serve` startup reachability check — regression for the
# `os.getenv(None)` TypeError that hit users with header-only configs
# before this fix.
# ---------------------------------------------------------------------------


def test_serve_reachability_api_key_mode(monkeypatch) -> None:
    from lyre.main import _model_entry_reachable

    monkeypatch.setenv("OPENAI_API_KEY", "sk-set")
    e = fake_entry(provider="openai", auth_env="OPENAI_API_KEY")
    assert _model_entry_reachable(e) is True

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert _model_entry_reachable(e) is False


def test_serve_reachability_header_only_mode(monkeypatch) -> None:
    """Was crashing before — `os.getenv(None)` raises TypeError.
    Header-only entries should be reachable iff headers are set."""
    from lyre.main import _model_entry_reachable

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with_headers = fake_entry(
        provider="openai",
        auth_env=None,
        headers=(("X-Custom-Auth", "token"),),
    )
    assert _model_entry_reachable(with_headers) is True

    no_auth = fake_entry(provider="openai", auth_env=None, headers=())
    assert _model_entry_reachable(no_auth) is False


def test_serve_reachability_stacked_mode_needs_api_key(monkeypatch) -> None:
    """Stacked auth — the API key is still required (headers can't
    substitute for the key the SDK builds Authorization from)."""
    from lyre.main import _model_entry_reachable

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    e = fake_entry(
        provider="openai",
        auth_env="OPENAI_API_KEY",
        headers=(("OpenAI-Organization", "org-x"),),
    )
    # Headers present but key missing → still not reachable.
    assert _model_entry_reachable(e) is False
    monkeypatch.setenv("OPENAI_API_KEY", "sk-set")
    assert _model_entry_reachable(e) is True
