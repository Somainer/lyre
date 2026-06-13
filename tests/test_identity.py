"""Agent-id grammar tests.

The format (bare lowercase token for bootstrap-seeded singletons,
``<persona>/<name>`` for spawned) is enforced at every trust boundary
— create_agent, mailbox_send, dashboard, CLI. The shape of those
rules lives in one place (`lyre.runtime.identity`) so this test file
is the single point of truth for what we consider valid.
"""

from __future__ import annotations

import pytest

from lyre.runtime.identity import (
    agent_notes_rel_path,
    compose_id,
    flat_id,
    is_valid_agent_id,
    split_id,
)

# ---------------------------------------------------------------------------
# is_valid_agent_id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "agent_id",
    [
        "owner",
        "dispatcher",
        "analyst-1",
        "reviewer-1",
        "worker-maintainer",
        "worker-maintainer/refactor-auth",
        "reviewer/pr-142",
        "analyst/research-x",
        "a",
        "a/1",
        "worker/1",  # numeric short name (auto-naming fallback)
    ],
)
def test_valid_ids(agent_id: str) -> None:
    assert is_valid_agent_id(agent_id), agent_id


@pytest.mark.parametrize(
    "agent_id",
    [
        "",                       # empty
        "Worker",                 # uppercase persona
        "worker_maintainer",      # underscores
        "worker maintainer",      # space
        "/foo",                   # missing persona
        "foo/",                   # missing name
        "foo//bar",               # double slash
        "foo/bar/baz",            # too many segments
        "-leading",               # leading hyphen
        "worker/-leading",        # name leading hyphen
        "worker/UPPER",           # uppercase name
        "1numeric-persona",       # persona starts with digit
        "worker/has space",       # name with space
    ],
)
def test_invalid_ids(agent_id: str) -> None:
    assert not is_valid_agent_id(agent_id), agent_id


# ---------------------------------------------------------------------------
# split_id / compose_id round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "agent_id,persona,name",
    [
        ("owner", "owner", None),
        ("dispatcher", "dispatcher", None),
        ("worker", "worker", None),
        ("worker/scout", "worker", "scout"),
        ("reviewer/pr-142", "reviewer", "pr-142"),
    ],
)
def test_split_id(agent_id: str, persona: str, name: str | None) -> None:
    assert split_id(agent_id) == (persona, name)


def test_compose_id_roundtrip() -> None:
    assert compose_id("worker", "scout") == "worker/scout"
    assert compose_id("worker", None) == "worker"
    assert compose_id("worker", "") == "worker"


# ---------------------------------------------------------------------------
# Filesystem mapping: flat_id / agent_notes_rel_path are the SSOT every
# per-agent file path derives from. wakeup_summary used to build the notes
# path from the RAW id (no flatten), forking every spawned agent's memory
# into a stray directory the agent never reads — these pin the contract.
# ---------------------------------------------------------------------------


def test_flat_id_flattens_spawned_ids() -> None:
    assert flat_id("dispatcher") == "dispatcher"
    assert flat_id("worker-maintainer/backend-1") == "worker-maintainer-backend-1"


def test_agent_notes_rel_path_is_always_a_flat_file() -> None:
    assert agent_notes_rel_path("dispatcher") == "facts/agent-dispatcher-notes.md"
    rel = agent_notes_rel_path("worker-maintainer/backend-1")
    assert rel == "facts/agent-worker-maintainer-backend-1-notes.md"
    # One path segment under facts/ — a slash here would imply a directory
    # layer and break the identity preamble's read_memory instructions.
    assert "/" not in rel.removeprefix("facts/")
