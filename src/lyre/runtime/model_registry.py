"""Model Registry — ground truth for provider/model entries.

Loads `model_registry.yaml` into typed entries that the Router consults.
Capability tags are free-form strings — no validation against an enum.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

Tier = Literal["flagship", "workhorse", "cheap"]
RegistryStatus = Literal["enabled", "disabled"]


_VALID_OPENAI_APIS: frozenset[str] = frozenset(
    {"chat-completions", "responses"}
)


@dataclass(frozen=True)
class ModelEndpoint:
    """How to reach + authenticate to one model endpoint.

    Two auth modes are supported (and they can stack — e.g. an API key
    via `auth_env` PLUS extra org/project headers):

      * `auth_env`: name of an environment variable whose value is the
        API key. Passed to the SDK as `api_key=…`, which then sets the
        provider's expected auth header (Bearer / x-api-key / etc.).
        Leave None to skip API-key auth entirely.

      * `headers`: explicit HTTP headers, sent on every request via
        the SDK's `default_headers`. Useful when the proxy / gateway
        in front of the model expects a custom auth scheme (signed
        JWT, mTLS-passthrough token, internal SSO, …) that the
        provider SDK doesn't know about. Values support `${ENV_VAR}`
        interpolation so secrets stay out of config.toml.

    At least one of the two must be set — adapter factory will refuse
    to build a client otherwise.

    `api` picks the dialect within an OpenAI-family provider:

      * `"chat-completions"` (default) — POST /v1/chat/completions
      * `"responses"`                  — POST /v1/responses (newer
                                          API surface; some internal
                                          gateways like bytedance
                                          ai-coder)

    Ignored for non-openai providers.
    """

    base_url: str | None
    auth_env: str | None
    # tuple of (name, value) pairs — frozen so the dataclass stays hashable.
    headers: tuple[tuple[str, str], ...] = ()
    api: str = "chat-completions"

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> ModelEndpoint:
        d = d or {}
        raw_headers = d.get("headers") or {}
        if not isinstance(raw_headers, dict):
            raise ValueError(
                "endpoint.headers must be a dict of header-name → value "
                f"strings; got {type(raw_headers).__name__}"
            )
        headers = tuple(
            (str(k), _interpolate_env(str(v)))
            for k, v in raw_headers.items()
        )
        api = d.get("api") or "chat-completions"
        if api not in _VALID_OPENAI_APIS:
            raise ValueError(
                f"endpoint.api must be one of "
                f"{sorted(_VALID_OPENAI_APIS)}; got {api!r}"
            )
        return cls(
            base_url=d.get("base_url") or None,
            auth_env=d.get("auth_env") or None,
            headers=headers,
            api=api,
        )

    @property
    def headers_dict(self) -> dict[str, str]:
        """Convenience for callers — never None, never empty-of-key
        entries."""
        return {k: v for k, v in self.headers if k and v}


# ${VAR} interpolation — shell-style. Each occurrence of `${NAME}`
# in a header value is replaced with the value of env var NAME (or
# empty if unset). Standard pattern: `Authorization = "Bearer ${TOKEN}"`
# resolves to `Authorization: Bearer <actual-token>`. Literal `$`
# survives because the regex requires the `${`/`}` braces.
_ENV_INTERPOLATE_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _interpolate_env(value: str) -> str:
    """Substitute every `${NAME}` occurrence in `value` with the env
    var's current value. Unset vars resolve to empty string —
    consistent with shell behavior; surfaces as an auth failure at
    request time rather than a startup crash, so the operator can fix
    by exporting the var without restarting the whole process.

    Env vars are read once at registry-load time, so rotating a token
    via env var requires a `lyre serve` restart.
    """
    return _ENV_INTERPOLATE_RE.sub(
        lambda m: os.environ.get(m.group(1), ""),
        value,
    )


@dataclass(frozen=True)
class ModelCost:
    input: float | None
    output: float | None

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> ModelCost:
        d = d or {}
        return cls(input=d.get("input"), output=d.get("output"))


@dataclass(frozen=True)
class ModelEntry:
    id: str
    provider: str
    endpoint: ModelEndpoint
    capabilities: tuple[str, ...]
    tier: Tier
    cost_per_mtok: ModelCost = field(default_factory=lambda: ModelCost(None, None))
    context_window: int | None = None
    status: RegistryStatus = "enabled"

    def supports(self, requires: list[str]) -> bool:
        """True iff every required capability tag is in this entry's tag set."""
        cap_set = set(self.capabilities)
        return all(r in cap_set for r in requires)


@dataclass
class ModelRegistry:
    entries: list[ModelEntry]

    def by_id(self, model_id: str) -> ModelEntry | None:
        for e in self.entries:
            if e.id == model_id:
                return e
        return None

    def enabled(self) -> list[ModelEntry]:
        return [e for e in self.entries if e.status == "enabled"]


def _validate_entry(d: dict[str, Any], idx: int) -> None:
    required = ("id", "provider", "tier", "capabilities")
    for k in required:
        if k not in d:
            raise ValueError(f"model_registry.yaml entry [{idx}] missing field: {k!r}")
    if d["tier"] not in ("flagship", "workhorse", "cheap"):
        raise ValueError(
            f"model_registry.yaml entry [{idx}] '{d['id']}' has invalid tier "
            f"{d['tier']!r} (expected flagship/workhorse/cheap)"
        )
    if not isinstance(d["capabilities"], list):
        raise ValueError(
            f"model_registry.yaml entry [{idx}] '{d['id']}' capabilities must be a list"
        )


def parse_registry(text: str) -> ModelRegistry:
    raw = yaml.safe_load(text) or {}
    models_raw = raw.get("models") or []
    if not isinstance(models_raw, list):
        raise ValueError("model_registry.yaml: top-level 'models' must be a list")

    seen_ids: set[str] = set()
    entries: list[ModelEntry] = []
    for idx, item in enumerate(models_raw):
        if not isinstance(item, dict):
            raise ValueError(f"model_registry.yaml entry [{idx}] must be a mapping")
        _validate_entry(item, idx)
        if item["id"] in seen_ids:
            raise ValueError(f"model_registry.yaml: duplicate id {item['id']!r}")
        seen_ids.add(item["id"])
        entries.append(
            ModelEntry(
                id=item["id"],
                provider=item["provider"],
                endpoint=ModelEndpoint.from_dict(item.get("endpoint")),
                capabilities=tuple(item["capabilities"]),
                tier=item["tier"],
                cost_per_mtok=ModelCost.from_dict(item.get("cost_per_mtok")),
                context_window=item.get("context_window"),
                status=item.get("status", "enabled"),
            )
        )
    return ModelRegistry(entries=entries)


def load_registry(path: str | Path) -> ModelRegistry:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"model registry not found: {p}")
    return parse_registry(p.read_text(encoding="utf-8"))


def load_registry_for_config(cfg: Any) -> ModelRegistry:
    """Load the shipped registry and merge ``cfg.models`` on top.

    ``cfg`` is a ``lyre.config.Config``; typed as ``Any`` to avoid circular
    import. Same-id user entries replace shipped entries; new ids append.
    """
    base = load_registry(default_registry_path())
    user_models = getattr(cfg, "models", None) or []
    return merge_user_entries(base, user_models)


def default_registry_path() -> Path:
    """Packaged shipped registry at ``src/lyre/data/model_registry.yaml``.

    Users do NOT edit this file. To add or override entries, write
    ``[[models]]`` blocks in ``~/.lyre/config.toml`` and pass the resulting
    ``config.models`` list through :func:`merge_user_entries`.
    """
    here = Path(__file__).resolve()
    # src/lyre/runtime/model_registry.py → src/lyre/data/model_registry.yaml
    pkg_root = here.parent.parent
    path = pkg_root / "data" / "model_registry.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"shipped model_registry.yaml missing at {path}"
        )
    return path


def merge_user_entries(
    base: ModelRegistry, user_entries: list[Any]
) -> ModelRegistry:
    """Resolve the effective model registry from shipped defaults +
    the user's ``Config.models`` (config.toml ``[[models]]`` blocks).

    Semantics: **explicit beats shipped**, with field-level fallback
    for same-id entries.

      * No ``user_entries`` (fresh install, no config.toml yet) →
        the shipped registry is returned unchanged. This keeps the
        out-of-box experience working before ``lyre onboard``.
      * Any ``user_entries`` present → those entries ARE the registry.
        Shipped defaults are dropped entirely. If the user wants a
        shipped entry, they list it explicitly in their config —
        otherwise the router won't even consider it as a candidate.
      * **Same-id field-level fallback**: when a user entry's ``id``
        matches a shipped entry, optional fields the user didn't
        specify (``context_window``, ``cost_per_mtok``) fall back to
        the shipped value. This is what lets the operator repoint
        ``base_url`` without re-typing the model's context window —
        and is the fix for the "ctx 0%, compaction never fires"
        symptom that hits any user whose config.toml omits the
        field (the typical case after ``lyre onboard``).

    Field-level merge applies ONLY to optional fields. List-shaped
    fields (``capabilities``) and required ones (``tier``,
    ``provider``, ``endpoint``) are NOT silently inherited —
    explicitly listing them is a user intent we shouldn't override.
    """
    if not user_entries:
        return base
    by_id = {e.id: e for e in base.entries}
    return ModelRegistry(
        entries=[
            _user_entry_to_runtime(raw, shipped=by_id.get(_user_entry_id(raw)))
            for raw in user_entries
        ]
    )


def _user_entry_id(raw: Any) -> str:
    """Pluck the id off either a dict or a typed config.ModelEntry."""
    rid: str = raw["id"] if isinstance(raw, dict) else raw.id
    return rid


def _user_entry_to_runtime(
    raw: Any, shipped: ModelEntry | None = None,
) -> ModelEntry:
    """Convert a ``config.ModelEntry`` (or duck-typed equivalent) into the
    runtime dataclass.

    When ``shipped`` is provided (the registry entry with the same id),
    optional fields the user didn't specify fall back to the shipped
    value. See ``merge_user_entries`` for the full inheritance contract.
    """
    if isinstance(raw, dict):
        d = raw
    else:
        d = {
            "id": raw.id,
            "provider": raw.provider,
            "endpoint": raw.endpoint,
            "capabilities": list(raw.capabilities),
            "tier": raw.tier,
            "status": "enabled" if getattr(raw, "enabled", True) else "disabled",
            "context_window": getattr(raw, "context_window", None),
            "cost_per_mtok": getattr(raw, "cost_per_mtok", None),
        }
    _validate_entry(d, idx=-1)

    # Field-level fallback: user wins when present, shipped fills in
    # otherwise. None / missing is the "not present" signal — explicit
    # 0 or empty dict still counts as user intent.
    ctx_window = d.get("context_window")
    if ctx_window is None and shipped is not None:
        ctx_window = shipped.context_window

    cost_raw = d.get("cost_per_mtok")
    if cost_raw is None and shipped is not None:
        cost = shipped.cost_per_mtok
    else:
        cost = ModelCost.from_dict(cost_raw)

    return ModelEntry(
        id=d["id"],
        provider=d["provider"],
        endpoint=ModelEndpoint.from_dict(d.get("endpoint")),
        capabilities=tuple(d["capabilities"]),
        tier=d["tier"],
        cost_per_mtok=cost,
        context_window=ctx_window,
        status=d.get("status", "enabled"),
    )
