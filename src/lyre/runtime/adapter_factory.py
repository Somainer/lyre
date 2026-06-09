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
from typing import Any

from ..adapter.anthropic import AnthropicAdapter
from ..adapter.llm_adapter import LLMAdapter
from ..adapter.openai import OpenAIAdapter
from ..adapter.openai_responses import OpenAIResponsesAdapter
from .blob_store import BlobStore
from .model_registry import ModelEntry


class AdapterFactoryError(RuntimeError):
    """Failed to construct an adapter for the given model entry."""


def entry_reachable(entry: ModelEntry) -> bool:
    """Cheap pre-flight: would ``AdapterFactory.make(entry)`` succeed
    right now?

    Used in two places (kept out of the factory itself so it stays a
    pure "build OR raise" path):

      * `lyre serve` / `lyre models list` for startup-time
        reachability hints in the CLI output.
      * The model router to filter out entries that have no usable
        auth from the candidate list — so a persona whose `prefer`
        names a shipped model with no API key configured will still
        fall through to a user-configured entry that does have auth.

    Rules:
      * API-key mode (auth_env set): env var must be non-empty.
      * Header-only mode (auth_env None, headers set): always
        "reachable" — we don't try to ping the proxy at startup.
      * Stacked (both set): API key is still required.
      * Neither set: not reachable (adapter factory will raise too).
    """
    auth_env = entry.endpoint.auth_env
    if auth_env:
        return bool(os.getenv(auth_env))
    return bool(entry.endpoint.headers)


class AdapterFactory:
    """Stateless: cache-free instantiation. The SDKs (Anthropic / OpenAI) each
    manage their own connection pools, so creating one client per wakeup is
    fine.

    ``blob_store`` (optional) is forwarded to each constructed adapter
    so it can resolve ``image`` / ``document`` content blocks at
    send-time. Leave unset in contexts that never dispatch multimodal
    content (most unit tests).
    """

    def __init__(
        self, blob_store: BlobStore | None = None, max_retries: int | None = None
    ) -> None:
        self._blob_store = blob_store
        # R1: forwarded to every adapter → the provider SDK client's retry
        # budget for transient errors (429/529/500/timeout). None leaves the
        # SDK default in place.
        self._max_retries = max_retries

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
                blob_store=self._blob_store,
                max_retries=self._max_retries,
            )
        if entry.provider == "openai":
            # Within the OpenAI family the `endpoint.api` field picks
            # the dialect — `chat-completions` (the historical
            # default, what OpenRouter / Together / vLLM-OAI expose)
            # or `responses` (OpenAI's newer surface, also some
            # internal corporate gateways like bytedance ai-coder).
            common_kwargs: dict[str, Any] = {
                "api_key": sdk_api_key,
                "base_url": entry.endpoint.base_url,
                "extra_headers": extra_headers or None,
                "blob_store": self._blob_store,
                "max_retries": self._max_retries,
            }
            if entry.endpoint.api == "responses":
                return OpenAIResponsesAdapter(**common_kwargs)
            return OpenAIAdapter(**common_kwargs)
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
