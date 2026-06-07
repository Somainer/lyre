"""A1 lease heartbeat: while a wakeup runs, the scheduler renews its task
lease; if the lease was stolen (recovery re-dispatched a long-stalled task),
renew_lease self-fences (WHERE lease_holder=?) and the heartbeat asks the loop
to stop so a superseded worker doesn't keep mutating shared FS state. A wall
budget bounds how long it will renew.

These use real (~1s) sleeps rather than monkeypatching asyncio.sleep — a global
no-op sleep deadlocks aiosqlite's event-loop teardown.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lyre.persistence.models import Persona, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.scheduler.scheduler import Scheduler


def _capturing_loop() -> tuple[SimpleNamespace, list[tuple[str, str]]]:
    stops: list[tuple[str, str]] = []
    loop = SimpleNamespace(
        request_stop=lambda status, reason: stops.append((status, reason))
    )
    return loop, stops


async def _seed_leased_task(repos: SqliteRepositories) -> str:
    await repos.personas.upsert(
        Persona(name="worker", role_description="w", system_prompt="w")
    )
    task_id = await repos.tasks.create(
        TaskSpec(persona_name="worker", goal="g", acceptance="a")
    )
    assert await repos.tasks.claim_lease(task_id, "wakeup-a", duration_sec=60)
    return task_id


@pytest.mark.asyncio
async def test_heartbeat_stops_loop_when_lease_lost(
    repos: SqliteRepositories,
) -> None:
    task_id = await _seed_leased_task(repos)
    # Simulate the lease being lost: release it, so renew_lease for wakeup-a
    # (WHERE lease_holder='wakeup-a') now matches no row → returns False.
    await repos.tasks.release_lease(task_id, "wakeup-a")

    loop, stops = _capturing_loop()
    fake_self = SimpleNamespace(
        config=SimpleNamespace(wakeup_wall_budget_s=0), repos=repos
    )
    # lease_duration_s=1 → renew interval floors at 1.0s; one real tick suffices.
    await Scheduler._lease_heartbeat(
        fake_self, task_id, "wakeup-a", lease_duration_s=1, agent_loop=loop
    )

    assert stops and stops[0][0] == "needs_continuation"
    assert "lease lost" in stops[0][1]


@pytest.mark.asyncio
async def test_heartbeat_stops_loop_when_wall_budget_exceeded(
    repos: SqliteRepositories,
) -> None:
    task_id = await _seed_leased_task(repos)  # lease stays held → renew succeeds

    loop, stops = _capturing_loop()
    # wall budget 1s; the first real ~1s tick crosses it, so the heartbeat
    # stops even though the lease is still validly held.
    fake_self = SimpleNamespace(
        config=SimpleNamespace(wakeup_wall_budget_s=1), repos=repos
    )
    await Scheduler._lease_heartbeat(
        fake_self, task_id, "wakeup-a", lease_duration_s=3, agent_loop=loop
    )

    assert stops and stops[0][0] == "needs_continuation"
    assert "wall budget" in stops[0][1]
