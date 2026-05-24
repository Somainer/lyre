"""``list_agents`` occupancy must be derived from active wakeups.

The runtime never writes ``agent.status = 'busy'`` — that column carries
only the lifecycle distinction (``idle`` vs ``archived``). Real
running-vs-not-running state lives in the wakeups table: a row with
``ended_at IS NULL`` is a genuine active wakeup. The
``_derive_occupancy_status`` in ``dashboard/routes/agents.py`` already
worked this way; ``runtime/tools/introspect._list_agents`` used to
check ``agent.status == 'busy'`` — a dead branch — and so reported
``queued`` for agents that were currently running a wakeup.

These tests pin the post-fix behavior: ``busy`` flows from
``wakeups.list_active()``, ``queued`` / ``available`` retain their
prior semantics (in-flight task vs none), ``archived`` still wins.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lyre.persistence.models import Persona, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.tools import ToolContext
from lyre.runtime.tools.introspect import LIST_AGENTS


async def _ctx(
    repos: SqliteRepositories, *, persona: str = "worker", agent_id: str = "worker",
) -> ToolContext:
    """Minimal ToolContext for LIST_AGENTS — the tool only reads, never
    writes via ctx, so task_id / wakeup_id can be empty stubs."""
    return ToolContext(
        repos=repos, task_id="", wakeup_id="",
        persona_name=persona, agent_id=agent_id,
    )


async def _occupancy_of(
    ctx: ToolContext, agent_id: str, *, include_archived: bool = False,
) -> str:
    out = await LIST_AGENTS.handler(
        ctx, {"include_archived": include_archived},
    )
    agents = {a["id"]: a for a in out["agents"]}
    return agents[agent_id]["occupancy"]


@pytest.mark.asyncio
async def test_busy_derives_from_active_wakeup_not_agent_status(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    """The exact production miss: agent has an open wakeup on a real
    in-progress task; ``list_agents`` MUST report ``busy``, not the
    pre-fix ``queued``. Reproduces what owner saw on momoka."""
    await repos.personas.upsert(
        Persona(name="momoka", role_description="m", system_prompt="m")
    )
    await repos.agents.create(agent_id="momoka", persona_name="momoka")
    task_id = await repos.tasks.create(
        TaskSpec(persona_name="momoka", agent_id="momoka", goal="g", acceptance="a")
    )
    wakeup_id = await repos.wakeups.start(task_id, "momoka", agent_id="momoka")
    await repos.tasks.claim_lease(task_id, wakeup_id, duration_sec=600)
    # claim_lease just moved task→in_progress. agent.status is still
    # the default 'idle' — that's the point.

    ctx = await _ctx(repos, persona="momoka", agent_id="momoka")
    assert await _occupancy_of(ctx, "momoka") == "busy"


@pytest.mark.asyncio
async def test_busy_outranks_queued_when_agent_has_both(
    repos: SqliteRepositories,
) -> None:
    """An agent inside a wakeup may ALSO have additional pending tasks
    waiting behind the one it's running. ``busy`` must win — leader
    needs to see "this agent is occupied right now", not "this agent
    has a queue" (which would suggest "still safe to add more")."""
    await repos.personas.upsert(
        Persona(name="w", role_description="w", system_prompt="w")
    )
    await repos.agents.create(agent_id="w", persona_name="w")

    running_task = await repos.tasks.create(
        TaskSpec(persona_name="w", agent_id="w", goal="now", acceptance="a")
    )
    wakeup_id = await repos.wakeups.start(running_task, "w", agent_id="w")
    await repos.tasks.claim_lease(running_task, wakeup_id, duration_sec=600)

    # Plus a queued pending task — would have driven occupancy=queued
    # in the old code if the running wakeup weren't recognised.
    await repos.tasks.create(
        TaskSpec(persona_name="w", agent_id="w", goal="next", acceptance="a")
    )

    ctx = await _ctx(repos, persona="w", agent_id="w")
    assert await _occupancy_of(ctx, "w") == "busy"


@pytest.mark.asyncio
async def test_queued_when_in_flight_task_but_no_open_wakeup(
    repos: SqliteRepositories,
) -> None:
    """Pre-existing semantics preserved: agent has pending work but no
    wakeup is currently running for it. Leader should NOT dispatch
    more — ``queued`` is the right signal."""
    await repos.personas.upsert(
        Persona(name="w", role_description="w", system_prompt="w")
    )
    await repos.agents.create(agent_id="w", persona_name="w")
    await repos.tasks.create(
        TaskSpec(persona_name="w", agent_id="w", goal="waiting", acceptance="a")
    )
    # No wakeups.start — task sits at pending, no open row.

    ctx = await _ctx(repos, persona="w", agent_id="w")
    assert await _occupancy_of(ctx, "w") == "queued"


@pytest.mark.asyncio
async def test_available_when_no_work_and_no_wakeup(
    repos: SqliteRepositories,
) -> None:
    """Pre-existing semantics preserved: no work, no wakeup → free to
    take new work. Drives the reuse-vs-spawn decision."""
    await repos.personas.upsert(
        Persona(name="w", role_description="w", system_prompt="w")
    )
    await repos.agents.create(agent_id="w", persona_name="w")

    ctx = await _ctx(repos, persona="w", agent_id="w")
    assert await _occupancy_of(ctx, "w") == "available"


@pytest.mark.asyncio
async def test_archived_outranks_busy(
    repos: SqliteRepositories,
) -> None:
    """An archived agent with leftover open wakeup row (theoretically
    possible if archive happened mid-wakeup) is still ``archived`` —
    archive is the terminal lifecycle state, no occupancy underneath
    it matters for dispatch."""
    await repos.personas.upsert(
        Persona(name="w", role_description="w", system_prompt="w")
    )
    await repos.agents.create(agent_id="w", persona_name="w")
    task_id = await repos.tasks.create(
        TaskSpec(persona_name="w", agent_id="w", goal="g", acceptance="a")
    )
    await repos.wakeups.start(task_id, "w", agent_id="w")
    await repos.agents.archive("w")

    ctx = await _ctx(repos, persona="w", agent_id="w")
    assert await _occupancy_of(ctx, "w", include_archived=True) == "archived"


@pytest.mark.asyncio
async def test_busy_attributed_per_agent_id_not_persona(
    repos: SqliteRepositories,
) -> None:
    """Multiple agents of the same persona must not share a busy
    flag. ``analyst/topic-A`` running a wakeup does NOT make
    ``analyst/topic-B`` ``busy`` — they're separate instances."""
    await repos.personas.upsert(
        Persona(name="analyst", role_description="a", system_prompt="a")
    )
    await repos.agents.create(agent_id="analyst/topic-A", persona_name="analyst")
    await repos.agents.create(agent_id="analyst/topic-B", persona_name="analyst")
    task_a = await repos.tasks.create(
        TaskSpec(
            persona_name="analyst", agent_id="analyst/topic-A",
            goal="A", acceptance="a",
        )
    )
    wakeup_a = await repos.wakeups.start(
        task_a, "analyst", agent_id="analyst/topic-A",
    )
    await repos.tasks.claim_lease(task_a, wakeup_a, duration_sec=600)

    ctx = await _ctx(repos, persona="analyst", agent_id="analyst/topic-A")
    out = await LIST_AGENTS.handler(ctx, {})
    by_id = {a["id"]: a["occupancy"] for a in out["agents"]}
    assert by_id["analyst/topic-A"] == "busy"
    assert by_id["analyst/topic-B"] == "available"
