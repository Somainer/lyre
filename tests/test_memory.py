"""Tests for the filesystem-backed memory layer.

Memory = files in `~/.lyre/memory/` per FOUNDATION §3.4 + the simpler design
chosen in 2026-05-17: 0 dedicated tools, agents read via `shell_exec cat`
and write via `shell_exec` redirects (Tier-matrix governs which dirs each
persona may write to).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lyre.persistence.models import Persona, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.context import assemble_system_prompt
from lyre.runtime.memory import (
    OWNER_SOUL_PLACEHOLDER,
    ensure_skeleton,
    format_memory_index,
    scan_memory_dir,
    write_default_owner_soul,
)
from lyre.runtime.tools import ToolContext
from lyre.runtime.tools.shell import SHELL_EXEC


def _write_md(
    path: Path, frontmatter: dict | None = None, body: str = ""
) -> None:
    import yaml
    path.parent.mkdir(parents=True, exist_ok=True)
    parts = []
    if frontmatter is not None:
        parts.append("---")
        parts.append(yaml.safe_dump(frontmatter, sort_keys=False).strip())
        parts.append("---")
        parts.append("")
    parts.append(body)
    path.write_text("\n".join(parts), encoding="utf-8")


# ---------------------------------------------------------------------------
# Skeleton + Owner Soul seed
# ---------------------------------------------------------------------------


def test_ensure_skeleton_creates_all_canonical_dirs(tmp_path: Path) -> None:
    created = ensure_skeleton(tmp_path)
    paths = {p.relative_to(tmp_path).as_posix() for p in created}
    # Skills moved out of memory/ to ~/.lyre/skills/ in B1.
    assert paths == {"facts", "personas"}
    # Idempotent: 2nd call creates nothing new.
    assert ensure_skeleton(tmp_path) == []


def test_owner_soul_seed_writes_placeholder_once(tmp_path: Path) -> None:
    p = write_default_owner_soul(tmp_path)
    assert p.exists()
    assert p.read_text() == OWNER_SOUL_PLACEHOLDER
    # Don't overwrite an existing Soul.
    p.write_text("---\ndescription: changed\n---\nnew body")
    write_default_owner_soul(tmp_path)
    assert "new body" in p.read_text()


# ---------------------------------------------------------------------------
# scan_memory_dir
# ---------------------------------------------------------------------------


def test_scan_empty_root_returns_empty(tmp_path: Path) -> None:
    assert scan_memory_dir(tmp_path) == []


def test_scan_nonexistent_root_returns_empty(tmp_path: Path) -> None:
    assert scan_memory_dir(tmp_path / "nope") == []


def test_scan_groups_by_directory(tmp_path: Path) -> None:
    ensure_skeleton(tmp_path)
    _write_md(
        tmp_path / "facts" / "default-branch.md",
        {"description": "lisa-lang default branch is main", "scope": "lisa-lang"},
        "body 1",
    )
    _write_md(
        tmp_path / "personas" / "owner.md",
        {"description": "Owner Soul"},
        "body 2",
    )

    entries = scan_memory_dir(tmp_path)
    by_kind = {(e.kind, e.name): e for e in entries}
    assert ("fact", "default-branch") in by_kind
    assert ("persona", "owner") in by_kind

    fact = by_kind[("fact", "default-branch")]
    assert fact.description == "lisa-lang default branch is main"
    assert fact.scope == "lisa-lang"
    assert fact.rel_path == "facts/default-branch.md"


def test_scan_skips_non_md_and_hidden_files(tmp_path: Path) -> None:
    ensure_skeleton(tmp_path)
    (tmp_path / "facts" / "ignore.txt").write_text("nope")
    (tmp_path / "facts" / ".hidden.md").write_text("nope")
    _write_md(
        tmp_path / "facts" / "real.md",
        {"description": "x"}, "y",
    )
    entries = scan_memory_dir(tmp_path)
    assert {e.name for e in entries} == {"real"}


def test_scan_tolerates_missing_frontmatter(tmp_path: Path) -> None:
    ensure_skeleton(tmp_path)
    (tmp_path / "facts" / "raw.md").write_text("just body, no frontmatter")
    entries = scan_memory_dir(tmp_path)
    assert len(entries) == 1
    assert entries[0].name == "raw"
    assert entries[0].frontmatter == {}
    assert entries[0].description == ""


def test_scan_tolerates_malformed_frontmatter(tmp_path: Path) -> None:
    ensure_skeleton(tmp_path)
    (tmp_path / "facts" / "bad.md").write_text(
        "---\nnot:valid:yaml::\n---\nbody"
    )
    entries = scan_memory_dir(tmp_path)
    # Either parses to empty or raises silently — we want the file to still
    # appear, just with no description.
    assert len(entries) == 1
    assert entries[0].name == "bad"


# ---------------------------------------------------------------------------
# format_memory_index
# ---------------------------------------------------------------------------


def test_format_index_empty_returns_empty_string() -> None:
    assert format_memory_index([]) == ""


def test_format_index_groups_and_describes(tmp_path: Path) -> None:
    ensure_skeleton(tmp_path)
    _write_md(
        tmp_path / "facts" / "c.md",
        {"description": "fact C", "scope": "lisa-lang"}, "",
    )
    _write_md(
        tmp_path / "personas" / "owner.md",
        {"description": "Owner Soul"}, "",
    )
    entries = scan_memory_dir(tmp_path)
    out = format_memory_index(entries)

    assert "## Available global memory" in out
    assert "### Facts" in out
    assert "### Persona profiles" in out
    assert "`facts/c.md` — fact C  [scope: lisa-lang]" in out
    assert "`personas/owner.md` — Owner Soul" in out
    # Skills moved out — memory index doesn't render them anymore.
    assert "### Skills" not in out


def test_format_index_skips_empty_groups(tmp_path: Path) -> None:
    ensure_skeleton(tmp_path)
    _write_md(
        tmp_path / "facts" / "only.md",
        {"description": "lone fact"}, "",
    )
    out = format_memory_index(scan_memory_dir(tmp_path))
    assert "### Facts" in out
    assert "### Persona profiles" not in out
    assert "### Skills" not in out


# ---------------------------------------------------------------------------
# assemble_system_prompt injection
# ---------------------------------------------------------------------------


def _persona() -> Persona:
    return Persona(
        name="worker",
        role_description="worker role",
        system_prompt="you write code",
    )


def test_assemble_system_prompt_without_memory_root_unchanged() -> None:
    prompt = assemble_system_prompt(_persona())
    # Identity preamble is always at the top (so the model knows its own
    # agent id); the role + system_prompt body follow unchanged.
    assert "You are agent **worker**" in prompt
    assert "worker role\n\nyou write code" in prompt


def test_assemble_system_prompt_with_empty_memory_root_no_section(
    tmp_path: Path,
) -> None:
    ensure_skeleton(tmp_path)
    # No files yet.
    prompt = assemble_system_prompt(_persona(), memory_root=tmp_path)
    assert "Available global memory" not in prompt


def test_assemble_system_prompt_includes_memory_index(tmp_path: Path) -> None:
    ensure_skeleton(tmp_path)
    write_default_owner_soul(tmp_path)
    _write_md(
        tmp_path / "facts" / "x.md",
        {"description": "fact X"}, "",
    )
    prompt = assemble_system_prompt(_persona(), memory_root=tmp_path)
    assert "worker role" in prompt
    assert "## Available global memory" in prompt
    assert "facts/x.md" in prompt
    assert "personas/owner.md" in prompt


# ---------------------------------------------------------------------------
# End-to-end: worker uses shell_exec to read/write memory
# ---------------------------------------------------------------------------


@pytest.fixture
async def worker_ctx(
    repos: SqliteRepositories, tmp_path: Path,
) -> tuple[ToolContext, Path]:
    await repos.personas.upsert(
        Persona(name="worker", role_description="w", system_prompt="w")
    )
    task_id = await repos.tasks.create(
        TaskSpec(persona_name="worker", goal="g", acceptance="a")
    )
    wakeup_id = await repos.wakeups.start(task_id, "worker")
    await repos.tasks.claim_lease(task_id, wakeup_id, duration_sec=600)

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    memory = tmp_path / "memory"
    ensure_skeleton(memory)

    ctx = ToolContext(
        repos=repos, task_id=task_id, wakeup_id=wakeup_id,
        persona_name="worker",
        extras={"worktree": str(worktree)},
    )
    return ctx, memory


@pytest.mark.asyncio
async def test_shell_exec_can_read_memory_files(
    worker_ctx: tuple[ToolContext, Path],
) -> None:
    """No tool needed — worker uses `cat` on memory paths directly."""
    ctx, memory = worker_ctx
    target = memory / "facts" / "default-branch.md"
    _write_md(
        target, {"description": "default branch = main"}, "body content"
    )
    res = await SHELL_EXEC.handler(
        ctx, {"argv": ["cat", str(target)]},
    )
    assert res["exit_code"] == 0
    assert "default branch = main" in res["stdout"]
    assert "body content" in res["stdout"]


# NOTE: end-to-end skill propose/approve flow lives in test_skills.py (B1
# moved skills out of memory/ to top-level ~/.lyre/skills/).
