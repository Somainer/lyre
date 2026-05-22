"""Tests for the persona-display_name-driven bootstrap seeding machinery.

Covers two pieces tightly coupled to the same identity.md ``display_name``
field (the SSOT for an agent's bare id):

  - wizard input normalization (``_normalize_agent_id_input``) so natural
    names like "Subaru" pass through as legal lowercase ids instead of
    silently creating a non-validatable persona row.
  - stale-bootstrap reconciliation (``archive_stale_bootstrap_agents``)
    so re-running onboard with different display_names doesn't leave
    both the old and new bare-id agents visible.
"""

from __future__ import annotations

import pytest

from lyre.onboard import _normalize_agent_id_input
from lyre.persistence.db import init_db
from lyre.persistence.models import Persona
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.personas.seed import (
    archive_stale_bootstrap_agents,
    seed_default_agents,
)

# ---------------------------------------------------------------------------
# Wizard input normalization
# ---------------------------------------------------------------------------


def test_normalize_accepts_lowercase_unchanged() -> None:
    value, hint = _normalize_agent_id_input("luna", default="dispatcher")
    assert value == "luna"
    assert hint is None


def test_normalize_blank_falls_back_to_default() -> None:
    value, hint = _normalize_agent_id_input("", default="dispatcher")
    assert value == "dispatcher"
    assert hint is None


def test_normalize_lowercases_titlecase_with_hint() -> None:
    """Owner typed 'Subaru' — accept it but explicitly tell them we
    stored it as 'subaru' so the runtime grammar is satisfied."""
    value, hint = _normalize_agent_id_input("Subaru", default="dispatcher")
    assert value == "subaru"
    assert hint is not None and "subaru" in hint


def test_normalize_rejects_spaces() -> None:
    value, hint = _normalize_agent_id_input("Some Name", default="dispatcher")
    assert value == ""
    assert hint is not None and "valid agent id" in hint


def test_normalize_rejects_punctuation() -> None:
    value, hint = _normalize_agent_id_input("luna!", default="dispatcher")
    assert value == ""
    assert hint is not None


def test_normalize_rejects_underscore_per_grammar() -> None:
    """Grammar is ``[a-z][a-z0-9-]*`` — underscore deliberately excluded
    so agent ids stay one-to-one with the filesystem-safe notes filename
    (avoids ``agent-foo_bar-notes.md`` vs ``agent-foo-bar-notes.md``
    collisions)."""
    value, _ = _normalize_agent_id_input("foo_bar", default="dispatcher")
    assert value == ""


# ---------------------------------------------------------------------------
# Persona-driven bootstrap seeding
# ---------------------------------------------------------------------------


def _persona(name: str, kind: str, display_name: str | None = None) -> Persona:
    return Persona(
        name=name, display_name=display_name, kind=kind,  # type: ignore[arg-type]
        role_description=name, system_prompt=name,
    )


@pytest.fixture
async def repos():
    conn = await init_db(":memory:")
    try:
        r = SqliteRepositories(conn)
        for p in (
            _persona("owner", "singleton", "owner"),
            _persona("dispatcher", "singleton", "dispatcher"),
            _persona("analyst", "seeded", "analyst-1"),
            _persona("reviewer", "seeded", "reviewer-1"),
            _persona("worker-maintainer", "spawn_only"),
        ):
            await r.personas.upsert(p)
        yield r
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_seed_creates_only_singleton_and_seeded(
    repos: SqliteRepositories,
) -> None:
    """``seed_default_agents`` walks personas in DB and creates one agent
    per ``singleton`` / ``seeded`` persona using the persona's
    ``display_name``. ``spawn_only`` personas are skipped."""
    await seed_default_agents(repos.personas, repos.agents)

    live = {a.id: a for a in await repos.agents.list_all(include_archived=False)}
    assert "owner" in live
    assert "dispatcher" in live
    assert "analyst-1" in live
    assert "reviewer-1" in live
    # spawn_only persona ⇒ no bootstrap agent
    assert not any(a.persona_name == "worker-maintainer" for a in live.values())


@pytest.mark.asyncio
async def test_seed_is_idempotent(repos: SqliteRepositories) -> None:
    created1 = await seed_default_agents(repos.personas, repos.agents)
    created2 = await seed_default_agents(repos.personas, repos.agents)
    assert sorted(created1) == ["analyst-1", "dispatcher", "owner", "reviewer-1"]
    assert created2 == []


@pytest.mark.asyncio
async def test_archive_stale_renames_old_default_when_owner_customizes(
    repos: SqliteRepositories,
) -> None:
    """First run: defaults seeded. Second run: owner edited identity.md to
    set ``display_name: luna`` on the dispatcher persona. The old
    ``dispatcher`` agent row should be archived."""
    await seed_default_agents(repos.personas, repos.agents)

    # Owner renames dispatcher's display_name.
    dispatcher = await repos.personas.get("dispatcher")
    assert dispatcher is not None
    await repos.personas.upsert(dispatcher.model_copy(update={"display_name": "luna"}))

    await seed_default_agents(repos.personas, repos.agents)

    live = {a.id for a in await repos.agents.list_all(include_archived=False)}
    archived = {
        a.id for a in await repos.agents.list_all(include_archived=True)
        if a.status == "archived"
    }
    assert "luna" in live
    assert "dispatcher" not in live
    assert "dispatcher" in archived
    # Unchanged slots are untouched.
    assert "analyst-1" in live
    assert "reviewer-1" in live


@pytest.mark.asyncio
async def test_archive_stale_leaves_user_spawned_agents_alone(
    repos: SqliteRepositories,
) -> None:
    """A child the owner spawned via ``create_agent`` (parent_agent_id set)
    must NOT be archived even if its persona's seeded singleton is being
    renamed."""
    await seed_default_agents(repos.personas, repos.agents)
    await repos.agents.create(
        agent_id="analyst/research-x",
        persona_name="analyst",
        parent_agent_id="owner",
    )

    analyst = await repos.personas.get("analyst")
    assert analyst is not None
    await repos.personas.upsert(analyst.model_copy(update={"display_name": "scribe"}))

    await archive_stale_bootstrap_agents(repos.personas, repos.agents)

    live = {a.id for a in await repos.agents.list_all(include_archived=False)}
    assert "analyst-1" not in live  # bootstrap singleton retired
    assert "scribe" not in live     # not yet seeded — that's seed's job
    assert "analyst/research-x" in live  # child preserved


@pytest.mark.asyncio
async def test_archive_stale_skips_personas_whose_kind_is_spawn_only(
    repos: SqliteRepositories,
) -> None:
    """If a persona is reclassified to ``spawn_only`` later, its existing
    bootstrap row is NOT auto-archived — that decision is left to the
    owner via ``lyre agent archive``."""
    await seed_default_agents(repos.personas, repos.agents)

    analyst = await repos.personas.get("analyst")
    assert analyst is not None
    await repos.personas.upsert(analyst.model_copy(update={"kind": "spawn_only"}))

    archived = await archive_stale_bootstrap_agents(repos.personas, repos.agents)
    assert "analyst-1" not in archived
    live = {a.id for a in await repos.agents.list_all(include_archived=False)}
    assert "analyst-1" in live
