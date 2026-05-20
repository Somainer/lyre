"""End-to-end: scheduler runs a worker that clones a real bare git repo,
edits a file, commits, pushes. No GitHub, no SSH transport — uses `file://`
URLs so we exercise the worktree + shell_exec + git path without needing
external infra.

This is the Sprint 1 §5.3 smoke test minus the GitHub-side push/PR (which the
SSH key would unlock once we add GitHub API integration).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

import pytest

from lyre.adapter.llm_adapter import (
    ContentDelta,
    ToolUseComplete,
    TurnComplete,
)
from lyre.config import Config
from lyre.persistence.models import Persona, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.worktree import WorktreeManager
from lyre.scheduler.scheduler import Scheduler

from .fake_adapter import FakeAdapter
from .helpers import fake_entry, fake_registry

_HAVE_TOOLS = all(
    shutil.which(t) is not None for t in ("ssh-keygen", "ssh-agent", "ssh-add", "git")
)
pytestmark = pytest.mark.skipif(not _HAVE_TOOLS, reason="git / ssh tooling missing")


async def _run(argv: list[str], cwd: Path | None = None) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Lyre Test",
            "GIT_AUTHOR_EMAIL": "test@lyre.local",
            "GIT_COMMITTER_NAME": "Lyre Test",
            "GIT_COMMITTER_EMAIL": "test@lyre.local",
        },
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out, err


@pytest.fixture
async def bare_repo_with_seed(tmp_path: Path) -> Path:
    """Spin up a bare git repo at tmp_path/origin.git, seeded with one commit
    containing README.md.

    The bare repo's HEAD points at branch 'main'.
    """
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    seed.mkdir()

    # Init the bare repo first so we can configure its default branch.
    rc, _, err = await _run(["git", "init", "--bare", "-b", "main", str(origin)])
    assert rc == 0, err

    # Make a seed workdir, commit a file, push to origin.
    for argv in [
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "test@lyre.local"],
        ["git", "config", "user.name", "Lyre Test"],
    ]:
        rc, _, err = await _run(argv, cwd=seed)
        assert rc == 0, err
    (seed / "README.md").write_text("# Hello\n", encoding="utf-8")
    for argv in [
        ["git", "add", "README.md"],
        ["git", "commit", "-m", "seed"],
        ["git", "remote", "add", "origin", f"file://{origin}"],
        ["git", "push", "origin", "main"],
    ]:
        rc, _, err = await _run(argv, cwd=seed)
        assert rc == 0, err

    return origin


def _make_config(tmp_path: Path) -> Config:
    mem = tmp_path / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    return Config(
        db_path=tmp_path / "x.db",
        object_store_path=tmp_path / "objstore",
        memory_path=mem,
        anthropic_api_key="fake",
        anthropic_base_url=None,
        default_model="m",
        model_override=None,
    )


@pytest.mark.asyncio
async def test_worker_can_clone_edit_commit_push(
    repos: SqliteRepositories,
    tmp_path: Path,
    bare_repo_with_seed: Path,
) -> None:
    """Scripted worker: shell_exec clones origin, edits README, commits,
    pushes to a feature branch. Bare repo afterwards must contain the branch.
    """
    origin_url = f"file://{bare_repo_with_seed}"
    cfg = _make_config(tmp_path)
    cfg.object_store_path.mkdir(parents=True, exist_ok=True)

    # Persona that lives in a worktree + has shell_exec.
    await repos.personas.upsert(
        Persona(
            name="worker",
            role_description="worker",
            system_prompt="you write code",
            allowed_lyre_tools=["shell_exec", "mailbox_send", "report_side_effect"],
            model_preference={
                "tier": "workhorse", "requires": ["tool_use"], "prefer": [],
            },
            needs_worktree=True,
        )
    )
    task_id = await repos.tasks.create(
        TaskSpec(
            persona_name="worker",
            goal=f"clone {origin_url}, edit README, push branch",
            acceptance="bare repo has the new branch",
        )
    )

    # Script the worker's tool calls. The agent loop emits each tool call,
    # gets a real shell_exec result, then proceeds. We push 6 turns: clone /
    # cd-check / edit / add+commit / push / final end_turn.
    fake = FakeAdapter()
    branch = "lyre/test-feature"

    fake.push_turn([
        ToolUseComplete(
            id="t1", name="shell_exec",
            input={"argv": ["git", "clone", origin_url, "repo"]},
        ),
        TurnComplete(stop_reason="tool_use"),
    ])
    fake.push_turn([
        ToolUseComplete(
            id="t2", name="shell_exec",
            input={
                "argv": ["git", "config", "user.email", "worker@lyre.local"],
                "cwd": None,  # default → worktree; we'll add cwd via subdir below
            },
        ),
        TurnComplete(stop_reason="tool_use"),
    ])
    # cwd must be inside the worktree. The cloned subdir is `<worktree>/repo`.
    # We need its absolute path — but FakeAdapter scripts are fixed strings.
    # Workaround: invoke git via `-C repo` instead of cwd.
    fake.push_turn([
        ToolUseComplete(
            id="t3", name="shell_exec",
            input={
                "argv": ["git", "-C", "repo", "config", "user.email", "worker@lyre.local"],
            },
        ),
        TurnComplete(stop_reason="tool_use"),
    ])
    fake.push_turn([
        ToolUseComplete(
            id="t4", name="shell_exec",
            input={
                "argv": ["git", "-C", "repo", "config", "user.name", "Lyre Worker"],
            },
        ),
        TurnComplete(stop_reason="tool_use"),
    ])
    fake.push_turn([
        ToolUseComplete(
            id="t5", name="shell_exec",
            input={
                "argv": ["git", "-C", "repo", "checkout", "-b", branch],
            },
        ),
        TurnComplete(stop_reason="tool_use"),
    ])
    # Edit README via a small python invocation
    fake.push_turn([
        ToolUseComplete(
            id="t6", name="shell_exec",
            input={
                "argv": [
                    sys.executable, "-c",
                    "import pathlib; p=pathlib.Path('repo/README.md');"
                    " p.write_text(p.read_text() + 'managed by lyre\\n')",
                ],
            },
        ),
        TurnComplete(stop_reason="tool_use"),
    ])
    fake.push_turn([
        ToolUseComplete(
            id="t7", name="shell_exec",
            input={"argv": ["git", "-C", "repo", "add", "README.md"]},
        ),
        TurnComplete(stop_reason="tool_use"),
    ])
    fake.push_turn([
        ToolUseComplete(
            id="t8", name="shell_exec",
            input={"argv": ["git", "-C", "repo", "commit", "-m", "managed by lyre"]},
        ),
        TurnComplete(stop_reason="tool_use"),
    ])
    fake.push_turn([
        ToolUseComplete(
            id="t9", name="shell_exec",
            input={"argv": ["git", "-C", "repo", "push", "origin", branch]},
        ),
        TurnComplete(stop_reason="tool_use"),
    ])
    fake.push_turn([
        ContentDelta(text="pushed."),
        TurnComplete(stop_reason="end_turn"),
    ])

    scheduler = Scheduler(
        repos,
        cfg,
        poll_interval_s=0.05,
        registry=fake_registry(fake_entry(id="fake.workhorse", tier="workhorse")),
        adapter_for_test=lambda e: fake,
        worktree_manager=WorktreeManager(root=cfg.object_store_path / "worktrees"),
    )
    await scheduler._tick()

    t = await repos.tasks.get(task_id)
    assert t is not None
    assert t.status == "completed", (
        f"task ended {t.status}; check transcript at "
        f"{cfg.object_store_path}/wakeups/"
    )

    # Verify the bare repo received the new branch.
    rc, out, err = await _run(
        ["git", "--git-dir", str(bare_repo_with_seed), "branch", "-a"]
    )
    assert rc == 0, err
    assert branch in out.decode(), f"branch missing in: {out!r}"

    # Worktree was cleaned up because task succeeded.
    worktree_dir = cfg.object_store_path / "worktrees" / task_id
    assert not worktree_dir.exists()
