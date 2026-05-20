"""Per-task worktree + ephemeral SSH key lifecycle.

Sprint 1 piece. Each task that needs to act on a real repo gets:
  - A clean tmpdir under `object_store/worktrees/{task_id}/`
  - A freshly-generated ed25519 SSH keypair under `.ssh/` inside the tmpdir
  - A dedicated `ssh-agent` process holding that key in memory
  - `SSH_AUTH_SOCK` + `SSH_AGENT_PID` exported via ToolContext.extras["env_overlay"],
    which shell_exec merges into every subprocess env

Cleanup on task success: kill the agent + rm -rf the worktree. On failure we
keep the dir for postmortem (Q5 says state must be reconstructable; remote-side
git state is the source of truth for retry, so local can be discarded on
success).

GitHub-side public key registration / deregistration is **not** done here yet
(Sprint 1 plan §5.2.3); we keep the keypair local-only so end-to-end tests can
hit `file://` bare repos without external infra.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
from dataclasses import dataclass
from pathlib import Path

import structlog

log = structlog.get_logger()


@dataclass(frozen=True)
class WorktreeHandle:
    task_id: str
    dir: Path
    ssh_priv_key_path: Path
    ssh_pub_key: str  # the contents of id_ed25519.pub (single line, trailing newline trimmed)
    ssh_auth_sock: str
    ssh_agent_pid: int

    def env_overlay(self) -> dict[str, str]:
        """Env vars to merge into shell_exec subprocesses for this task."""
        return {
            "SSH_AUTH_SOCK": self.ssh_auth_sock,
            "SSH_AGENT_PID": str(self.ssh_agent_pid),
        }


class WorktreeError(RuntimeError):
    """Raised when worktree preparation fails."""


async def _run(argv: list[str], env: dict[str, str] | None = None) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env if env is not None else os.environ.copy(),
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out, err


def _parse_ssh_agent_output(out: bytes) -> tuple[str, int]:
    """ssh-agent -s prints lines like:
        SSH_AUTH_SOCK=/tmp/ssh-XXX/agent.123; export SSH_AUTH_SOCK;
        SSH_AGENT_PID=124; export SSH_AGENT_PID;
        echo Agent pid 124;
    Parse the SOCK + PID out.
    """
    sock: str | None = None
    pid: int | None = None
    for raw in out.decode("utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line.startswith("SSH_AUTH_SOCK="):
            value = line[len("SSH_AUTH_SOCK="):]
            # strip "; export SSH_AUTH_SOCK;" suffix
            value = value.split(";", 1)[0]
            sock = value
        elif line.startswith("SSH_AGENT_PID="):
            value = line[len("SSH_AGENT_PID="):]
            value = value.split(";", 1)[0]
            try:
                pid = int(value)
            except ValueError:
                pass
    if sock is None or pid is None:
        raise WorktreeError(
            f"could not parse ssh-agent output: {out!r}"
        )
    return sock, pid


class WorktreeManager:
    """Owns the prepare/cleanup lifecycle. One instance per scheduler."""

    def __init__(
        self,
        root: Path,
        ssh_keygen: str = "ssh-keygen",
        ssh_agent: str = "ssh-agent",
        ssh_add: str = "ssh-add",
    ):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.ssh_keygen = ssh_keygen
        self.ssh_agent = ssh_agent
        self.ssh_add = ssh_add

    async def prepare(self, task_id: str) -> WorktreeHandle:
        """Make tmpdir + ed25519 keypair + ssh-agent. Idempotent: re-running
        for the same task_id reuses existing keypair (Q5: kill-restart safety)
        but always starts a fresh agent (the old one died with the previous
        process)."""
        wd = self.root / task_id
        wd.mkdir(parents=True, exist_ok=True)
        ssh_dir = wd / ".ssh"
        ssh_dir.mkdir(mode=0o700, exist_ok=True)
        # Tighten perms in case the dir already existed with wider mode.
        try:
            ssh_dir.chmod(0o700)
        except OSError:
            pass

        priv = ssh_dir / "id_ed25519"
        pub = ssh_dir / "id_ed25519.pub"

        if not priv.exists():
            rc, _out, err = await _run(
                [
                    self.ssh_keygen,
                    "-t", "ed25519",
                    "-N", "",
                    "-f", str(priv),
                    "-C", f"lyre/task-{task_id}",
                    "-q",
                ]
            )
            if rc != 0:
                raise WorktreeError(
                    f"ssh-keygen failed (rc={rc}): {err.decode('utf-8', errors='replace')}"
                )
            try:
                priv.chmod(0o600)
            except OSError:
                pass

        pub_text = pub.read_text(encoding="utf-8").strip()

        # Start a fresh agent for this task — even if a stale one was running
        # from a previous wakeup, its sock path is in the parent process which
        # has died.
        rc, out, err = await _run([self.ssh_agent, "-s"])
        if rc != 0:
            raise WorktreeError(
                f"ssh-agent failed (rc={rc}): {err.decode('utf-8', errors='replace')}"
            )
        sock, pid = _parse_ssh_agent_output(out)

        # Load the key into the agent.
        agent_env = {**os.environ, "SSH_AUTH_SOCK": sock, "SSH_AGENT_PID": str(pid)}
        rc, _out, err = await _run([self.ssh_add, str(priv)], env=agent_env)
        if rc != 0:
            # Best-effort kill of the orphaned agent before raising.
            self._kill_agent(pid)
            raise WorktreeError(
                f"ssh-add failed (rc={rc}): {err.decode('utf-8', errors='replace')}"
            )

        log.info(
            "worktree_prepared",
            task_id=task_id,
            dir=str(wd),
            ssh_agent_pid=pid,
        )
        return WorktreeHandle(
            task_id=task_id,
            dir=wd,
            ssh_priv_key_path=priv,
            ssh_pub_key=pub_text,
            ssh_auth_sock=sock,
            ssh_agent_pid=pid,
        )

    async def cleanup(
        self,
        handle: WorktreeHandle,
        *,
        remove_dir: bool = True,
    ) -> None:
        """Kill agent + optionally remove tmpdir. `remove_dir=False` is used
        on task failure so a human / next wakeup can inspect what happened."""
        self._kill_agent(handle.ssh_agent_pid)
        if remove_dir:
            shutil.rmtree(handle.dir, ignore_errors=True)
        log.info(
            "worktree_cleaned",
            task_id=handle.task_id,
            removed_dir=remove_dir,
            ssh_agent_pid=handle.ssh_agent_pid,
        )

    @staticmethod
    def _kill_agent(pid: int) -> None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except PermissionError as exc:
            log.warning("worktree_agent_kill_perm", pid=pid, error=str(exc))
