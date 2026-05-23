"""Tests for the persona-display_name-driven bootstrap seeding machinery.

The system used to ALSO run ``archive_stale_bootstrap_agents`` inside
``seed_default_agents`` — silently archiving any parentless agent whose
id no longer matched its persona's current ``display_name``. That sounded
like a clean rename mechanism but created a catastrophic bug: a
fat-fingered ``identity.md`` edit + restart would archive the live
agent; correcting the edit + restart would archive the freshly-seeded
one too. Two typos = every dispatcher / analyst / reviewer wiped.

The mechanism is removed. ``seed_default_agents`` now only ensures the
persona's current ``display_name`` agent EXISTS and is LIVE:

  * agent_id doesn't exist     → create
  * agent_id exists, idle       → no-op
  * agent_id exists, archived   → unarchive (revive)

Tests below pin all three branches plus the boundaries.
"""

from __future__ import annotations

import pytest

from lyre.onboard import _normalize_agent_id_input
from lyre.persistence.db import init_db
from lyre.persistence.models import Persona
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.personas.seed import seed_default_agents

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
    seeded1 = await seed_default_agents(repos.personas, repos.agents)
    seeded2 = await seed_default_agents(repos.personas, repos.agents)
    assert sorted(seeded1) == ["analyst-1", "dispatcher", "owner", "reviewer-1"]
    assert seeded2 == []


@pytest.mark.asyncio
async def test_seed_does_not_archive_old_agent_on_rename(
    repos: SqliteRepositories,
) -> None:
    """The old auto-archive mechanism is gone. Owner edits identity.md
    to change ``display_name`` from ``dispatcher`` to ``luna``; seeding
    now creates ``luna`` BUT LEAVES ``dispatcher`` alive. The owner
    decides whether to clean up the stale one via
    ``lyre agent archive``. This is what fixes the typo-cascade
    failure mode where back-and-forth rename wiped every agent."""
    await seed_default_agents(repos.personas, repos.agents)

    # Owner renames dispatcher's display_name.
    dispatcher = await repos.personas.get("dispatcher")
    assert dispatcher is not None
    await repos.personas.upsert(
        dispatcher.model_copy(update={"display_name": "luna"})
    )

    await seed_default_agents(repos.personas, repos.agents)

    live = {a.id for a in await repos.agents.list_all(include_archived=False)}
    archived = {
        a.id for a in await repos.agents.list_all(include_archived=True)
        if a.status == "archived"
    }
    # Both alive; the rename added a new agent without killing the old.
    assert "luna" in live
    assert "dispatcher" in live
    # Nothing got auto-archived.
    assert "dispatcher" not in archived


@pytest.mark.asyncio
async def test_seed_unarchives_when_display_name_returns_to_archived_id(
    repos: SqliteRepositories,
) -> None:
    """The self-healing path: owner accidentally archived their
    dispatcher agent (or a previous overzealous auto-archive did it).
    Setting ``display_name`` back to that id and restarting must
    REVIVE the archived row, not create a duplicate. Mail / task
    history attached to the id is preserved by the unarchive."""
    await seed_default_agents(repos.personas, repos.agents)
    # Simulate the foot-gun: archive the dispatcher.
    await repos.agents.archive("dispatcher")

    # Owner restarts lyre serve — seed_default_agents runs again.
    seeded = await seed_default_agents(repos.personas, repos.agents)

    # The agent didn't get re-created (would duplicate notes / orphan
    # mail FK semantics); it got unarchived.
    assert "dispatcher" in seeded
    dispatcher = await repos.agents.get("dispatcher")
    assert dispatcher is not None
    assert dispatcher.status == "idle"
    assert dispatcher.archived_at is None


@pytest.mark.asyncio
async def test_seed_skips_spawn_only_personas(
    repos: SqliteRepositories,
) -> None:
    """``spawn_only`` personas (workers) don't get a default singleton.
    They're spawned on demand by the dispatcher via ``create_agent``."""
    await seed_default_agents(repos.personas, repos.agents)
    workers = [
        a for a in await repos.agents.list_all()
        if a.persona_name == "worker-maintainer"
    ]
    assert workers == []
