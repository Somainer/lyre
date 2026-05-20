"""Model Registry — ground truth for provider/model entries.

Loads `model_registry.yaml` into typed entries that the Router consults.
Capability tags are free-form strings — no validation against an enum.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

Tier = Literal["flagship", "workhorse", "cheap"]
RegistryStatus = Literal["enabled", "disabled"]


@dataclass(frozen=True)
class ModelEndpoint:
    base_url: str | None
    auth_env: str

    @classmethod
    def from_dict(cls, d: dict | None) -> ModelEndpoint:
        d = d or {}
        return cls(
            base_url=d.get("base_url") or None,
            auth_env=d.get("auth_env") or "ANTHROPIC_API_KEY",
        )


@dataclass(frozen=True)
class ModelCost:
    input: float | None
    output: float | None

    @classmethod
    def from_dict(cls, d: dict | None) -> ModelCost:
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


def _validate_entry(d: dict, idx: int) -> None:
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


def default_registry_path() -> Path:
    """Walk up from this module to find <repo_root>/model_registry.yaml."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "model_registry.yaml"
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "model_registry.yaml not found in any parent of "
        f"{here} — expected at repo root."
    )
