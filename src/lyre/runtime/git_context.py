"""Optional git-working-copy provisioning, layered on top of a worktree.

Used only when a task's ``TaskSpec.git_context`` is set. Two-step
preparation:

  1. Ephemeral ed25519 SSH keypair under ``<worktree>/.ssh/``
  2. Dedicated ``ssh-agent`` process holding that key in memory
  3. ``git clone <repo_url>`` into the worktree
  4. ``git switch -c <target_branch>`` based on ``<base_branch>``

The worker walks into a fully-checked-out working copy ready for
edits. ``SSH_AUTH_SOCK`` + ``SSH_AGENT_PID`` are returned in
``env_overlay()`` so shell_exec / python_exec subprocesses can ``git
push`` and ``gh pr create`` over SSH transparently.

Cleanup tears down the ssh-agent. Tmpdir cleanup is the
``WorktreeManager``'s responsibility — the two layers stay independent
so a non-git task path doesn't touch any of this code.
"""

from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass
from pathlib import Path

import structlog

from ..persistence.models import GitContext

log = structlog.get_logger()


@dataclass(frozen=True)
class GitContextHandle:
    task_id: str
    worktree_dir: Path
    repo_url: str
    target_branch: str
    base_branch: str
    ssh_priv_key_path: Path
    ssh_pub_key: str
    ssh_auth_sock: str
    ssh_agent_pid: int

    def env_overlay(self) -> dict[str, str]:
        return {
            "SSH_AUTH_SOCK": self.ssh_auth_sock,
            "SSH_AGENT_PID": str(self.ssh_agent_pid),
        }


class GitContextError(RuntimeError):
    """Raised when git-context provisioning fails."""


async def _run(
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env if env is not None else os.environ.copy(),
        cwd=str(cwd) if cwd is not None else None,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out, err


def _parse_ssh_agent_output(out: bytes) -> tuple[str, int]:
    """``ssh-agent -s`` prints:
        SSH_AUTH_SOCK=/tmp/.../agent.PID; export SSH_AUTH_SOCK;
        SSH_AGENT_PID=PID; export SSH_AGENT_PID;
        echo Agent pid PID;
    Parse SOCK + PID out.
    """
    sock: str | None = None
    pid: int | None = None
    for raw in out.decode("utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line.startswith("SSH_AUTH_SOCK="):
            sock = line[len("SSH_AUTH_SOCK="):].split(";", 1)[0]
        elif line.startswith("SSH_AGENT_PID="):
            try:
                pid = int(line[len("SSH_AGENT_PID="):].split(";", 1)[0])
            except ValueError:
                pass
    if sock is None or pid is None:
        raise GitContextError(
            f"could not parse ssh-agent output: {out!r}"
        )
    return sock, pid


class GitContextProvisioner:
    """Prepare / cleanup the git overlay for one task. One instance
    per scheduler — same lifetime as ``WorktreeManager``."""

    def __init__(
        self,
        *,
        ssh_keygen: str = "ssh-keygen",
        ssh_agent: str = "ssh-agent",
        ssh_add: str = "ssh-add",
        git: str = "git",
    ):
        self.ssh_keygen = ssh_keygen
        self.ssh_agent = ssh_agent
        self.ssh_add = ssh_add
        self.git = git

    async def prepare(
        self,
        *,
        task_id: str,
        worktree_dir: Path,
        git_context: GitContext,
    ) -> GitContextHandle:
        """Provision SSH key + agent, then clone + checkout in-place.

        On failure raises ``GitContextError`` — the scheduler treats
        this the same as any other task setup failure (lease released,
        task marked failed, owner can re-dispatch after the
        underlying issue is fixed).
        """
        ssh_dir = worktree_dir / ".ssh"
        ssh_dir.mkdir(mode=0o700, exist_ok=True)
        try:
            ssh_dir.chmod(0o700)
        except OSError:
            pass

        priv = ssh_dir / "id_ed25519"
        pub = ssh_dir / "id_ed25519.pub"
        if not priv.exists():
            rc, _out, err = await _run([
                self.ssh_keygen, "-t", "ed25519", "-N", "",
                "-f", str(priv),
                "-C", f"lyre/task-{task_id}",
                "-q",
            ])
            if rc != 0:
                raise GitContextError(
                    f"ssh-keygen failed (rc={rc}): "
                    f"{err.decode('utf-8', errors='replace')}"
                )
            try:
                priv.chmod(0o600)
            except OSError:
                pass

        pub_text = pub.read_text(encoding="utf-8").strip()

        # Start a fresh agent (the old one died with the previous
        # process — its SOCK path is gone).
        rc, out, err = await _run([self.ssh_agent, "-s"])
        if rc != 0:
            raise GitContextError(
                f"ssh-agent failed (rc={rc}): "
                f"{err.decode('utf-8', errors='replace')}"
            )
        sock, pid = _parse_ssh_agent_output(out)

        agent_env = {
            **os.environ,
            "SSH_AUTH_SOCK": sock,
            "SSH_AGENT_PID": str(pid),
        }
        rc, _out, err = await _run([self.ssh_add, str(priv)], env=agent_env)
        if rc != 0:
            self._kill_agent(pid)
            raise GitContextError(
                f"ssh-add failed (rc={rc}): "
                f"{err.decode('utf-8', errors='replace')}"
            )

        # Clone into worktree_dir. We pass --branch base_branch on
        # clone so the initial fetch only pulls what we need, then
        # ``git switch -c target_branch`` to put the worker on the
        # task's working branch.
        #
        # Cloning INTO an existing dir is allowed when the dir is
        # empty modulo .ssh/. ``git clone <url> .`` from inside the
        # worktree handles that.
        rc, _out, err = await _run(
            [
                self.git, "clone",
                "--branch", git_context.base_branch,
                "--single-branch",
                git_context.repo_url, ".",
            ],
            cwd=worktree_dir,
            env=agent_env,
        )
        if rc != 0:
            self._kill_agent(pid)
            raise GitContextError(
                f"git clone failed (rc={rc}): "
                f"{err.decode('utf-8', errors='replace')}"
            )

        rc, _out, err = await _run(
            [self.git, "switch", "-c", git_context.target_branch],
            cwd=worktree_dir,
            env=agent_env,
        )
        if rc != 0:
            self._kill_agent(pid)
            raise GitContextError(
                f"git switch -c {git_context.target_branch} failed "
                f"(rc={rc}): "
                f"{err.decode('utf-8', errors='replace')}"
            )

        log.info(
            "git_context_provisioned",
            task_id=task_id,
            repo_url=git_context.repo_url,
            base_branch=git_context.base_branch,
            target_branch=git_context.target_branch,
            ssh_agent_pid=pid,
        )
        return GitContextHandle(
            task_id=task_id,
            worktree_dir=worktree_dir,
            repo_url=git_context.repo_url,
            target_branch=git_context.target_branch,
            base_branch=git_context.base_branch,
            ssh_priv_key_path=priv,
            ssh_pub_key=pub_text,
            ssh_auth_sock=sock,
            ssh_agent_pid=pid,
        )

    async def cleanup(self, handle: GitContextHandle) -> None:
        """Kill the SSH agent. Worktree dir removal is the
        WorktreeManager's job — kept separate so a non-git task path
        doesn't need to know GitContext exists."""
        self._kill_agent(handle.ssh_agent_pid)
        log.info(
            "git_context_cleaned",
            task_id=handle.task_id,
            ssh_agent_pid=handle.ssh_agent_pid,
        )

    @staticmethod
    def _kill_agent(pid: int) -> None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except PermissionError as exc:
            log.warning("git_context_agent_kill_perm", pid=pid, error=str(exc))
