"""Builtin skills — read DIRECTLY from the packaged library (no mirror).

Packaged skills under src/lyre/data/skills/ are scanned in place by the skill
menu and read in place by read_memory (which permits that one trusted read-only
root). Builtin skills are code-like — they track the installed version with no
copy step. An owner skill of the same name in approved/ shadows the builtin
(override).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.skills import (
    ensure_skills_skeleton,
    load_skills_for_context,
    shipped_skills_dir,
)
from lyre.runtime.tools import ToolContext, ToolError
from lyre.runtime.tools.introspect import READ_MEMORY


def _ctx(repos: SqliteRepositories, memory_root: Path) -> ToolContext:
    return ToolContext(
        repos=repos, task_id="t", wakeup_id="w", persona_name="dispatcher",
        extras={"memory_root": str(memory_root)},
    )


def test_builtin_skill_surfaces_from_package_no_copy(tmp_path: Path) -> None:
    ensure_skills_skeleton(tmp_path)
    by_name = {s.name: s for s in load_skills_for_context(tmp_path).skills}
    assert "adversarial-review" in by_name
    s = by_name["adversarial-review"]
    assert s.source == "builtin"
    # Read in place from the package — NOT copied/mirrored into ~/.lyre.
    assert shipped_skills_dir().resolve() in s.path.resolve().parents
    assert not (tmp_path / "skills" / "builtin").exists()


def test_approved_shadows_builtin_override(tmp_path: Path) -> None:
    ensure_skills_skeleton(tmp_path)
    ov = tmp_path / "skills" / "approved" / "adversarial-review"
    ov.mkdir(parents=True)
    (ov / "SKILL.md").write_text(
        "---\nname: adversarial-review\ndescription: owner override version\n"
        "---\noverridden body\n",
        encoding="utf-8",
    )
    s = {x.name: x for x in load_skills_for_context(tmp_path).skills}["adversarial-review"]
    assert s.source == "approved"  # owner override wins on collision
    assert s.description == "owner override version"


@pytest.mark.asyncio
async def test_read_memory_reads_builtin_skill_body_in_place(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    skill_md = shipped_skills_dir() / "adversarial-review" / "SKILL.md"
    out = await READ_MEMORY.handler(_ctx(repos, memory), {"rel_path": str(skill_md)})
    assert "prosecution" in out["body"].lower()


@pytest.mark.asyncio
async def test_read_memory_still_rejects_foreign_absolute_paths(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    # An absolute path NOT under the trusted package skills dir is still rejected
    # — the builtin allowance is bounded to that one read-only root.
    with pytest.raises(ToolError, match="relative"):
        await READ_MEMORY.handler(_ctx(repos, memory), {"rel_path": "/etc/passwd"})
