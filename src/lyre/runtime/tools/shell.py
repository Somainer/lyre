"""shell_exec tool — agent's interface to host commands.

Cwd defaults to ToolContext.extras['worktree'] (set by scheduler for
every wakeup — every LLM persona gets a sandbox tmpdir now). If the
task was dispatched with a ``git_context``, the worktree is already a
checked-out git working copy with an active SSH agent; otherwise it's
an empty tmpdir. Either way, ``cwd`` defaults to that tmpdir.

Read-only PATH inheritance, no shell expansion.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .. import shell
from . import Tool, ToolContext, ToolError


def _resolve_credentials(ctx: ToolContext, name: str) -> dict[str, str]:
    """Resolve a configured coding-backend bundle to ``{auth_env: secret}`` for
    injection into the subprocess. The secret is read SERVER-SIDE from the
    runtime's env — the agent only names the bundle, never sees the value. See
    docs/design/CAPABILITY_DISCOVERY.md.
    """
    backends = ctx.extras.get("coding_backends") or {}
    bundle = backends.get(name)
    if bundle is None:
        raise ToolError(
            f"unknown credentials bundle {name!r}. Declare it under "
            f"[coding_backends.{name}] in config.toml (auth_env = <ENV VAR>)."
        )
    allowed = bundle.allowed_personas
    if allowed and ctx.persona_name not in allowed:
        raise ToolError(
            f"persona {ctx.persona_name!r} is not allowed to use the {name!r} "
            f"credentials bundle (allowed: {list(allowed)})."
        )
    secret = os.environ.get(bundle.auth_env)
    if not secret:
        raise ToolError(
            f"credentials bundle {name!r} maps to env {bundle.auth_env!r}, "
            f"which is not set — provision it in ~/.lyre/.env."
        )
    return {bundle.auth_env: secret}


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

    # Coding-backend credential opt-in: inject one owner-declared secret into
    # this subprocess so a discovered coding-agent skill can authenticate. The
    # value is read server-side and never returned to the agent. Layered last
    # so a configured bundle wins over any same-named user_env key.
    credentials = args.get("credentials")
    if credentials is not None:
        if not isinstance(credentials, str):
            raise ToolError("credentials must be a string (a coding-backend name)")
        extra_env = {**extra_env, **_resolve_credentials(ctx, credentials)}

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
        "single string and Lyre will shlex-split it. Output truncated at 100 KB. "
        "Pass `credentials=<backend-name>` to inject an owner-declared external "
        "coding-agent key (e.g. to run `codex`/`claude` headless) into just this "
        "call — see a coding-agent skill for the exact recipe."
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
            "credentials": {
                "type": "string",
                "description": (
                    "Name of an owner-configured coding-backend bundle "
                    "([coding_backends.<name>] in config.toml). Injects that "
                    "backend's API key into this one subprocess so an external "
                    "coding agent can authenticate. Omit for ordinary commands."
                ),
            },
        },
    },
    handler=_shell_exec,
)
