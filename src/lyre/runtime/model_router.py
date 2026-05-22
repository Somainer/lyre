"""Model Router — selects an ordered list of candidate ModelEntry for a wakeup.

Per Q9, the selection algorithm:
  1. **Override**: if `LYRE_MODEL_OVERRIDE` is set → return only that entry (or
     raise if it's not in the registry). The override beats persona preference
     and tier matching.
  2. **Hard filter**: drop entries that are
       - disabled (status='disabled' in yaml)
       - missing any `requires` capability
  3. **Soft rank**: ascending
       a. unhealthy circuit (opened) sinks to the bottom
       b. tier match: persona.tier == entry.tier ranked before mismatched tiers
       c. `prefer` ordering: entries listed in persona.prefer get their index
          as rank; unlisted get a large sentinel
  4. Return the full ranked list. Agent loop tries them in order on per-turn
     transient errors (rate limit / 5xx).

Q9.4: cost is NOT consulted in MVP routing decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from .adapter_factory import entry_reachable
from .health_tracker import HealthTracker
from .model_registry import ModelEntry, ModelRegistry, Tier

log = structlog.get_logger()


@dataclass(frozen=True)
class ModelPreference:
    """What a persona declares about which model it wants."""

    tier: Tier
    requires: tuple[str, ...] = ()
    prefer: tuple[str, ...] = ()  # ranked list of model ids

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> ModelPreference | None:
        if not d:
            return None
        tier = d.get("tier")
        if tier not in ("flagship", "workhorse", "cheap"):
            raise ValueError(
                f"model_preference.tier must be flagship/workhorse/cheap, got {tier!r}"
            )
        return cls(
            tier=tier,
            requires=tuple(d.get("requires") or ()),
            prefer=tuple(d.get("prefer") or ()),
        )


class NoEligibleModelError(RuntimeError):
    """No registry entry satisfies the persona's preference + override."""


@dataclass
class ModelRouter:
    registry: ModelRegistry
    health: HealthTracker
    override_id: str | None = None  # LYRE_MODEL_OVERRIDE

    def select(self, pref: ModelPreference) -> list[ModelEntry]:
        """Return ranked candidates the agent loop should try in order."""
        if self.override_id:
            entry = self.registry.by_id(self.override_id)
            if entry is None:
                raise NoEligibleModelError(
                    f"LYRE_MODEL_OVERRIDE={self.override_id!r} but no such entry "
                    f"in registry. Known ids: {[e.id for e in self.registry.entries]}"
                )
            return [entry]

        # First pass: requires-capability filter (the persona declares
        # what features it depends on, e.g. tool_use + streaming).
        candidates = [
            e
            for e in self.registry.enabled()
            if e.supports(list(pref.requires))
        ]
        if not candidates:
            raise NoEligibleModelError(
                f"No enabled model in registry supports requires={list(pref.requires)}. "
                f"Persona tier={pref.tier}. "
                "Check model_registry.yaml or persona model_preference."
            )

        # Second pass: drop entries we have no auth for. Without this,
        # a persona whose `prefer` names a shipped model (e.g.
        # anthropic.claude-opus-4-7) wins ranking even when
        # ANTHROPIC_API_KEY is unset — the agent_loop then trips on
        # adapter_factory and the whole task fails, even though the
        # user HAS configured a different reachable model. Filtering
        # at the router lets the user-configured entry (with custom
        # headers, or a different env var that IS set) bubble up.
        unreachable = [e for e in candidates if not entry_reachable(e)]
        candidates = [e for e in candidates if entry_reachable(e)]
        if not candidates:
            blocked_ids = ", ".join(e.id for e in unreachable)
            raise NoEligibleModelError(
                f"No reachable model can satisfy persona "
                f"requires={list(pref.requires)} tier={pref.tier}. "
                f"Entries that match capability-wise but lack auth: "
                f"{blocked_ids}. Either set the relevant API-key env "
                f"var, configure custom headers in "
                f"~/.lyre/config.toml [models.endpoint.headers], or "
                f"add a different [[models]] entry the router can "
                f"reach."
            )
        if unreachable:
            log.debug(
                "model_router_filtered_unreachable",
                dropped=[e.id for e in unreachable],
                remaining=[e.id for e in candidates],
            )

        prefer_index = {mid: i for i, mid in enumerate(pref.prefer)}
        large = len(pref.prefer) + 999

        def rank(e: ModelEntry) -> tuple[int, int, int, str]:
            unhealthy = 0 if self.health.is_available(e.id) else 1
            tier_match = 0 if e.tier == pref.tier else 1
            prefer_pos = prefer_index.get(e.id, large)
            # last key (id) is for stable, deterministic ordering when tied
            return (unhealthy, prefer_pos, tier_match, e.id)

        ranked = sorted(candidates, key=rank)
        log.debug(
            "model_router_select",
            persona_tier=pref.tier,
            persona_requires=list(pref.requires),
            persona_prefer=list(pref.prefer),
            override=self.override_id,
            ranked=[e.id for e in ranked],
        )
        return ranked
