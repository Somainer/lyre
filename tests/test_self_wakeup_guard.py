"""list_tasks / list_agents must mark the caller's OWN current wakeup.

The 019e8d7d incident: a coordinator answered "what task is running?" by
reporting the task of the very wakeup it was answering inside — a top-level
dispatcher task that is in_progress only while the agent is awake to observe
it, and completed/gone the moment the wakeup ends. The fix surfaces that
self-reference so an agent can't mistake "the wakeup I'm running in" for
"delegated work in flight":

  - list_tasks flags is_current_wakeup on the task whose id == ctx.task_id.
  - list_agents flags is_you on the calling agent's own entry.
"""

from __future__ import annotations

import pytest

from lyre.persistence.models import Persona, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.tools import ToolContext
from lyre.runtime.tools.introspect import LIST_AGENTS, LIST_TASKS


async def _seed_agent(repos: SqliteRepositories, name: str) -> None:
    await repos.personas.upsert(
        Persona(name=name, role_description=name, system_prompt=name)
    )
    await repos.agents.create(agent_id=name, persona_name=name)


@pytest.mark.asyncio
async def test_list_tasks_flags_callers_own_wakeup_task(
    repos: SqliteRepositories,
) -> None:
    """The task carrying THIS wakeup is is_current_wakeup=true; a sibling
    task of the same agent is false. A self-clarifying note is attached."""
    await _seed_agent(repos, "dispatcher")
    own = await repos.tasks.create(
        TaskSpec(persona_name="dispatcher", agent_id="dispatcher", goal="my wakeup", acceptance="a")
    )
    other = await repos.tasks.create(
        TaskSpec(persona_name="dispatcher", agent_id="dispatcher", goal="sibling", acceptance="a")
    )

    # ctx.task_id is the task this wakeup is running — that's the whole point.
    ctx = ToolContext(
        repos=repos, task_id=own, wakeup_id="w", persona_name="dispatcher", agent_id="dispatcher"
    )
    out = await LIST_TASKS.handler(ctx, {})
    by_id = {t["id"]: t for t in out["tasks"]}

    assert by_id[own]["is_current_wakeup"] is True
    assert by_id[other]["is_current_wakeup"] is False
    # Present only because the self task is in the result — steers the model
    # away from reporting it as "a running task".
    assert "note" in out and "is_current_wakeup" in out["note"]


@pytest.mark.asyncio
async def test_list_tasks_no_self_note_when_own_task_absent(
    repos: SqliteRepositories,
) -> None:
    """If the caller's task isn't in the result set, nothing is flagged and
    no self-note is attached (don't nag when irrelevant)."""
    await _seed_agent(repos, "dispatcher")
    await repos.tasks.create(
        TaskSpec(persona_name="dispatcher", agent_id="dispatcher", goal="g", acceptance="a")
    )
    # task_id points at a wakeup task that isn't among the rows (e.g. empty stub).
    ctx = ToolContext(
        repos=repos, task_id="", wakeup_id="w", persona_name="dispatcher", agent_id="dispatcher"
    )
    out = await LIST_TASKS.handler(ctx, {})
    assert all(t["is_current_wakeup"] is False for t in out["tasks"])
    assert "note" not in out


@pytest.mark.asyncio
async def test_list_agents_flags_the_caller_as_is_you(
    repos: SqliteRepositories,
) -> None:
    """The calling agent's own row carries is_you=true; peers are false."""
    await _seed_agent(repos, "dispatcher")
    await _seed_agent(repos, "analyst")

    ctx = ToolContext(
        repos=repos, task_id="", wakeup_id="w", persona_name="dispatcher", agent_id="dispatcher"
    )
    out = await LIST_AGENTS.handler(ctx, {})
    by_id = {a["id"]: a for a in out["agents"]}

    assert by_id["dispatcher"]["is_you"] is True
    assert by_id["analyst"]["is_you"] is False


@pytest.mark.asyncio
async def test_list_agents_note_clarifies_own_busy_wakeup(
    repos: SqliteRepositories,
) -> None:
    """When the caller is itself busy (its active_task_id is its own running
    wakeup), the note spells out that this is not delegated work — the exact
    self-reference that produced the 019e8d7d misreport."""
    await _seed_agent(repos, "dispatcher")
    task_id = await repos.tasks.create(
        TaskSpec(persona_name="dispatcher", agent_id="dispatcher", goal="g", acceptance="a")
    )
    wakeup_id = await repos.wakeups.start(task_id, "dispatcher", agent_id="dispatcher")
    await repos.tasks.claim_lease(task_id, wakeup_id, duration_sec=600)

    ctx = ToolContext(
        repos=repos, task_id=task_id, wakeup_id=wakeup_id,
        persona_name="dispatcher", agent_id="dispatcher",
    )
    out = await LIST_AGENTS.handler(ctx, {})
    me = next(a for a in out["agents"] if a["is_you"])
    assert me["occupancy"] == "busy"
    assert me["active_task_id"] == task_id
    assert "is_you=true" in out["note"]
