"""Tests for the customizable-bootstrap-agent-id machinery.

Covers two pieces tightly coupled to the same config.toml [bootstrap]
section:

  - wizard input normalization (`_prompt_agent_id` / `_normalize_agent_id_input`)
    so natural names like "Subaru" pass through as legal lowercase ids
    instead of silently creating a non-validatable DB row.
  - stale-bootstrap reconciliation (`archive_stale_bootstrap_agents`) so
    re-running onboard with different names doesn't leave both old and
    new ids visible in the dashboard.
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
# Wizard input normalization (Bug D)
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
    """Grammar is [a-z][a-z0-9-]* — underscore deliberately excluded so
    agent ids stay one-to-one with the filesystem-safe notes filename
    (avoids `agent-foo_bar-notes.md` vs `agent-foo-bar-notes.md`
    collisions)."""
    value, _ = _normalize_agent_id_input("foo_bar", default="dispatcher")
    assert value == ""


# ---------------------------------------------------------------------------
# Stale-bootstrap reconciliation (Bug C)
# ---------------------------------------------------------------------------


@pytest.fixture
async def repos():
    conn = await init_db(":memory:")
    try:
        r = SqliteRepositories(conn)
        for name in ("owner", "dispatcher", "analyst", "reviewer",
                     "worker-maintainer"):
            await r.personas.upsert(
                Persona(name=name, role_description=name, system_prompt=name)
            )
        yield r
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_archive_stale_renames_old_default_when_owner_customizes(
    repos: SqliteRepositories,
) -> None:
    """First run: defaults seeded. Second run: owner customized dispatcher
    name to 'luna'. The old `dispatcher` row should be archived."""
    # First onboard: defaults.
    defaults = (
        ("owner", "owner"),
        ("dispatcher", "dispatcher"),
        ("analyst-1", "analyst"),
        ("reviewer-1", "reviewer"),
    )
    await seed_default_agents(repos.agents, agents=defaults)

    # Second onboard: dispatcher renamed.
    new_set = (
        ("owner", "owner"),
        ("luna", "dispatcher"),
        ("analyst-1", "analyst"),
        ("reviewer-1", "reviewer"),
    )
    await seed_default_agents(repos.agents, agents=new_set)

    live = {a.id: a for a in await repos.agents.list_all(include_archived=False)}
    archived = {
        a.id for a in await repos.agents.list_all(include_archived=True)
        if a.status == "archived"
    }
    assert "luna" in live
    assert "dispatcher" not in live
    assert "dispatcher" in archived
    # Unchanged slots are NOT archived.
    assert "analyst-1" in live
    assert "reviewer-1" in live


@pytest.mark.asyncio
async def test_archive_stale_leaves_user_spawned_agents_alone(
    repos: SqliteRepositories,
) -> None:
    """A worker the owner spawned via create_agent (parent_agent_id set)
    must NOT be archived even if its persona is among the bootstrap set."""
    # Bootstrap an analyst, then spawn a child analyst.
    await seed_default_agents(repos.agents, agents=(
        ("owner", "owner"),
        ("analyst-1", "analyst"),
    ))
    await repos.agents.create(
        agent_id="analyst/research-x",
        persona_name="analyst",
        parent_agent_id="owner",
    )

    # Rename the bootstrap analyst.
    await archive_stale_bootstrap_agents(repos.agents, (
        ("owner", "owner"),
        ("scribe", "analyst"),
    ))

    live = {a.id for a in await repos.agents.list_all(include_archived=False)}
    # Old singleton archived, child preserved.
    assert "analyst-1" not in live
    assert "analyst/research-x" in live


@pytest.mark.asyncio
async def test_archive_stale_skips_personas_not_in_canonical(
    repos: SqliteRepositories,
) -> None:
    """Bootstrap personas not mentioned in `canonical` (e.g. someone runs
    seed with a partial agents list) shouldn't have their existing
    singleton retroactively archived."""
    await seed_default_agents(repos.agents, agents=(
        ("owner", "owner"),
        ("dispatcher", "dispatcher"),
        ("analyst-1", "analyst"),
    ))

    # Re-run with NO analyst entry. The existing analyst-1 must stay.
    await archive_stale_bootstrap_agents(repos.agents, (
        ("owner", "owner"),
        ("dispatcher", "dispatcher"),
    ))

    live = {a.id for a in await repos.agents.list_all(include_archived=False)}
    assert "analyst-1" in live
