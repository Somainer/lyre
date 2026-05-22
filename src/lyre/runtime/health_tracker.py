"""Health tracker for model entries.

MVP: in-memory circuit breaker. Per Q9 decision:
  - 60-second sliding window of failures per model_id
  - 3 failures within the window → open circuit (model is `failing`)
  - 180-second cooldown → half-open: next call attempted; if it succeeds we
    close again
  - Not persisted: process restart resets all to healthy. Adding a
    `model_health` table is left to a future iteration.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

CircuitState = Literal["closed", "open", "half_open"]

_WINDOW_S = 60.0
_FAIL_THRESHOLD = 3
_COOLDOWN_S = 180.0


@dataclass
class _ModelState:
    failures: deque[float] = field(default_factory=deque)
    opened_at: float | None = None
    last_ok_at: float | None = None


class HealthTracker:
    """Per-process, lock-free (single-threaded asyncio assumed)."""

    def __init__(
        self,
        window_s: float = _WINDOW_S,
        fail_threshold: int = _FAIL_THRESHOLD,
        cooldown_s: float = _COOLDOWN_S,
        now_fn: Callable[[], float] | None = None,
    ):
        self.window_s = window_s
        self.fail_threshold = fail_threshold
        self.cooldown_s = cooldown_s
        self._now = now_fn or time.time
        self._state: dict[str, _ModelState] = {}

    def _get(self, model_id: str) -> _ModelState:
        st = self._state.get(model_id)
        if st is None:
            st = _ModelState()
            self._state[model_id] = st
        return st

    def _prune(self, st: _ModelState, now: float) -> None:
        cutoff = now - self.window_s
        while st.failures and st.failures[0] < cutoff:
            st.failures.popleft()

    def state(self, model_id: str) -> CircuitState:
        now = self._now()
        st = self._get(model_id)
        if st.opened_at is None:
            return "closed"
        if (now - st.opened_at) >= self.cooldown_s:
            return "half_open"
        return "open"

    def is_available(self, model_id: str) -> bool:
        """True iff we should attempt this model right now."""
        s = self.state(model_id)
        return s in ("closed", "half_open")

    def mark_failure(self, model_id: str) -> None:
        now = self._now()
        st = self._get(model_id)
        self._prune(st, now)
        st.failures.append(now)
        if len(st.failures) >= self.fail_threshold and st.opened_at is None:
            st.opened_at = now

    def mark_success(self, model_id: str) -> None:
        now = self._now()
        st = self._get(model_id)
        st.last_ok_at = now
        # Recovery: clear failures and close the circuit if open / half_open.
        st.failures.clear()
        st.opened_at = None

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """For logging / debugging."""
        out: dict[str, dict[str, Any]] = {}
        for mid, st in self._state.items():
            out[mid] = {
                "state": self.state(mid),
                "recent_failures": len(st.failures),
                "opened_at": st.opened_at,
                "last_ok_at": st.last_ok_at,
            }
        return out
