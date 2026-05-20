"""Agent-id grammar tests.

The format (`<persona>` for bootstrap, `<persona>/<name>` for spawned)
is enforced at every trust boundary — create_agent, mailbox_send,
dashboard, CLI. The shape of those rules lives in one place
(`lyre.runtime.identity`) so this test file is the single point of
truth for what we consider valid.
"""

from __future__ import annotations

import pytest

from lyre.runtime.identity import (
    BOOTSTRAP_IDS,
    compose_id,
    is_bootstrap,
    is_valid_agent_id,
    split_id,
    validate_agent_id,
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
# validate_agent_id raises
# ---------------------------------------------------------------------------


def test_validate_agent_id_raises_on_bad_id() -> None:
    with pytest.raises(ValueError, match="invalid agent_id"):
        validate_agent_id("Bad Id")


def test_validate_agent_id_accepts_good_id() -> None:
    # No raise.
    validate_agent_id("worker-maintainer/refactor-auth")


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
# Bootstrap predicate
# ---------------------------------------------------------------------------


def test_bootstrap_ids_are_well_known() -> None:
    assert is_bootstrap("owner")
    assert is_bootstrap("dispatcher")
    # analyst & reviewer are NOT bootstrap personas — they can spawn for
    # parallelism. Only the owner-facing singleton is locked.
    assert not is_bootstrap("analyst")
    assert not is_bootstrap("reviewer")
    assert not is_bootstrap("worker")
    assert not is_bootstrap("worker/scout")


def test_bootstrap_set_is_frozen() -> None:
    """Frozenset → can't be mutated at runtime."""
    assert isinstance(BOOTSTRAP_IDS, frozenset)
