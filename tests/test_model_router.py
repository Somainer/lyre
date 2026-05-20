"""Tests for ModelRouter.select()."""

from __future__ import annotations

import pytest

from lyre.runtime.health_tracker import HealthTracker
from lyre.runtime.model_router import (
    ModelPreference,
    ModelRouter,
    NoEligibleModelError,
)

from .helpers import fake_entry, fake_registry


def _pref(
    tier: str = "workhorse",
    requires: tuple[str, ...] = ("tool_use",),
    prefer: tuple[str, ...] = (),
) -> ModelPreference:
    return ModelPreference(tier=tier, requires=requires, prefer=prefer)


def test_select_filters_by_requires_capability() -> None:
    reg = fake_registry(
        fake_entry(id="a", capabilities=("tool_use",), tier="workhorse"),
        fake_entry(id="b", capabilities=("streaming",), tier="workhorse"),
    )
    router = ModelRouter(reg, HealthTracker())
    ranked = router.select(_pref(requires=("tool_use",)))
    assert [e.id for e in ranked] == ["a"]


def test_select_raises_when_no_entry_supports_requires() -> None:
    reg = fake_registry(
        fake_entry(id="a", capabilities=("tool_use",))
    )
    router = ModelRouter(reg, HealthTracker())
    with pytest.raises(NoEligibleModelError):
        router.select(_pref(requires=("vision",)))


def test_select_skips_disabled_entries() -> None:
    reg = fake_registry(
        fake_entry(id="a", capabilities=("tool_use",), status="disabled"),
        fake_entry(id="b", capabilities=("tool_use",)),
    )
    router = ModelRouter(reg, HealthTracker())
    ranked = router.select(_pref())
    assert [e.id for e in ranked] == ["b"]


def test_prefer_list_ranks_above_others() -> None:
    reg = fake_registry(
        fake_entry(id="a", capabilities=("tool_use",), tier="workhorse"),
        fake_entry(id="b", capabilities=("tool_use",), tier="workhorse"),
        fake_entry(id="c", capabilities=("tool_use",), tier="workhorse"),
    )
    router = ModelRouter(reg, HealthTracker())
    ranked = router.select(_pref(prefer=("c", "a")))
    assert [e.id for e in ranked] == ["c", "a", "b"]


def test_tier_match_ranks_above_mismatch() -> None:
    reg = fake_registry(
        fake_entry(id="cheap-one", capabilities=("tool_use",), tier="cheap"),
        fake_entry(id="match", capabilities=("tool_use",), tier="workhorse"),
    )
    router = ModelRouter(reg, HealthTracker())
    ranked = router.select(_pref(tier="workhorse"))
    assert ranked[0].id == "match"


def test_unhealthy_models_sink_to_bottom() -> None:
    reg = fake_registry(
        fake_entry(id="a", capabilities=("tool_use",)),
        fake_entry(id="b", capabilities=("tool_use",)),
    )
    health = HealthTracker()
    for _ in range(3):
        health.mark_failure("a")
    router = ModelRouter(reg, health)
    ranked = router.select(_pref())
    assert ranked[0].id == "b"
    assert ranked[1].id == "a"


def test_override_returns_only_overridden_entry() -> None:
    reg = fake_registry(
        fake_entry(id="a", tier="flagship", capabilities=("tool_use",)),
        fake_entry(id="b", tier="cheap", capabilities=("tool_use",)),
    )
    router = ModelRouter(reg, HealthTracker(), override_id="b")
    ranked = router.select(_pref(tier="flagship"))
    assert [e.id for e in ranked] == ["b"]


def test_override_raises_if_unknown() -> None:
    reg = fake_registry(fake_entry(id="a"))
    router = ModelRouter(reg, HealthTracker(), override_id="ghost")
    with pytest.raises(NoEligibleModelError):
        router.select(_pref())


def test_preference_from_dict_validates_tier() -> None:
    with pytest.raises(ValueError):
        ModelPreference.from_dict({"tier": "ultra"})


def test_preference_from_dict_returns_none_for_empty() -> None:
    assert ModelPreference.from_dict(None) is None
    assert ModelPreference.from_dict({}) is None
