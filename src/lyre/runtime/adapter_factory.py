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
        api_key = os.getenv(entry.endpoint.auth_env)
        if not api_key:
            raise AdapterFactoryError(
                f"Model {entry.id!r}: env var {entry.endpoint.auth_env!r} is unset. "
                f"Set it before dispatching tasks that may use this model."
            )
        if entry.provider == "anthropic":
            return AnthropicAdapter(
                api_key=api_key,
                base_url=entry.endpoint.base_url,
            )
        if entry.provider == "openai":
            return OpenAIAdapter(
                api_key=api_key,
                base_url=entry.endpoint.base_url,
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
