"""Map a ModelEntry → an LLMAdapter instance.

Q9.6: each provider has its own auth env var, named in the registry entry
under `endpoint.auth_env`. The factory reads that env var at construction time.

Supported providers:
  - `anthropic` — Anthropic /v1/messages shape. Also serves DeepSeek's
    Anthropic-compat endpoint via base_url repointing.
  - `openai` — OpenAI /v1/chat/completions shape. Covers OpenAI proper,
    DeepSeek's OpenAI-compat endpoint, OpenRouter, Together, vLLM-served
    hosts, and similar OAI-compat providers.
"""

from __future__ import annotations

import os

from ..adapter.anthropic import AnthropicAdapter
from ..adapter.llm_adapter import LLMAdapter
from ..adapter.openai import OpenAIAdapter
from .model_registry import ModelEntry


class AdapterFactoryError(RuntimeError):
    """Failed to construct an adapter for the given model entry."""


class AdapterFactory:
    """Stateless: cache-free instantiation. The SDKs (Anthropic / OpenAI) each
    manage their own connection pools, so creating one client per wakeup is
    fine.
    """

    def make(self, entry: ModelEntry) -> LLMAdapter:
        # Two auth modes; either or both can be configured per entry:
        #   1. API key from env var (entry.endpoint.auth_env) — the SDK
        #      sets Bearer / x-api-key based on its provider.
        #   2. Custom HTTP headers (entry.endpoint.headers) — sent on
        #      every request via the SDK's default_headers. Useful for
        #      proxies/gateways with non-standard auth schemes.
        api_key: str | None = None
        if entry.endpoint.auth_env:
            api_key = os.getenv(entry.endpoint.auth_env)
            if not api_key:
                raise AdapterFactoryError(
                    f"Model {entry.id!r}: env var "
                    f"{entry.endpoint.auth_env!r} is unset. Set it (or "
                    f"switch to header-only auth) before dispatching "
                    f"tasks that may use this model."
                )

        extra_headers = entry.endpoint.headers_dict

        if not api_key and not extra_headers:
            raise AdapterFactoryError(
                f"Model {entry.id!r}: no auth configured. Set either "
                f"endpoint.auth_env (API-key mode) or endpoint.headers "
                f"(custom-header mode), or both."
            )

        # SDK constructors require a non-empty api_key string even when
        # we're authenticating via custom headers — provider SDKs use a
        # sentinel like "EMPTY" / "PLACEHOLDER" in that case. The actual
        # auth comes from the custom headers we pass through.
        sdk_api_key = api_key or "PLACEHOLDER"

        if entry.provider == "anthropic":
            return AnthropicAdapter(
                api_key=sdk_api_key,
                base_url=entry.endpoint.base_url,
                extra_headers=extra_headers or None,
            )
        if entry.provider == "openai":
            return OpenAIAdapter(
                api_key=sdk_api_key,
                base_url=entry.endpoint.base_url,
                extra_headers=extra_headers or None,
            )
        raise AdapterFactoryError(
            f"Unknown provider {entry.provider!r} for model {entry.id!r}. "
            f"Supported: 'anthropic', 'openai'."
        )


def model_name_for_provider(entry: ModelEntry) -> str:
    """The string passed to the upstream API as the model identifier.

    Convention: registry id is `<provider_namespace>.<provider_model_name>`.
    The provider only needs the part after the first dot.
    For DeepSeek the SDK sends e.g. 'deepseek-v4-pro'; for Anthropic it sends
    'claude-opus-4-7'.
    """
    if "." in entry.id:
        return entry.id.split(".", 1)[1]
    return entry.id
