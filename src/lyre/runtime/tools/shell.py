"""shell_exec tool — agent's interface to host commands.

Cwd defaults to ToolContext.extras['worktree'] (set by scheduler for
every wakeup — every LLM persona gets a sandbox tmpdir now). If the
task was dispatched with a ``git_context``, the worktree is already a
checked-out git working copy with an active SSH agent; otherwise it's
an empty tmpdir. Either way, ``cwd`` defaults to that tmpdir.

Read-only PATH inheritance, no shell expansion.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import shell
from . import Tool, ToolContext, ToolError


async def _shell_exec(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    argv = args.get("argv")
    cmd_string = args.get("command")
    if not argv and cmd_string and isinstance(cmd_string, str):
        argv = shell.split_command(cmd_string)
    if not isinstance(argv, list) or not argv:
        raise ToolError("provide either 'argv' (list) or 'command' (string)")
    if any(not isinstance(a, str) for a in argv):
        raise ToolError("argv items must all be strings")

    timeout_s = float(args.get("timeout_s", 60.0))
    user_env = args.get("env")
    if user_env is not None and not isinstance(user_env, dict):
        raise ToolError("env must be an object of string→string")

    # Per-task overlay (e.g. SSH_AUTH_SOCK/SSH_AGENT_PID from WorktreeManager)
    # is applied beneath any user-supplied env, so worktree-scoped credentials
    # propagate by default.
    overlay = ctx.extras.get("env_overlay") or {}
    if not isinstance(overlay, dict):
        overlay = {}
    extra_env: dict[str, str] = {**overlay, **(user_env or {})}

    requested_cwd = args.get("cwd")
    if requested_cwd is not None and not isinstance(requested_cwd, str):
        raise ToolError("cwd must be a string path")

    # No cwd jail — per 铁律 2 (FOUNDATION §3.7) the agent subprocess has
    # full shell freedom inside its process. Isolation is OS-level (e.g.
    # `docker run lyre`), not a fake fence at the tool layer. The agent's
    # persona prompt + Tier matrix + reviewer audit is the social mechanism
    # that keeps writes outside the worktree intentional.
    worktree = ctx.extras.get("worktree")
    if requested_cwd:
        cwd: Path | None = Path(requested_cwd).resolve()
    else:
        cwd = Path(worktree).resolve() if worktree else None

    return await shell.run_command(
        argv=argv,
        cwd=cwd,
        timeout_s=timeout_s,
        extra_env=extra_env,
    )


SHELL_EXEC = Tool(
    name="shell_exec",
    description=(
        "Execute a host command (git, gh, sbt, ls, cat, etc.) and capture its "
        "stdout/stderr/exit_code. Runs inside the task's worktree directory by "
        "default. No shell expansion — pass argv as a list, or 'command' as a "
        "single string and Lyre will shlex-split it. Output truncated at 100 KB."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "argv": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Command as argv list (preferred). E.g. ['git','status'].",
            },
            "command": {
                "type": "string",
                "description": "Alternative to argv: a shell-quoted string. Will be shlex-split.",
            },
            "cwd": {
                "type": "string",
                "description": "Optional subdir under the worktree; must not escape the worktree.",
            },
            "timeout_s": {
                "type": "number",
                "default": 60,
                "minimum": 1,
                "maximum": 600,
            },
            "env": {
                "type": "object",
                "description": "Extra env vars merged onto the allowlist.",
            },
        },
    },
    handler=_shell_exec,
)
