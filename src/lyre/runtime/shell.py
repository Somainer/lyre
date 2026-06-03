"""Asyncio subprocess.exec wrapper used by the shell_exec tool.

Sprint 1 surface: run a command in a worktree directory with a timeout, capture
stdout/stderr/exit_code, return a structured dict to the LLM.

Security is layered on top via ToolContext.extras['worktree'] (cwd jail), env
allowlist (no inherited LYRE_*/ANTHROPIC_* secrets), and the upcoming per-task
ephemeral SSH key. Today's MVP scope:
  - subprocess.exec (no shell=True, args list only OR `argv`)
  - cwd defaults to ToolContext.extras['worktree'] when present
  - env: starts from a small allowlist (PATH, HOME, USER, LANG, LC_*, TERM,
    GH_TOKEN, GIT_*, SSH_AUTH_SOCK) plus optional 'extra_env' from the tool call
  - timeout: default 60s, max 600s
  - output: stdout/stderr truncated to 100 KB each so we don't blow up the
    LLM context
"""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
import time
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()

_MAX_BYTES_PER_STREAM = 100 * 1024  # 100 KB
_DEFAULT_TIMEOUT_S = 60.0
_MAX_TIMEOUT_S = 600.0
# Cap the post-kill pipe drain so a grandchild that inherited the
# stdout/stderr write-end can never hang the wakeup.
_POST_KILL_DRAIN_S = 5.0

# Whitelist of env vars we forward into the subprocess. Anything else is dropped.
_ENV_ALLOWLIST = frozenset(
    [
        "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
        "TERM", "TZ", "SHELL",
        # Git / SSH / GH
        "SSH_AUTH_SOCK", "SSH_AGENT_PID",
        "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
        "GH_TOKEN", "GITHUB_TOKEN",
    ]
)


def _filter_env(extra: dict[str, str] | None) -> dict[str, str]:
    base = {k: v for k, v in os.environ.items() if k in _ENV_ALLOWLIST}
    if extra:
        for k, v in extra.items():
            if not isinstance(k, str) or not isinstance(v, str):
                continue
            # Caller-supplied extra_env is trusted and overlaid as-is. The secret
            # barrier is the _ENV_ALLOWLIST filter on os.environ above (which drops
            # ANTHROPIC_*/LYRE_* etc.), not this branch — extra_env intentionally
            # has no key restriction.
            base[k] = v
    return base


def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.pid is None:
        return
    # POSIX: signal the session/process group created by start_new_session.
    killpg = getattr(os, "killpg", None)
    getpgid = getattr(os, "getpgid", None)
    if killpg is not None and getpgid is not None:
        try:
            os.killpg(getpgid(proc.pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    # Fallback (non-POSIX or killpg failed): kill just the leader.
    try:
        proc.kill()
    except ProcessLookupError:
        pass


def _truncate(b: bytes) -> tuple[str, bool]:
    if len(b) <= _MAX_BYTES_PER_STREAM:
        return b.decode("utf-8", errors="replace"), False
    head = b[:_MAX_BYTES_PER_STREAM]
    return (
        head.decode("utf-8", errors="replace")
        + f"\n... [truncated {len(b) - _MAX_BYTES_PER_STREAM} bytes]",
        True,
    )


async def run_command(
    argv: list[str],
    cwd: Path | str | None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    if not argv or not isinstance(argv, list) or any(not isinstance(a, str) for a in argv):
        raise ValueError("argv must be a non-empty list of strings")
    if timeout_s <= 0 or timeout_s > _MAX_TIMEOUT_S:
        timeout_s = min(max(timeout_s, 0.1), _MAX_TIMEOUT_S)

    env = _filter_env(extra_env)
    started = time.time()
    cwd_str = str(cwd) if cwd else None

    log.info("shell_exec_start", argv=argv, cwd=cwd_str, timeout_s=timeout_s)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd_str,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # own process group -> killpg on timeout reaps grandchildren
        )
    except FileNotFoundError as exc:
        return {
            "exit_code": -1,
            "timed_out": False,
            "stdout": "",
            "stderr": f"command not found: {argv[0]} ({exc})",
            "stdout_truncated": False,
            "stderr_truncated": False,
            "wall_ms": 0,
            "argv": argv,
        }

    timed_out = False
    try:
        out_bytes, err_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_s
        )
    except TimeoutError:
        timed_out = True
        # Kill the whole process group, not just the leader: a grandchild that
        # inherited the stdout/stderr pipe write-end would otherwise keep the
        # second communicate() blocked indefinitely (e.g. ssh spawned by git push).
        _kill_process_tree(proc)
        try:
            out_bytes, err_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=_POST_KILL_DRAIN_S
            )
        except TimeoutError:
            # A descendant still holds the pipe open past the drain budget.
            # Give up on output rather than pin the single-threaded scheduler.
            out_bytes, err_bytes = b"", b""

    exit_code = proc.returncode if proc.returncode is not None else -1
    wall_ms = int((time.time() - started) * 1000)
    stdout, stdout_trunc = _truncate(out_bytes or b"")
    stderr, stderr_trunc = _truncate(err_bytes or b"")
    log.info(
        "shell_exec_done",
        argv=argv,
        exit_code=exit_code,
        timed_out=timed_out,
        wall_ms=wall_ms,
        stdout_bytes=len(out_bytes or b""),
        stderr_bytes=len(err_bytes or b""),
    )
    return {
        "exit_code": exit_code,
        "timed_out": timed_out,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": stdout_trunc,
        "stderr_truncated": stderr_trunc,
        "wall_ms": wall_ms,
        "argv": argv,
    }


def split_command(text: str) -> list[str]:
    """Convenience for tests / CLI: shlex-split a string into argv."""
    return shlex.split(text)
