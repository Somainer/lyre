"""Tests for the WorktreeManager.

Real ssh-keygen + ssh-agent + ssh-add are invoked. Skip if any tool is missing.
"""

from __future__ import annotations

import os
import shutil
import signal
from pathlib import Path

import pytest

from lyre.runtime.worktree import (
    WorktreeError,
    WorktreeManager,
    _parse_ssh_agent_output,
)

_HAVE_SSH = all(
    shutil.which(t) is not None for t in ("ssh-keygen", "ssh-agent", "ssh-add", "git")
)

pytestmark = pytest.mark.skipif(
    not _HAVE_SSH,
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
    with pytest.raises(WorktreeError):
        _parse_ssh_agent_output(b"nothing parseable here")


@pytest.mark.asyncio
async def test_prepare_creates_dir_keypair_and_agent(tmp_path: Path) -> None:
    mgr = WorktreeManager(root=tmp_path)
    handle = await mgr.prepare("task-abc")
    try:
        assert handle.dir.is_dir()
        assert handle.dir.name == "task-abc"
        assert handle.ssh_priv_key_path.exists()
        # Public key is the standard ssh format.
        assert handle.ssh_pub_key.startswith("ssh-ed25519 ")
        assert "lyre/task-task-abc" in handle.ssh_pub_key
        # Agent process is alive (signal 0 = check).
        os.kill(handle.ssh_agent_pid, 0)
        # Socket exists.
        assert Path(handle.ssh_auth_sock).exists()
    finally:
        await mgr.cleanup(handle)


@pytest.mark.asyncio
async def test_cleanup_kills_agent_and_removes_dir(tmp_path: Path) -> None:
    import asyncio as _asyncio

    mgr = WorktreeManager(root=tmp_path)
    handle = await mgr.prepare("task-1")
    await mgr.cleanup(handle, remove_dir=True)

    assert not handle.dir.exists()
    # Agent shutdown after SIGTERM can take a moment on macOS. Poll up to 2s.
    deadline = 2.0
    interval = 0.05
    elapsed = 0.0
    dead = False
    while elapsed < deadline:
        try:
            os.kill(handle.ssh_agent_pid, 0)
        except ProcessLookupError:
            dead = True
            break
        await _asyncio.sleep(interval)
        elapsed += interval
    assert dead, f"ssh-agent pid {handle.ssh_agent_pid} still alive after {deadline}s"


@pytest.mark.asyncio
async def test_cleanup_keeps_dir_when_remove_dir_false(tmp_path: Path) -> None:
    mgr = WorktreeManager(root=tmp_path)
    handle = await mgr.prepare("task-keep")
    await mgr.cleanup(handle, remove_dir=False)
    assert handle.dir.exists()
    assert handle.ssh_priv_key_path.exists()


@pytest.mark.asyncio
async def test_prepare_reuses_existing_keypair_on_restart(tmp_path: Path) -> None:
    """Q5: kill-restart should reuse the same keypair. We simulate by calling
    prepare twice — the second call must NOT regenerate the key file."""
    mgr = WorktreeManager(root=tmp_path)
    h1 = await mgr.prepare("task-restart")
    pub1 = h1.ssh_pub_key
    # Mimic process death: kill agent, leave files.
    os.kill(h1.ssh_agent_pid, signal.SIGTERM)
    h2 = await mgr.prepare("task-restart")
    try:
        assert h2.ssh_pub_key == pub1, "keypair must be reused across restarts"
        assert h2.ssh_agent_pid != h1.ssh_agent_pid, "new agent must be spawned"
    finally:
        await mgr.cleanup(h2)


@pytest.mark.asyncio
async def test_two_tasks_get_isolated_dirs_and_agents(tmp_path: Path) -> None:
    mgr = WorktreeManager(root=tmp_path)
    h1 = await mgr.prepare("task-a")
    h2 = await mgr.prepare("task-b")
    try:
        assert h1.dir != h2.dir
        assert h1.ssh_auth_sock != h2.ssh_auth_sock
        assert h1.ssh_agent_pid != h2.ssh_agent_pid
        assert h1.ssh_pub_key != h2.ssh_pub_key
    finally:
        await mgr.cleanup(h1)
        await mgr.cleanup(h2)


@pytest.mark.asyncio
async def test_env_overlay_carries_auth_sock_and_pid(tmp_path: Path) -> None:
    mgr = WorktreeManager(root=tmp_path)
    h = await mgr.prepare("task-env")
    try:
        overlay = h.env_overlay()
        assert overlay["SSH_AUTH_SOCK"] == h.ssh_auth_sock
        assert overlay["SSH_AGENT_PID"] == str(h.ssh_agent_pid)
    finally:
        await mgr.cleanup(h)
