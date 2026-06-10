"""Tests for the in-memory HealthTracker circuit breaker."""

from __future__ import annotations

from lyre.runtime.health_tracker import HealthTracker


class _Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_unknown_model_is_healthy_by_default() -> None:
    h = HealthTracker()
    assert h.is_available("any.model")
    assert h.state("any.model") == "closed"


def test_failures_below_threshold_keep_closed() -> None:
    clock = _Clock()
    h = HealthTracker(now_fn=clock)
    for _ in range(2):
        h.mark_failure("m")
    assert h.state("m") == "closed"
    assert h.is_available("m")


def test_failures_at_threshold_open_circuit() -> None:
    clock = _Clock()
    h = HealthTracker(now_fn=clock)
    for _ in range(3):
        h.mark_failure("m")
    assert h.state("m") == "open"
    assert not h.is_available("m")


def test_cooldown_transitions_to_half_open() -> None:
    clock = _Clock()
    h = HealthTracker(cooldown_s=180.0, now_fn=clock)
    for _ in range(3):
        h.mark_failure("m")
    assert h.state("m") == "open"
    # Advance past cooldown.
    clock.t += 200
    assert h.state("m") == "half_open"
    assert h.is_available("m")  # half-open is attemptable


def test_mark_success_closes_circuit_and_clears_failures() -> None:
    clock = _Clock()
    h = HealthTracker(now_fn=clock)
    for _ in range(3):
        h.mark_failure("m")
    h.mark_success("m")
    assert h.state("m") == "closed"
    # Subsequent failures restart from zero.
    h.mark_failure("m")
    h.mark_failure("m")
    assert h.state("m") == "closed"


def test_failure_during_half_open_reopens_circuit() -> None:
    """A still-dead model probed in half_open must re-enter a full
    cooldown. opened_at used to be set only once, so after the first
    cooldown the state stuck at half_open (available) forever and the
    breaker protected for exactly one window per process lifetime."""
    clock = _Clock()
    h = HealthTracker(cooldown_s=180.0, now_fn=clock)
    for _ in range(3):
        h.mark_failure("m")
    clock.t += 200
    assert h.state("m") == "half_open"
    # The half-open probe fails → circuit re-opens for a fresh cooldown.
    h.mark_failure("m")
    assert h.state("m") == "open"
    assert not h.is_available("m")
    # And the new cooldown counts from the re-open, not the original open.
    clock.t += 100
    assert h.state("m") == "open"
    clock.t += 100
    assert h.state("m") == "half_open"


def test_success_during_half_open_closes_circuit() -> None:
    clock = _Clock()
    h = HealthTracker(cooldown_s=180.0, now_fn=clock)
    for _ in range(3):
        h.mark_failure("m")
    clock.t += 200
    assert h.state("m") == "half_open"
    h.mark_success("m")
    assert h.state("m") == "closed"


def test_old_failures_are_pruned_from_window() -> None:
    clock = _Clock()
    h = HealthTracker(window_s=60.0, now_fn=clock)
    h.mark_failure("m")
    h.mark_failure("m")
    # Move forward past window then add one more — old two are pruned.
    clock.t += 90
    h.mark_failure("m")
    assert h.state("m") == "closed"


def test_snapshot_returns_per_model_summary() -> None:
    clock = _Clock()
    h = HealthTracker(now_fn=clock)
    h.mark_failure("m1")
    h.mark_success("m2")
    snap = h.snapshot()
    assert snap["m1"]["state"] == "closed"
    assert snap["m1"]["recent_failures"] == 1
    assert snap["m2"]["state"] == "closed"
    assert snap["m2"]["last_ok_at"] is not None
