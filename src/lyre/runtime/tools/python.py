"""python_exec — Lyre's first-class execution tool.

Design intent (per Owner, 2026-05-17):
  Python is the preferred way for agents to do file edits, data shaping, JSON
  manipulation, ad-hoc logic, anything you'd write a small script for. Reserve
  `shell_exec` for invoking specific binaries (git, gh, sbt, make, etc.).

Implementation:
  - Agent supplies the source as a `code` string (multi-line OK)
  - Lyre writes it to `<worktree>/.lyre/scripts/py_<tool_use_id>.py`
    (deterministic name so retries overwrite the same file → idempotent + the
    file stays around for postmortem if the wakeup fails)
  - Runs `sys.executable <script>` so the interpreter is Lyre's own venv
    Python (with anthropic SDK etc. on path); agent can override via
    `interpreter` arg if a project-specific Python is needed
  - All other plumbing (env allowlist, env_overlay merge, cwd, timeout,
    output truncation) goes through the same `shell.run_command` as
    `shell_exec` — one place to harden, one place to debug
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

from .. import shell
from . import Tool, ToolContext, ToolError

_DEFAULT_TIMEOUT_S = 60.0


async def _python_exec(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    code = args.get("code")
    if not code or not isinstance(code, str):
        raise ToolError("provide 'code' (a Python source string)")

    timeout_s = float(args.get("timeout_s", _DEFAULT_TIMEOUT_S))
    user_env = args.get("env")
    if user_env is not None and not isinstance(user_env, dict):
        raise ToolError("env must be an object of string→string")

    overlay = ctx.extras.get("env_overlay") or {}
    if not isinstance(overlay, dict):
        overlay = {}
    extra_env: dict[str, str] = {**overlay, **(user_env or {})}

    requested_cwd = args.get("cwd")
    if requested_cwd is not None and not isinstance(requested_cwd, str):
        raise ToolError("cwd must be a string path")

    worktree = ctx.extras.get("worktree")
    if requested_cwd:
        cwd: Path | None = Path(requested_cwd).resolve()
    else:
        cwd = Path(worktree).resolve() if worktree else None

    tool_use_id = args.get("_tool_use_id") or "anonymous"
    if worktree:
        scripts_dir = Path(worktree) / ".lyre" / "scripts"
    else:
        scripts_dir = Path(tempfile.gettempdir()) / "lyre-scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    script_path = scripts_dir / f"py_{tool_use_id}.py"
    script_path.write_text(code, encoding="utf-8")

    interpreter = args.get("interpreter") or sys.executable

    result = await shell.run_command(
        argv=[interpreter, str(script_path)],
        cwd=cwd,
        timeout_s=timeout_s,
        extra_env=extra_env,
    )
    result["script_path"] = str(script_path)
    return result


PYTHON_EXEC = Tool(
    name="python_exec",
    description=(
        "PREFERRED execution tool — use this BEFORE reaching for shell_exec. "
        "Run a Python script and capture stdout/stderr/exit_code. Good for: "
        "file edits, JSON/YAML/text manipulation, data shaping, ad-hoc "
        "logic, anything you'd write a small Python script for. Multi-line "
        "code is fine — the whole `code` string is written to a file and "
        "executed. Use shell_exec only when invoking a specific binary "
        "(git, gh, sbt, make, etc.)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": (
                    "Python source. Multi-line is fine. Use stdlib freely; "
                    "Lyre's own venv Python is the default interpreter so "
                    "common deps like pyyaml/structlog/httpx are available."
                ),
            },
            "cwd": {
                "type": "string",
                "description": "Working directory. Defaults to your worktree root.",
            },
            "timeout_s": {
                "type": "number",
                "default": 60,
                "minimum": 1,
                "maximum": 600,
            },
            "env": {
                "type": "object",
                "description": "Extra env vars merged on top of the task overlay.",
            },
            "interpreter": {
                "type": "string",
                "description": (
                    "Override interpreter path (e.g. a project's venv). "
                    "Default = Lyre's own sys.executable."
                ),
            },
        },
        "required": ["code"],
    },
    handler=_python_exec,
)
