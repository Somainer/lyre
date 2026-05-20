"""Kill-point injection for chaos tests (拔线测试).

In production this is a no-op (the default `KillSwitch()` instance has
`fire_at=None`). Tests construct one with `fire_at=<point_name>` and pass it
to Scheduler / AgentLoop / OutboxDispatcher. When the matching `check()` call
fires, `SimulatedKill` is raised — a `BaseException` so it skips ordinary
`except Exception:` blocks, and a `sys.exc_info()` guard in `finally` blocks
lets cleanup logic skip itself to simulate "the process actually died".

The 4 named kill points:

  - "before_action"            after lease claim, before agent_loop.run
  - "mid_action_after_tool"    inside agent_loop, after every tool dispatch
  - "post_action_pre_report"   after agent_loop.run completes, before any
                                outbox/report finalisation
  - "post_outbox_pre_dispatch" after the agent's outbox row(s) are committed,
                                before the OutboxDispatcher.tick that would
                                deliver them
"""

from __future__ import annotations

import sys
from dataclasses import dataclass


class SimulatedKill(BaseException):
    """Raised by KillSwitch to mimic abrupt process death.

    BaseException so that `except Exception:` in normal error-handling
    paths does NOT catch it. Cleanup `finally` blocks must check
    `sys.exc_info()` and skip themselves if a SimulatedKill is propagating.
    """


@dataclass
class KillSwitch:
    """One-shot kill at a named point. Wires through Scheduler / AgentLoop /
    OutboxDispatcher constructors.
    """

    fire_at: str | None = None
    fired: bool = False
    # Optional callback fired alongside the kill (e.g. for tests that need to
    # snapshot DB state at the exact moment of death). Receives the point name.
    on_fire: object | None = None  # Callable[[str], None] | None

    def check(self, point: str) -> None:
        if self.fired or self.fire_at != point:
            return
        self.fired = True
        if self.on_fire is not None:
            self.on_fire(point)  # type: ignore[operator]
        raise SimulatedKill(f"kill at {point}")

    def disable(self) -> None:
        """Used after a 'restart': the same KillSwitch instance shouldn't fire
        again on the second run."""
        self.fired = True
        self.fire_at = None


def is_simulated_kill_in_flight() -> bool:
    """Use in `finally` blocks to detect a SimulatedKill propagating.

    Real process death never runs `finally` either, so when this returns True
    we skip cleanup (release_lease, worktree wipe, etc.) on purpose.
    """
    exc = sys.exc_info()[1]
    return isinstance(exc, SimulatedKill)
