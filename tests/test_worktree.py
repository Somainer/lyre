"""Tests for the WorktreeManager.

After the worktree / git_context split, WorktreeManager is a pure
tmpdir lifecycle — no SSH, no git, no clone. SSH + git provisioning
moved to ``runtime.git_context.GitContextProvisioner`` (see
``test_git_context.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lyre.runtime.worktree import WorktreeManager


@pytest.mark.asyncio
async def test_prepare_creates_clean_tmpdir(tmp_path: Path) -> None:
    """First call → fresh dir at ``<root>/<task_id>``, no SSH files,
    no git, nothing — just an empty sandbox the worker can write into.
    """
    wm = WorktreeManager(root=tmp_path / "worktrees")
    handle = await wm.prepare("t-1")

    assert handle.task_id == "t-1"
    assert handle.dir == tmp_path / "worktrees" / "t-1"
    assert handle.dir.is_dir()
    # Critical invariant: no SSH dir, no ssh-agent, no .git — the
    # worktree is just a tmpdir until something (GitContextProvisioner
    # or the worker itself) populates it.
    assert not (handle.dir / ".ssh").exists()
    assert not (handle.dir / ".git").exists()
    assert list(handle.dir.iterdir()) == []


@pytest.mark.asyncio
async def test_prepare_is_idempotent_on_same_task_id(tmp_path: Path) -> None:
    """Kill-restart safety: re-running prepare for the same task_id
    reuses the existing dir (with whatever the previous wakeup wrote
    into it) rather than wiping it."""
    wm = WorktreeManager(root=tmp_path / "worktrees")
    handle1 = await wm.prepare("t-1")
    (handle1.dir / "midwork.txt").write_text("partial result", encoding="utf-8")

    handle2 = await wm.prepare("t-1")
    assert handle2.dir == handle1.dir
    assert (handle2.dir / "midwork.txt").read_text(encoding="utf-8") == (
        "partial result"
    )


@pytest.mark.asyncio
async def test_cleanup_removes_dir(tmp_path: Path) -> None:
    """Success path: ``remove_dir=True`` wipes the dir."""
    wm = WorktreeManager(root=tmp_path / "worktrees")
    handle = await wm.prepare("t-1")
    (handle.dir / "scratch.txt").write_text("x", encoding="utf-8")
    assert handle.dir.exists()

    await wm.cleanup(handle, remove_dir=True)
    assert not handle.dir.exists()


@pytest.mark.asyncio
async def test_cleanup_keeps_dir_when_remove_dir_false(tmp_path: Path) -> None:
    """Failure path: ``remove_dir=False`` leaves the dir for postmortem."""
    wm = WorktreeManager(root=tmp_path / "worktrees")
    handle = await wm.prepare("t-1")
    (handle.dir / "scratch.txt").write_text("x", encoding="utf-8")

    await wm.cleanup(handle, remove_dir=False)
    assert handle.dir.exists()
    assert (handle.dir / "scratch.txt").read_text(encoding="utf-8") == "x"


@pytest.mark.asyncio
async def test_two_tasks_get_isolated_dirs(tmp_path: Path) -> None:
    """Same WorktreeManager + two task_ids = two independent dirs.
    Tasks must not be able to step on each other even if they're
    running concurrent wakeups."""
    wm = WorktreeManager(root=tmp_path / "worktrees")
    h1 = await wm.prepare("t-1")
    h2 = await wm.prepare("t-2")

    assert h1.dir != h2.dir
    (h1.dir / "a.txt").write_text("one", encoding="utf-8")
    (h2.dir / "a.txt").write_text("two", encoding="utf-8")
    assert (h1.dir / "a.txt").read_text(encoding="utf-8") == "one"
    assert (h2.dir / "a.txt").read_text(encoding="utf-8") == "two"
