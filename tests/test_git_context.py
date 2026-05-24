"""Tests for the GitContextProvisioner.

Real ssh-keygen + ssh-agent + ssh-add + git are invoked against a
local ``file://`` bare repo (no external network). Skip if any tool
is missing.

The full integration with the scheduler (TaskSpec.git_context →
clone + checkout before worker arrives) lives in
``test_worktree_e2e_git.py``; here we just exercise the
provisioner's prepare / cleanup surface in isolation.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from lyre.persistence.models import GitContext
from lyre.runtime.git_context import (
    GitContextProvisioner,
    _parse_ssh_agent_output,
)

_HAVE_TOOLS = all(
    shutil.which(t) is not None
    for t in ("ssh-keygen", "ssh-agent", "ssh-add", "git")
)

pytestmark = pytest.mark.skipif(
    not _HAVE_TOOLS,
    reason="ssh-keygen / ssh-agent / ssh-add / git not on PATH",
)


def test_parse_ssh_agent_output_handles_bash_format() -> None:
    sample = (
        b"SSH_AUTH_SOCK=/tmp/ssh-XYZ/agent.123; export SSH_AUTH_SOCK;\n"
        b"SSH_AGENT_PID=42; export SSH_AGENT_PID;\n"
        b"echo Agent pid 42;\n"
    )
    sock, pid = _parse_ssh_agent_output(sample)
    assert sock == "/tmp/ssh-XYZ/agent.123"
    assert pid == 42


def test_parse_ssh_agent_output_raises_on_garbage() -> None:
    from lyre.runtime.git_context import GitContextError
    with pytest.raises(GitContextError):
        _parse_ssh_agent_output(b"no SSH stuff here")


def _make_bare_repo(tmp_path: Path) -> Path:
    """Build a ``file://`` bare repo with one commit on main."""
    import subprocess  # noqa: S404 — test infra only

    seed = tmp_path / "seed"
    seed.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=seed, check=True)  # noqa: S603, S607
    subprocess.run(  # noqa: S603, S607
        ["git", "-c", "user.email=t@e", "-c", "user.name=t",
         "commit", "--allow-empty", "-q", "-m", "init"],
        cwd=seed, check=True,
    )
    bare = tmp_path / "bare.git"
    subprocess.run(  # noqa: S603, S607
        ["git", "clone", "-q", "--bare", str(seed), str(bare)],
        check=True,
    )
    return bare


@pytest.mark.asyncio
async def test_prepare_clones_and_checks_out_target_branch(
    tmp_path: Path,
) -> None:
    """Happy path: provisioner generates SSH key, clones the repo
    into the worktree, switches to a new ``target_branch`` based on
    ``base_branch``. Returned handle exposes env_overlay for git
    operations."""
    bare = _make_bare_repo(tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    gc = GitContext(
        repo_url=f"file://{bare}",
        target_branch="feature/x",
        base_branch="main",
    )

    prov = GitContextProvisioner()
    handle = await prov.prepare(
        task_id="t-1", worktree_dir=worktree, git_context=gc,
    )
    try:
        # Worktree is now a working copy of the bare repo.
        assert (worktree / ".git").is_dir()
        # SSH key + pub are present, perms tightened.
        assert handle.ssh_priv_key_path.exists()
        assert handle.ssh_pub_key.startswith("ssh-ed25519 ")
        # Agent is running and env_overlay carries the sock + pid.
        ov = handle.env_overlay()
        assert ov["SSH_AUTH_SOCK"] == handle.ssh_auth_sock
        assert int(ov["SSH_AGENT_PID"]) == handle.ssh_agent_pid
        # target_branch was created and checked out — read HEAD ref
        # straight from the .git dir to avoid invoking a subprocess
        # from inside the async test body.
        head_ref = (worktree / ".git" / "HEAD").read_text(encoding="utf-8").strip()
        assert head_ref == "ref: refs/heads/feature/x"
    finally:
        await prov.cleanup(handle)


@pytest.mark.asyncio
async def test_cleanup_kills_ssh_agent(tmp_path: Path) -> None:
    """``cleanup`` SIGTERMs the ssh-agent so it doesn't leak."""
    bare = _make_bare_repo(tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    prov = GitContextProvisioner()
    handle = await prov.prepare(
        task_id="t-1", worktree_dir=worktree,
        git_context=GitContext(
            repo_url=f"file://{bare}",
            target_branch="feature/x",
        ),
    )
    pid = handle.ssh_agent_pid

    await prov.cleanup(handle)
    # Agent should be gone. ``os.kill(pid, 0)`` raises ProcessLookupError
    # if the process is dead.
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
