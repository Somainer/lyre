"""Tests for the subagent / parent-task relationship.

The previous ``await_subagents`` machinery (parent task → ``needs_input``,
scheduler ``find_parents_ready_to_wake`` flips it back to ``pending``) has
been removed. Parent-child task lineage stays — ``parent_task_id`` is
still set by ``dispatch_task``, ``find_children`` still works — but
synchronisation is event-driven: children mail their parent when done,
auto-wake-on-mail picks it up.

Tests below cover what remains: the DAO link and the initial user
message rendering. The scheduler end-to-end (dispatcher → worker →
wake) lives in test_scheduler.py / test_subprocess_runner.py.
"""

from __future__ import annotations

import pytest

from lyre.persistence.models import Persona, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.context import assemble_initial_user_message


@pytest.mark.asyncio
async def test_find_children_returns_only_direct_children(
    repos: SqliteRepositories,
) -> None:
    """Parent-child lineage is still tracked. ``find_children`` is what
    composer agents (analyst, reviewer) use to poll sub-research
    status via ``query_task_status`` after dispatching sub-tasks."""
    await repos.personas.upsert(
        Persona(name="p", role_description="p", system_prompt="p")
    )
    parent = await repos.tasks.create(
        TaskSpec(persona_name="p", goal="parent", acceptance="a")
    )
    child1 = await repos.tasks.create(
        TaskSpec(persona_name="p", goal="c1", acceptance="a", parent_task_id=parent)
    )
    child2 = await repos.tasks.create(
        TaskSpec(persona_name="p", goal="c2", acceptance="a", parent_task_id=parent)
    )
    unrelated = await repos.tasks.create(
        TaskSpec(persona_name="p", goal="u", acceptance="a")
    )

    kids = await repos.tasks.find_children(parent)
    assert {c.id for c in kids} == {child1, child2}
    assert unrelated not in {c.id for c in kids}
    assert await repos.tasks.find_children(unrelated) == []


@pytest.mark.asyncio
async def test_user_message_includes_children_status(
    repos: SqliteRepositories,
) -> None:
    """When a parent task is run, its initial user message lists each
    child's status so the model can decide whether to wait, retry,
    or compose."""
    await repos.personas.upsert(
        Persona(name="dispatcher", role_description="l", system_prompt="l")
    )
    await repos.personas.upsert(
        Persona(name="worker", role_description="w", system_prompt="w")
    )
    parent_id = await repos.tasks.create(
        TaskSpec(persona_name="dispatcher", goal="parent goal", acceptance="parent ok")
    )
    child_id = await repos.tasks.create(
        TaskSpec(
            persona_name="worker", goal="c", acceptance="a",
            parent_task_id=parent_id,
        )
    )
    await repos.tasks.update_status(child_id, "completed")

    parent = await repos.tasks.get(parent_id)
    assert parent is not None
    msg = await assemble_initial_user_message(parent, tasks_repo=repos.tasks)
    text = msg.content[0].text or ""
    assert "parent goal" in text
    assert child_id in text
    assert "status=completed" in text


@pytest.mark.asyncio
async def test_user_message_without_repo_omits_children_section(
    repos: SqliteRepositories,
) -> None:
    """If the caller didn't pass a tasks_repo, the children section is
    silently omitted (test fixture / standalone scenarios)."""
    await repos.personas.upsert(
        Persona(name="dispatcher", role_description="l", system_prompt="l")
    )
    parent_id = await repos.tasks.create(
        TaskSpec(persona_name="dispatcher", goal="g", acceptance="a")
    )
    parent = await repos.tasks.get(parent_id)
    assert parent is not None
    msg = await assemble_initial_user_message(parent)
    text = msg.content[0].text or ""
    assert "subagent" not in text
