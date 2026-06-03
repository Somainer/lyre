"""CD-3a: read_memory can reach skills at ~/.lyre/skills/ + personas use that path.

B1 moved skills out of memory/ to the top-level ~/.lyre/skills/ (sibling of
memory_root), but the persona docs still pointed agents at ~/.lyre/memory/skills/
— so a proposed/promoted skill was written where the loader never scans (and
read_memory, sandboxed to memory_root, couldn't read it). CD-3a consolidates on
~/.lyre/skills/: personas use it, and read_memory resolves a `skills/...`
rel_path under lyre_home (bounded to the skills dir). Prerequisite for CD-3.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import lyre.personas as personas_pkg
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.tools import ToolContext, ToolError
from lyre.runtime.tools.introspect import READ_MEMORY


def _ctx(repos: SqliteRepositories, memory_root: Path) -> ToolContext:
    return ToolContext(
        repos=repos, task_id="t", wakeup_id="w", persona_name="reviewer",
        extras={"memory_root": str(memory_root)},
    )


@pytest.mark.asyncio
async def test_read_memory_reads_skill_at_lyre_home_skills(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    # lyre_home = tmp_path; memory_root = tmp_path/memory; skills = tmp_path/skills.
    memory = tmp_path / "memory"
    memory.mkdir()
    skill = tmp_path / "skills" / "approved" / "use-codex" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# how to drive codex\nstep 1...", encoding="utf-8")

    out = await READ_MEMORY.handler(
        _ctx(repos, memory), {"rel_path": "skills/approved/use-codex/SKILL.md"}
    )
    assert "how to drive codex" in out["body"]


@pytest.mark.asyncio
async def test_read_memory_still_reads_memory_paths(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    memory = tmp_path / "memory"
    (memory / "facts").mkdir(parents=True)
    (memory / "facts" / "foo.md").write_text("a fact", encoding="utf-8")
    out = await READ_MEMORY.handler(_ctx(repos, memory), {"rel_path": "facts/foo.md"})
    assert "a fact" in out["body"]


@pytest.mark.asyncio
async def test_read_memory_skills_cannot_escape(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    (tmp_path / "secret.md").write_text("nope", encoding="utf-8")
    # `..` is rejected outright — can't climb out of the skills dir to siblings.
    with pytest.raises(ToolError):
        await READ_MEMORY.handler(_ctx(repos, memory), {"rel_path": "skills/../secret.md"})


def test_shipped_personas_use_canonical_skills_path() -> None:
    # Lock the fix: no shipped persona may point agents at the dead
    # ~/.lyre/memory/skills/ location again.
    pdir = Path(personas_pkg.__file__).parent
    offenders = [
        p.name for p in pdir.glob("*.md") if "memory/skills" in p.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"personas still reference memory/skills: {offenders}"
