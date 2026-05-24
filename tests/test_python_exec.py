"""Tests for the python_exec tool — Lyre's first-class execution path."""

from __future__ import annotations

from pathlib import Path

import pytest

from lyre.persistence.models import Persona, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.tools import ToolContext, ToolError
from lyre.runtime.tools.builtin import build_default_registry
from lyre.runtime.tools.python import PYTHON_EXEC


@pytest.fixture
async def py_ctx(
    repos: SqliteRepositories, tmp_path: Path,
) -> ToolContext:
    await repos.personas.upsert(
        Persona(name="worker", role_description="w", system_prompt="w")
    )
    task_id = await repos.tasks.create(
        TaskSpec(persona_name="worker", goal="g", acceptance="a")
    )
    wakeup_id = await repos.wakeups.start(task_id, "worker")
    await repos.tasks.claim_lease(task_id, wakeup_id, duration_sec=600)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    return ToolContext(
        repos=repos, task_id=task_id, wakeup_id=wakeup_id,
        persona_name="worker",
        extras={"worktree": str(worktree)},
    )


@pytest.mark.asyncio
async def test_python_exec_runs_simple_script(py_ctx: ToolContext) -> None:
    res = await PYTHON_EXEC.handler(
        py_ctx, {"code": "print('hello from python_exec')", "_tool_use_id": "t1"},
    )
    assert res["exit_code"] == 0
    assert "hello from python_exec" in res["stdout"]
    assert res["script_path"].endswith("py_t1.py")


@pytest.mark.asyncio
async def test_python_exec_multiline_with_imports(py_ctx: ToolContext) -> None:
    code = """
import json
import sys

data = {"answer": 42, "list": [1, 2, 3]}
print(json.dumps(data))
print("py", sys.version_info[:2])
"""
    res = await PYTHON_EXEC.handler(py_ctx, {"code": code, "_tool_use_id": "t2"})
    assert res["exit_code"] == 0
    assert '"answer": 42' in res["stdout"]
    assert "py (3," in res["stdout"]


@pytest.mark.asyncio
async def test_python_exec_can_edit_files_in_worktree(py_ctx: ToolContext) -> None:
    """This is THE motivating use case — file edits should feel native, no
    shell quoting headaches."""
    worktree = Path(py_ctx.extras["worktree"])
    (worktree / "README.md").write_text("hello\n", encoding="utf-8")

    code = """
import pathlib
p = pathlib.Path("README.md")
p.write_text(p.read_text() + "managed by lyre\\n")
print("ok")
"""
    res = await PYTHON_EXEC.handler(py_ctx, {"code": code, "_tool_use_id": "t3"})
    assert res["exit_code"] == 0
    assert "ok" in res["stdout"]
    assert (worktree / "README.md").read_text() == "hello\nmanaged by lyre\n"


@pytest.mark.asyncio
async def test_python_exec_captures_stderr_and_nonzero_exit(
    py_ctx: ToolContext,
) -> None:
    code = """
import sys
sys.stderr.write("boom\\n")
sys.exit(7)
"""
    res = await PYTHON_EXEC.handler(py_ctx, {"code": code, "_tool_use_id": "t4"})
    assert res["exit_code"] == 7
    assert "boom" in res["stderr"]


@pytest.mark.asyncio
async def test_python_exec_syntax_error_surfaces_cleanly(
    py_ctx: ToolContext,
) -> None:
    res = await PYTHON_EXEC.handler(
        py_ctx, {"code": "def bad(:\n  pass", "_tool_use_id": "t5"},
    )
    assert res["exit_code"] != 0
    assert "SyntaxError" in res["stderr"] or "invalid syntax" in res["stderr"]


@pytest.mark.asyncio
async def test_python_exec_respects_timeout(py_ctx: ToolContext) -> None:
    code = "import time; time.sleep(5)"
    res = await PYTHON_EXEC.handler(
        py_ctx, {"code": code, "timeout_s": 0.3, "_tool_use_id": "t6"},
    )
    assert res["timed_out"] is True


@pytest.mark.asyncio
async def test_python_exec_rejects_empty_code(py_ctx: ToolContext) -> None:
    with pytest.raises(ToolError):
        await PYTHON_EXEC.handler(py_ctx, {"_tool_use_id": "t7"})
    with pytest.raises(ToolError):
        await PYTHON_EXEC.handler(py_ctx, {"code": "", "_tool_use_id": "t8"})


@pytest.mark.asyncio
async def test_python_exec_env_overlay_propagates(py_ctx: ToolContext) -> None:
    """If WorktreeManager set env_overlay, python_exec's subprocess sees it
    (proves SSH_AUTH_SOCK and friends reach Python scripts that shell out
    to git)."""
    py_ctx.extras["env_overlay"] = {"FAKE_TOKEN": "abc123"}
    code = """
import os
print("token:", os.environ.get("FAKE_TOKEN"))
"""
    res = await PYTHON_EXEC.handler(py_ctx, {"code": code, "_tool_use_id": "t9"})
    assert res["exit_code"] == 0
    assert "token: abc123" in res["stdout"]


@pytest.mark.asyncio
async def test_python_exec_uses_worktree_as_cwd(py_ctx: ToolContext) -> None:
    code = "import os; print(os.getcwd())"
    res = await PYTHON_EXEC.handler(py_ctx, {"code": code, "_tool_use_id": "t10"})
    assert res["exit_code"] == 0
    assert py_ctx.extras["worktree"] in res["stdout"]


@pytest.mark.asyncio
async def test_python_exec_script_written_to_worktree_scripts_dir(
    py_ctx: ToolContext,
) -> None:
    res = await PYTHON_EXEC.handler(
        py_ctx, {"code": "print(1)", "_tool_use_id": "tu_xyz"},
    )
    p = Path(res["script_path"])
    assert p.exists()
    assert "/.lyre/scripts/py_tu_xyz.py" in str(p)
    assert p.read_text() == "print(1)"


@pytest.mark.asyncio
async def test_python_exec_retry_overwrites_same_script_file(
    py_ctx: ToolContext,
) -> None:
    """Idempotency: same tool_use_id → same script path → re-running just
    overwrites. No accumulation of stale scripts on retry."""
    await PYTHON_EXEC.handler(
        py_ctx, {"code": "print('v1')", "_tool_use_id": "same"},
    )
    res2 = await PYTHON_EXEC.handler(
        py_ctx, {"code": "print('v2')", "_tool_use_id": "same"},
    )
    assert "v2" in res2["stdout"]
    p = Path(res2["script_path"])
    assert p.read_text() == "print('v2')"


def test_python_exec_registered_in_default_registry() -> None:
    reg = build_default_registry()
    assert "python_exec" in reg.all_names()
    # Worker-maintainer's allowlist puts python_exec BEFORE shell_exec —
    # not enforced by code, but a doc check we can run via persona seed.


@pytest.mark.asyncio
async def test_worker_maintainer_persona_lists_python_exec_first(
    repos: SqliteRepositories,
) -> None:
    from lyre.persistence.fs_personas import FilesystemPersonaRepository
    from lyre.personas.seed import ensure_user_personas
    assert isinstance(repos.personas, FilesystemPersonaRepository)
    ensure_user_personas(repos.personas.personas_dir)
    worker = await repos.personas.get("worker-maintainer")
    assert worker is not None
    tools = worker.allowed_lyre_tools
    assert "python_exec" in tools
    assert "shell_exec" in tools
    assert tools.index("python_exec") < tools.index("shell_exec"), (
        "python_exec should appear before shell_exec to signal preference"
    )
