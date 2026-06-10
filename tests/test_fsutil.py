"""Atomic-write helper: the kill-test law for markdown-tier files.

``update_scratchpad`` used to rewrite the scratchpad with a bare
``write_text`` (truncate-then-write) — a SIGKILL mid-write destroyed the
agent's working memory with no recovery source. These tests pin the
shared helper's guarantee: an interrupted write leaves the prior
complete file, never a truncated one, and no temp debris.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lyre.fsutil import atomic_write_text
from lyre.persistence.models import Persona, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.tools import ToolContext
from lyre.runtime.tools.introspect import UPDATE_SCRATCHPAD


def test_atomic_write_creates_and_overwrites(tmp_path: Path) -> None:
    p = tmp_path / "f.md"
    atomic_write_text(p, "v1")
    assert p.read_text(encoding="utf-8") == "v1"
    atomic_write_text(p, "v2")
    assert p.read_text(encoding="utf-8") == "v2"
    assert list(tmp_path.iterdir()) == [p], "no temp debris"


def test_interrupted_write_preserves_prior_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure at the publish step (stand-in for SIGKILL anywhere in the
    write) must leave the previous complete file untouched."""
    p = tmp_path / "f.md"
    atomic_write_text(p, "the working memory I must not lose")

    def _boom(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        raise OSError("simulated kill at publish")

    monkeypatch.setattr("lyre.fsutil.os.replace", _boom)
    with pytest.raises(OSError, match="simulated kill"):
        atomic_write_text(p, "half-finished replacement")

    assert p.read_text(encoding="utf-8") == "the working memory I must not lose"
    assert list(tmp_path.iterdir()) == [p], "failed write must clean its temp"


@pytest.mark.asyncio
async def test_scratchpad_survives_interrupted_overwrite(
    repos: SqliteRepositories, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end through the tool: an overwrite that dies mid-publish must
    not leave the scratchpad empty/truncated (it is a durability-tier file
    the identity preamble tells every agent to read at wakeup start)."""
    await repos.personas.upsert(
        Persona(name="dispatcher", role_description="d", system_prompt="d")
    )
    await repos.agents.create(agent_id="dispatcher", persona_name="dispatcher")
    task_id = await repos.tasks.create(
        TaskSpec(agent_id="dispatcher", goal="g", acceptance="a"),
    )
    wakeup_id = await repos.wakeups.start(
        task_id, "dispatcher", agent_id="dispatcher"
    )
    memory_root = tmp_path / "memory"
    memory_root.mkdir()
    ctx = ToolContext(
        repos=repos,
        task_id=task_id,
        wakeup_id=wakeup_id,
        persona_name="dispatcher",
        agent_id="dispatcher",
        extras={"memory_root": str(memory_root)},
    )
    await UPDATE_SCRATCHPAD.handler(ctx, {"content": "- commitments I made"})

    monkeypatch.setattr(
        "lyre.fsutil.os.replace",
        lambda s, d: (_ for _ in ()).throw(OSError("simulated kill")),
    )
    with pytest.raises(OSError, match="simulated kill"):
        await UPDATE_SCRATCHPAD.handler(
            ctx, {"content": "overwritten", "mode": "overwrite"},
        )

    pad = memory_root / "scratchpad" / "dispatcher.md"
    assert pad.read_text(encoding="utf-8") == "- commitments I made"
