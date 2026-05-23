"""Agents are sequential actors: two pending tasks for the same agent
do not run concurrently.

If they did, two subprocesses would race on the agent's shared
filesystem state (scratchpad, notes, ## Auto-summary log) — lost
updates, interleaved log entries, broken short-term memory. The
scheduler must serialise: claim the first one, leave the second
pending until the first wakeup ends.

Parallelism within a persona uses multiple AGENT INSTANCES
(``analyst/topic-A`` + ``analyst/topic-B``), not multiple wakeups of
one agent.

The DAO-level invariant (``has_active_for_agent``) lives in
WakeupRepository; the scheduling-level invariant (skip pending tasks
whose agent is busy) lives in Scheduler._tick. Both are checked here.
"""

from __future__ import annotations

import pytest

from lyre.persistence.models import Persona, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories

# ---------------------------------------------------------------------------
# DAO: has_active_for_agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_has_active_for_agent_false_when_no_wakeups(
    repos: SqliteRepositories,
) -> None:
    await repos.personas.upsert(
        Persona(name="analyst", role_description="a", system_prompt="a")
    )
    await repos.agents.create(agent_id="analyst-1", persona_name="analyst")
    assert await repos.wakeups.has_active_for_agent("analyst-1") is False


@pytest.mark.asyncio
async def test_has_active_for_agent_true_while_wakeup_running(
    repos: SqliteRepositories,
) -> None:
    await repos.personas.upsert(
        Persona(name="analyst", role_description="a", system_prompt="a")
    )
    await repos.agents.create(agent_id="analyst-1", persona_name="analyst")
    task_id = await repos.tasks.create(
        TaskSpec(agent_id="analyst-1", goal="g", acceptance="a"),
    )
    wakeup_id = await repos.wakeups.start(task_id, "analyst", agent_id="analyst-1")
    await repos.tasks.claim_lease(task_id, wakeup_id, duration_sec=600)

    assert await repos.wakeups.has_active_for_agent("analyst-1") is True


@pytest.mark.asyncio
async def test_has_active_for_agent_flips_false_after_wakeup_ends(
    repos: SqliteRepositories,
) -> None:
    """``ended_at`` (set by ``wakeups.end()``) is the signal. Once the
    wakeup row records an end time, the agent is free again."""
    await repos.personas.upsert(
        Persona(name="analyst", role_description="a", system_prompt="a")
    )
    await repos.agents.create(agent_id="analyst-1", persona_name="analyst")
    task_id = await repos.tasks.create(
        TaskSpec(agent_id="analyst-1", goal="g", acceptance="a"),
    )
    wakeup_id = await repos.wakeups.start(task_id, "analyst", agent_id="analyst-1")
    await repos.tasks.claim_lease(task_id, wakeup_id, duration_sec=600)
    assert await repos.wakeups.has_active_for_agent("analyst-1") is True

    await repos.wakeups.end(wakeup_id, end_status="completed")
    assert await repos.wakeups.has_active_for_agent("analyst-1") is False


@pytest.mark.asyncio
async def test_has_active_for_agent_isolates_by_agent_id(
    repos: SqliteRepositories,
) -> None:
    """Two different agents of the same persona are independent —
    ``analyst-1`` running does not block ``analyst-2``."""
    await repos.personas.upsert(
        Persona(name="analyst", role_description="a", system_prompt="a")
    )
    await repos.agents.create(agent_id="analyst-1", persona_name="analyst")
    await repos.agents.create(agent_id="analyst-2", persona_name="analyst")
    task_id = await repos.tasks.create(
        TaskSpec(agent_id="analyst-1", goal="g", acceptance="a"),
    )
    wakeup_id = await repos.wakeups.start(task_id, "analyst", agent_id="analyst-1")
    await repos.tasks.claim_lease(task_id, wakeup_id, duration_sec=600)

    assert await repos.wakeups.has_active_for_agent("analyst-1") is True
    assert await repos.wakeups.has_active_for_agent("analyst-2") is False
