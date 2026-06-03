"""query_task_status observability + the list_tasks↔query_task_status invariant.

Follow-ups to the 019e8d7d RCA: a coordinator must be able to answer "is this
task running?" with evidence, not by inferring from task.status. query_task_status
now surfaces run-state from an OPEN wakeup (ended_at IS NULL), plus agent_id, the
children it dispatched, and recent wakeups. And the by-id path and the enumeration
path must never disagree (the RCA report claimed they did).
"""

from __future__ import annotations

import pytest

from lyre.persistence.models import Persona, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.tools import ToolContext
from lyre.runtime.tools.introspect import LIST_TASKS
from lyre.runtime.tools.tasks import QUERY_TASK_STATUS


async def _seed_agent(repos: SqliteRepositories, name: str) -> None:
    await repos.personas.upsert(
        Persona(name=name, role_description=name, system_prompt=name)
    )
    await repos.agents.create(agent_id=name, persona_name=name)


def _ctx(repos: SqliteRepositories, *, agent_id: str = "dispatcher", task_id: str = "") -> ToolContext:
    return ToolContext(
        repos=repos, task_id=task_id, wakeup_id="w",
        persona_name=agent_id, agent_id=agent_id,
    )


@pytest.mark.asyncio
async def test_is_running_only_with_open_wakeup_not_status(
    repos: SqliteRepositories,
) -> None:
    """is_running tracks an OPEN wakeup, not task.status — the core fix."""
    await _seed_agent(repos, "dispatcher")
    task_id = await repos.tasks.create(
        TaskSpec(persona_name="dispatcher", agent_id="dispatcher", goal="g", acceptance="a")
    )

    # No wakeup yet → not running.
    out = await QUERY_TASK_STATUS.handler(_ctx(repos), {"task_id": task_id})
    assert out["is_running"] is False
    assert out["active_wakeup_id"] is None
    assert out["agent_id"] == "dispatcher"

    # Open wakeup → running, with the wakeup id surfaced.
    wakeup_id = await repos.wakeups.start(task_id, "dispatcher", agent_id="dispatcher")
    await repos.tasks.claim_lease(task_id, wakeup_id, duration_sec=600)
    out = await QUERY_TASK_STATUS.handler(_ctx(repos), {"task_id": task_id})
    assert out["is_running"] is True
    assert out["active_wakeup_id"] == wakeup_id

    # Wakeup ended + task completed → NOT running (this is the 019e8d7d case:
    # a completed task with no open wakeup is not "currently running").
    await repos.wakeups.end(wakeup_id, end_status="completed")
    await repos.tasks.update_status(task_id, "completed")
    out = await QUERY_TASK_STATUS.handler(_ctx(repos), {"task_id": task_id})
    assert out["status"] == "completed"
    assert out["is_running"] is False
    assert out["active_wakeup_id"] is None
    # The ended wakeup is still surfaced (transcript/audit).
    assert any(
        w["id"] == wakeup_id and w["end_status"] == "completed"
        for w in out["recent_wakeups"]
    )


@pytest.mark.asyncio
async def test_query_task_status_lists_children(repos: SqliteRepositories) -> None:
    """The children a task dispatched are surfaced — so "what did I spawn?" is
    answerable by id instead of reconstructed from mailbox."""
    await _seed_agent(repos, "dispatcher")
    await _seed_agent(repos, "analyst")
    parent = await repos.tasks.create(
        TaskSpec(persona_name="dispatcher", agent_id="dispatcher", goal="p", acceptance="a")
    )
    child = await repos.tasks.create(
        TaskSpec(
            persona_name="analyst", agent_id="analyst",
            goal="c", acceptance="a", parent_task_id=parent,
        )
    )
    out = await QUERY_TASK_STATUS.handler(_ctx(repos), {"task_id": parent})
    kids = {c["id"]: c for c in out["children"]}
    assert child in kids
    assert kids[child]["persona"] == "analyst"
    assert kids[child]["agent_id"] == "analyst"


@pytest.mark.asyncio
async def test_query_task_status_flags_own_wakeup(repos: SqliteRepositories) -> None:
    """Asked from within the very task → is_current_wakeup, same guard as list_tasks."""
    await _seed_agent(repos, "dispatcher")
    task_id = await repos.tasks.create(
        TaskSpec(persona_name="dispatcher", agent_id="dispatcher", goal="g", acceptance="a")
    )
    out = await QUERY_TASK_STATUS.handler(
        _ctx(repos, task_id=task_id), {"task_id": task_id}
    )
    assert out["is_current_wakeup"] is True


@pytest.mark.asyncio
async def test_list_tasks_enumerates_what_query_task_status_finds(
    repos: SqliteRepositories,
) -> None:
    """Invariant lock: a completed task query_task_status resolves by id MUST be
    enumerable by list_tasks(status=completed). Both read the same `tasks` table,
    so they cannot disagree — pins against a future scope/filter divergence (the
    RCA report claimed list_tasks was empty while the by-id path found it)."""
    await _seed_agent(repos, "dispatcher")
    task_id = await repos.tasks.create(
        TaskSpec(persona_name="dispatcher", agent_id="dispatcher", goal="g", acceptance="a")
    )
    await repos.tasks.update_status(task_id, "completed")

    by_id = await QUERY_TASK_STATUS.handler(_ctx(repos), {"task_id": task_id})
    assert by_id["status"] == "completed"

    listed = await LIST_TASKS.handler(_ctx(repos), {"status": "completed", "limit": 200})
    assert task_id in {t["id"] for t in listed["tasks"]}
