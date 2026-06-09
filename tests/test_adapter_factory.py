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
    """Header values use shell-style ${ENV_VAR} so secrets stay out of
    config.toml. Substitution applies to every ${NAME} occurrence in
    the value — supports the common `Bearer ${TOKEN}` pattern."""
    monkeypatch.setenv("PROXY_TOKEN", "secret-jwt")
    from lyre.runtime.model_registry import ModelEndpoint

    # Whole-value placeholder
    ep_pure = ModelEndpoint.from_dict({
        "headers": {"X-Auth": "${PROXY_TOKEN}"},
    })
    assert dict(ep_pure.headers)["X-Auth"] == "secret-jwt"

    # Partial placeholder — the standard `Authorization: Bearer …` form
    ep_partial = ModelEndpoint.from_dict({
        "headers": {"Authorization": "Bearer ${PROXY_TOKEN}"},
    })
    assert (
        dict(ep_partial.headers)["Authorization"] == "Bearer secret-jwt"
    )

    # Multiple placeholders in one value
    monkeypatch.setenv("ORG", "acme")
    ep_multi = ModelEndpoint.from_dict({
        "headers": {"X-Org-Token": "${ORG}/${PROXY_TOKEN}"},
    })
    assert dict(ep_multi.headers)["X-Org-Token"] == "acme/secret-jwt"

    # Literal `$` survives when not in `${…}` form
    ep_lit = ModelEndpoint.from_dict({
        "headers": {"X-Plain": "$NOT_A_PLACEHOLDER"},
    })
    assert dict(ep_lit.headers)["X-Plain"] == "$NOT_A_PLACEHOLDER"


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


# ---------------------------------------------------------------------------
# openai provider + endpoint.api="responses" — Responses API surface.
# Same provider as Chat Completions; dialect picked by `endpoint.api`.
# ---------------------------------------------------------------------------


def test_factory_makes_openai_responses_adapter(monkeypatch) -> None:
    """provider='openai' + endpoint.api='responses' routes to
    OpenAIResponsesAdapter — covers OpenAI's newer /v1/responses
    surface and corporate proxies that mirror it."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    entry = fake_entry(
        provider="openai",
        api="responses",
        auth_env="OPENAI_API_KEY",
        base_url="https://internal-proxy.example/responses",
    )
    adapter = AdapterFactory().make(entry)
    from lyre.adapter.openai_responses import OpenAIResponsesAdapter
    assert isinstance(adapter, OpenAIResponsesAdapter)


def test_factory_openai_default_api_is_chat_completions(monkeypatch) -> None:
    """No explicit `api` → default 'chat-completions' → OpenAIAdapter
    (NOT OpenAIResponsesAdapter). Guards against accidentally routing
    every OpenAI-family entry through Responses."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    entry = fake_entry(provider="openai", auth_env="OPENAI_API_KEY")
    adapter = AdapterFactory().make(entry)
    from lyre.adapter.openai import OpenAIAdapter
    from lyre.adapter.openai_responses import OpenAIResponsesAdapter
    assert isinstance(adapter, OpenAIAdapter)
    assert not isinstance(adapter, OpenAIResponsesAdapter)


def test_factory_openai_responses_header_only(monkeypatch) -> None:
    """Header-only auth path works with the Responses adapter too —
    that's the actual production setup for bytedance / similar
    internal gateways."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    entry = fake_entry(
        provider="openai",
        api="responses",
        auth_env=None,
        headers=(("Authorization", "Bearer abc"),),
        base_url="https://gateway.internal/responses",
    )
    adapter = AdapterFactory().make(entry)
    from lyre.adapter.openai_responses import OpenAIResponsesAdapter
    assert isinstance(adapter, OpenAIResponsesAdapter)
    assert adapter.client.default_headers.get("Authorization") == "Bearer abc"


# ---------------------------------------------------------------------------
# R1: tunable transient-error retry budget, threaded factory → adapter → SDK
# client. The SDK retries 429/529/500/timeout with backoff before raising.
# ---------------------------------------------------------------------------


def test_factory_threads_max_retries_to_anthropic_client(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    entry = fake_entry(provider="anthropic", auth_env="ANTHROPIC_API_KEY")
    adapter = AdapterFactory(max_retries=7).make(entry)
    assert adapter.client.max_retries == 7


def test_factory_threads_max_retries_to_openai_client(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    entry = fake_entry(provider="openai", auth_env="OPENAI_API_KEY")
    adapter = AdapterFactory(max_retries=5).make(entry)
    assert adapter.client.max_retries == 5


def test_factory_default_leaves_sdk_retry_default(monkeypatch) -> None:
    """No max_retries → the adapter doesn't override → the SDK's own default
    (2) stands, so behavior is unchanged unless the owner opts in."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    entry = fake_entry(provider="anthropic", auth_env="ANTHROPIC_API_KEY")
    adapter = AdapterFactory().make(entry)
    assert adapter.client.max_retries == 2


def test_endpoint_api_field_validates_value() -> None:
    """ModelEndpoint.from_dict must reject unknown `api` values up-front
    so config typos surface at startup, not on first request."""
    from lyre.runtime.model_registry import ModelEndpoint
    with pytest.raises(ValueError, match="endpoint.api"):
        ModelEndpoint.from_dict({"api": "completions"})  # typo
    with pytest.raises(ValueError, match="endpoint.api"):
        ModelEndpoint.from_dict({"api": "anthropic-messages"})


def test_endpoint_api_field_defaults_to_chat_completions() -> None:
    """Empty/missing `api` → default 'chat-completions' for backward
    compat with configs that predate the Responses surface."""
    from lyre.runtime.model_registry import ModelEndpoint
    assert ModelEndpoint.from_dict({}).api == "chat-completions"
    assert ModelEndpoint.from_dict(None).api == "chat-completions"
    assert (
        ModelEndpoint.from_dict({"api": "responses"}).api == "responses"
    )


def test_responses_adapter_input_conversion_text_only() -> None:
    """User+assistant text messages become Responses `input` items with
    the right `input_text` / `output_text` content-part types. System
    messages drop here — they ride on `instructions` instead."""
    from lyre.adapter.llm_adapter import LyreContentBlock, LyreMessage
    from lyre.adapter.openai_responses import OpenAIResponsesAdapter

    msgs = [
        LyreMessage(
            role="system",
            content=[LyreContentBlock(type="text", text="be helpful")],
        ),
        LyreMessage(
            role="user",
            content=[LyreContentBlock(type="text", text="hi")],
        ),
        LyreMessage(
            role="assistant",
            content=[LyreContentBlock(type="text", text="hello there")],
        ),
    ]
    out = OpenAIResponsesAdapter._lyre_to_responses_input(msgs)
    assert len(out) == 2  # system dropped
    assert out[0] == {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "hi"}],
    }
    assert out[1] == {
        "type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": "hello there"}],
    }


def test_responses_adapter_input_conversion_tool_use_round_trip() -> None:
    """assistant `tool_use` → `function_call` input item;
       user `tool_result` → `function_call_output` input item.
    The call_id round-trips so the upstream model can match results
    to calls."""
    from lyre.adapter.llm_adapter import LyreContentBlock, LyreMessage
    from lyre.adapter.openai_responses import OpenAIResponsesAdapter

    msgs = [
        LyreMessage(
            role="assistant",
            content=[
                LyreContentBlock(type="text", text="checking"),
                LyreContentBlock(
                    type="tool_use",
                    tool_use_id="call_abc",
                    tool_name="lookup",
                    tool_input={"key": "x"},
                ),
            ],
        ),
        LyreMessage(
            role="user",
            content=[
                LyreContentBlock(
                    type="tool_result",
                    tool_use_id="call_abc",
                    tool_result={"found": True},
                ),
            ],
        ),
    ]
    out = OpenAIResponsesAdapter._lyre_to_responses_input(msgs)
    assert len(out) == 3
    # Assistant text message
    assert out[0]["role"] == "assistant"
    # function_call item next
    assert out[1] == {
        "type": "function_call",
        "call_id": "call_abc",
        "name": "lookup",
        "arguments": '{"key": "x"}',
    }
    # User tool_result becomes function_call_output
    assert out[2] == {
        "type": "function_call_output",
        "call_id": "call_abc",
        "output": '{"found": true}',
    }


def test_responses_adapter_tool_spec_is_flat() -> None:
    """Responses API tools are flat ({type, name, description,
    parameters}) — no nested `function` wrapper like Chat Completions."""
    from lyre.adapter.llm_adapter import LyreToolSpec
    from lyre.adapter.openai_responses import OpenAIResponsesAdapter

    t = LyreToolSpec(
        name="greet",
        description="say hi",
        input_schema={"type": "object", "properties": {}},
    )
    out = OpenAIResponsesAdapter._tool_to_responses(t)
    assert out == {
        "type": "function",
        "name": "greet",
        "description": "say hi",
        "parameters": {"type": "object", "properties": {}},
    }
    assert "function" not in out  # no nested wrapper
