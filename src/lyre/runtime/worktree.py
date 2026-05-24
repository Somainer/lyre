"""Per-task sandbox tmpdir lifecycle.

Every wakeup gets a clean directory under
``object_store/worktrees/{task_id}/`` to work in — for code edits,
shell scratch files, python_exec scripts, downloaded inputs,
whatever. The tmpdir is **just a tmpdir**; it carries no git or SSH
state.

If the task needs to operate on a git working copy (`TaskSpec.
git_context` was set when the task was dispatched), the runtime
overlays a ``GitContext`` provisioning step on top — see
``runtime/git_context.py``. The split matters because most worker
tasks today aren't code edits: skill migration, research,
data shaping, log parsing all want a sandbox but no SSH key.
Coupling worktree creation with SSH-keygen was the anti-pattern
that surfaced when a worker died complaining "cwd is not a git
repo" after being assigned a skill-migration task.

Cleanup on task success: ``rm -rf`` the tmpdir. On failure we
keep it for postmortem (Q5 says state must be reconstructable;
remote-side git state is the source of truth for retry, so local
can be discarded on success).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import structlog

log = structlog.get_logger()


@dataclass(frozen=True)
class WorktreeHandle:
    """Just the sandbox dir. SSH-related state lives on
    ``GitContextHandle`` when a git_context is provisioned."""

    task_id: str
    dir: Path


class WorktreeError(RuntimeError):
    """Raised when worktree preparation fails."""


class WorktreeManager:
    """Owns the prepare/cleanup lifecycle. One instance per scheduler."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    async def prepare(self, task_id: str) -> WorktreeHandle:
        """Make / reuse a tmpdir at ``<root>/<task_id>/``.

        Idempotent on the same task_id (kill-restart safety: the
        dir survives a process crash so the recovered wakeup picks
        up where the old one left off).
        """
        wd = self.root / task_id
        wd.mkdir(parents=True, exist_ok=True)
        log.info("worktree_prepared", task_id=task_id, dir=str(wd))
        return WorktreeHandle(task_id=task_id, dir=wd)

    async def cleanup(
        self,
        handle: WorktreeHandle,
        *,
        remove_dir: bool = True,
    ) -> None:
        """Optionally remove the tmpdir. ``remove_dir=False`` is used
        on task failure so a human / next wakeup can inspect what
        happened. SSH-agent teardown (when a git_context was active)
        is the GitContextProvisioner's job, called separately by the
        scheduler."""
        if remove_dir:
            shutil.rmtree(handle.dir, ignore_errors=True)
        log.info(
            "worktree_cleaned",
            task_id=handle.task_id,
            removed_dir=remove_dir,
        )
