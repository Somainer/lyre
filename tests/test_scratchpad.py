"""Per-agent scratchpad: short-term working memory across wakeups.

Each LLM agent owns a single markdown file at
``memory/scratchpad/<flat-agent-id>.md`` that persists across wakeups.
``update_scratchpad(content, mode)`` is the only write surface; the
sandbox is enforced by deriving the path from ``ctx.agent_id``, not
from caller-supplied arguments.

Tests cover:
  - file creation on first append
  - append vs overwrite semantics
  - cross-agent isolation (each agent only writes its own file)
  - size cap
  - companion ``ensure_agent_scratchpad_file`` / ``scratchpad_rel_path``
    helpers used by ``seed_default_agents`` + the ``create_agent`` tool
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lyre.persistence.models import Persona, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.personas.seed import (
    ensure_agent_scratchpad_file,
    scratchpad_rel_path,
)
from lyre.runtime.tools import ToolContext, ToolError
from lyre.runtime.tools.introspect import UPDATE_SCRATCHPAD

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def test_scratchpad_rel_path_flattens_slashes() -> None:
    """``persona/name`` ids must collapse to a flat filename — otherwise
    the slash would imply a directory level and break ``read_memory``
    sandboxing assumptions."""
    assert scratchpad_rel_path("dispatcher") == "scratchpad/dispatcher.md"
    assert (
        scratchpad_rel_path("worker-maintainer/backend-1")
        == "scratchpad/worker-maintainer-backend-1.md"
    )


def test_ensure_agent_scratchpad_creates_empty_file(tmp_path: Path) -> None:
    """First call creates the file empty (no template). Owner-of-file is
    the model — preset content would frame the workspace and tempt the
    model to feel constrained by the template."""
    path = ensure_agent_scratchpad_file(tmp_path, "dispatcher")
    assert path == tmp_path / "scratchpad" / "dispatcher.md"
    assert path.exists()
    assert path.read_text(encoding="utf-8") == ""


def test_ensure_agent_scratchpad_is_idempotent(tmp_path: Path) -> None:
    """Re-running on an existing scratchpad leaves content untouched."""
    path = ensure_agent_scratchpad_file(tmp_path, "dispatcher")
    path.write_text("- existing TODO\n", encoding="utf-8")
    again = ensure_agent_scratchpad_file(tmp_path, "dispatcher")
    assert again == path
    assert again.read_text(encoding="utf-8") == "- existing TODO\n"


# ---------------------------------------------------------------------------
# update_scratchpad tool
# ---------------------------------------------------------------------------


@pytest.fixture
async def ctx(
    repos: SqliteRepositories, tmp_path: Path,
) -> ToolContext:
    """A minimal ToolContext with memory_root configured. Note the
    fixture seeds personas for ``dispatcher`` AND ``analyst`` so cross-
    agent isolation tests below can flip ``ctx.agent_id`` and exercise
    the second agent's scratchpad without recreating the whole fixture.
    """
    for persona, agent_id in (
        ("dispatcher", "dispatcher"),
        ("analyst", "analyst-1"),
    ):
        await repos.personas.upsert(
            Persona(name=persona, role_description=persona, system_prompt=persona)
        )
        await repos.agents.create(agent_id=agent_id, persona_name=persona)
    task_id = await repos.tasks.create(
        TaskSpec(agent_id="dispatcher", goal="g", acceptance="a"),
    )
    wakeup_id = await repos.wakeups.start(task_id, "dispatcher", agent_id="dispatcher")
    await repos.tasks.claim_lease(task_id, wakeup_id, duration_sec=600)
    memory_root = tmp_path / "memory"
    memory_root.mkdir()
    return ToolContext(
        repos=repos,
        task_id=task_id,
        wakeup_id=wakeup_id,
        persona_name="dispatcher",
        agent_id="dispatcher",
        extras={"memory_root": str(memory_root)},
    )


@pytest.mark.asyncio
async def test_append_creates_file_and_adds_content(
    ctx: ToolContext, tmp_path: Path,
) -> None:
    out = await UPDATE_SCRATCHPAD.handler(
        ctx, {"content": "- promised owner: dispatch X by EOD"},
    )
    assert out["mode"] == "append"
    assert out["rel_path"] == "scratchpad/dispatcher.md"
    path = tmp_path / "memory" / "scratchpad" / "dispatcher.md"
    assert path.read_text(encoding="utf-8") == (
        "- promised owner: dispatch X by EOD"
    )


@pytest.mark.asyncio
async def test_append_inserts_separator_between_chunks(
    ctx: ToolContext, tmp_path: Path,
) -> None:
    """Two appends must not smash together: there should be a newline
    between them so each chunk reads as its own bullet / paragraph."""
    await UPDATE_SCRATCHPAD.handler(ctx, {"content": "- first"})
    await UPDATE_SCRATCHPAD.handler(ctx, {"content": "- second"})
    path = tmp_path / "memory" / "scratchpad" / "dispatcher.md"
    body = path.read_text(encoding="utf-8")
    assert body == "- first\n- second"


@pytest.mark.asyncio
async def test_overwrite_replaces_whole_file(
    ctx: ToolContext, tmp_path: Path,
) -> None:
    """The curation path: model reads, decides what stays, writes
    back the pruned version. Done items disappear from context."""
    await UPDATE_SCRATCHPAD.handler(
        ctx, {"content": "- DONE: A\n- still pending: B"},
    )
    await UPDATE_SCRATCHPAD.handler(
        ctx, {"content": "- still pending: B", "mode": "overwrite"},
    )
    path = tmp_path / "memory" / "scratchpad" / "dispatcher.md"
    assert path.read_text(encoding="utf-8") == "- still pending: B"


@pytest.mark.asyncio
async def test_scratchpad_is_per_agent_isolated(
    ctx: ToolContext, tmp_path: Path,
) -> None:
    """The sandbox derives the path from ``ctx.agent_id``, not from a
    caller argument — so flipping agent_id (a wakeup running as
    analyst-1 instead of dispatcher) writes a different file."""
    await UPDATE_SCRATCHPAD.handler(ctx, {"content": "dispatcher TODO"})

    analyst_ctx = ToolContext(
        repos=ctx.repos,
        task_id=ctx.task_id,
        wakeup_id=ctx.wakeup_id,
        persona_name="analyst",
        agent_id="analyst-1",
        extras=ctx.extras,
    )
    await UPDATE_SCRATCHPAD.handler(analyst_ctx, {"content": "analyst TODO"})

    sp = tmp_path / "memory" / "scratchpad"
    dispatcher_file = (sp / "dispatcher.md").read_text(encoding="utf-8")
    analyst_file = (sp / "analyst-1.md").read_text(encoding="utf-8")
    assert dispatcher_file == "dispatcher TODO"
    assert analyst_file == "analyst TODO"


@pytest.mark.asyncio
async def test_rejects_invalid_mode(ctx: ToolContext) -> None:
    with pytest.raises(ToolError, match="mode must be"):
        await UPDATE_SCRATCHPAD.handler(
            ctx, {"content": "x", "mode": "delete"},
        )


@pytest.mark.asyncio
async def test_rejects_non_string_content(ctx: ToolContext) -> None:
    with pytest.raises(ToolError, match="content required"):
        await UPDATE_SCRATCHPAD.handler(ctx, {"content": 42})


@pytest.mark.asyncio
async def test_rejects_when_memory_root_unconfigured(
    repos: SqliteRepositories,
) -> None:
    """Without ``memory_root`` in ctx.extras the tool has no sandbox to
    enforce. Refuse rather than write somewhere random."""
    await repos.personas.upsert(
        Persona(name="dispatcher", role_description="d", system_prompt="d")
    )
    await repos.agents.create(agent_id="dispatcher", persona_name="dispatcher")
    task_id = await repos.tasks.create(
        TaskSpec(agent_id="dispatcher", goal="g", acceptance="a"),
    )
    wakeup_id = await repos.wakeups.start(task_id, "dispatcher", agent_id="dispatcher")
    await repos.tasks.claim_lease(task_id, wakeup_id, duration_sec=600)
    bare_ctx = ToolContext(
        repos=repos,
        task_id=task_id,
        wakeup_id=wakeup_id,
        persona_name="dispatcher",
        agent_id="dispatcher",
        # No memory_root → must refuse
    )
    with pytest.raises(ToolError, match="memory_root"):
        await UPDATE_SCRATCHPAD.handler(bare_ctx, {"content": "x"})


@pytest.mark.asyncio
async def test_size_cap_refuses_oversized_writes(
    ctx: ToolContext,
) -> None:
    """Scratchpad is working memory, not archive. The 32 KiB cap
    forces the model to curate via overwrite instead of accumulating
    forever. Bulk content belongs in ``facts/agent-<id>-notes.md``."""
    huge = "A" * (33 * 1024)
    with pytest.raises(ToolError, match="exceed"):
        await UPDATE_SCRATCHPAD.handler(
            ctx, {"content": huge, "mode": "overwrite"},
        )


# ---------------------------------------------------------------------------
# Persona allowlist alignment
# ---------------------------------------------------------------------------


def test_update_scratchpad_in_every_llm_persona_allowlist() -> None:
    """Every LLM-driven persona (i.e. not ``owner``, which is a human)
    must have ``update_scratchpad`` in its allowed_lyre_tools, so its
    identity preamble's "your short-term memory lives here" pointer
    is actionable. Without this any persona-specific gap silently
    makes scratchpad unreachable for that role."""
    from lyre.personas.seed import (
        _shipped_persona_files,
        load_persona_from_file,
    )

    for path in _shipped_persona_files():
        persona = load_persona_from_file(path)
        if persona.name == "owner":
            continue  # not an LLM agent
        assert "update_scratchpad" in persona.allowed_lyre_tools, (
            f"persona {persona.name!r} is missing update_scratchpad — "
            f"short-term memory will be inaccessible for this role"
        )
