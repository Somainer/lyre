"""Tests for the shell_exec tool + the underlying run_command."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from lyre.persistence.models import Persona, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime import shell as shell_mod
from lyre.runtime.tools import ToolContext, ToolError
from lyre.runtime.tools.shell import SHELL_EXEC


@pytest.fixture
async def shell_ctx(
    repos: SqliteRepositories, tmp_path: Path
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
        repos=repos,
        task_id=task_id,
        wakeup_id=wakeup_id,
        persona_name="worker",
        extras={"worktree": str(worktree)},
    )


@pytest.mark.asyncio
async def test_run_command_captures_stdout(tmp_path: Path) -> None:
    res = await shell_mod.run_command(
        [sys.executable, "-c", "print('hi from py')"],
        cwd=tmp_path,
    )
    assert res["exit_code"] == 0
    assert "hi from py" in res["stdout"]
    assert res["timed_out"] is False


@pytest.mark.asyncio
async def test_run_command_captures_nonzero_exit(tmp_path: Path) -> None:
    res = await shell_mod.run_command(
        [sys.executable, "-c", "import sys; sys.exit(7)"],
        cwd=tmp_path,
    )
    assert res["exit_code"] == 7


@pytest.mark.asyncio
async def test_run_command_truncates_huge_output(tmp_path: Path) -> None:
    # Print 200 KB; expect truncation marker.
    script = "import sys; sys.stdout.write('x' * (200*1024))"
    res = await shell_mod.run_command(
        [sys.executable, "-c", script], cwd=tmp_path
    )
    assert res["stdout_truncated"] is True
    assert "truncated" in res["stdout"]


@pytest.mark.asyncio
async def test_run_command_times_out(tmp_path: Path) -> None:
    res = await shell_mod.run_command(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        cwd=tmp_path,
        timeout_s=0.3,
    )
    assert res["timed_out"] is True


@pytest.mark.asyncio
async def test_run_command_missing_binary_returns_error(tmp_path: Path) -> None:
    res = await shell_mod.run_command(
        ["definitely-not-a-real-binary-xyz"], cwd=tmp_path
    )
    assert res["exit_code"] == -1
    assert "not found" in res["stderr"]


@pytest.mark.asyncio
async def test_shell_exec_tool_uses_worktree_as_cwd(shell_ctx: ToolContext) -> None:
    res = await SHELL_EXEC.handler(
        shell_ctx,
        {"argv": [sys.executable, "-c", "import os; print(os.getcwd())"]},
    )
    assert res["exit_code"] == 0
    assert shell_ctx.extras["worktree"] in res["stdout"]


@pytest.mark.asyncio
async def test_shell_exec_accepts_cwd_outside_worktree(
    shell_ctx: ToolContext,
) -> None:
    """No cwd jail (per 铁律 2): agents can cd anywhere their OS user can.
    Isolation is OS-level (docker), not a fence at the tool layer."""
    res = await SHELL_EXEC.handler(
        shell_ctx,
        {"argv": [sys.executable, "-c", "import os; print(os.getcwd())"],
         "cwd": "/tmp"},
    )
    assert res["exit_code"] == 0
    assert "/tmp" in res["stdout"]


@pytest.mark.asyncio
async def test_shell_exec_accepts_command_string(shell_ctx: ToolContext) -> None:
    res = await SHELL_EXEC.handler(
        shell_ctx,
        {"command": f"{sys.executable} -c 'print(1+1)'"},
    )
    assert res["exit_code"] == 0
    assert "2" in res["stdout"]


@pytest.mark.asyncio
async def test_shell_exec_rejects_empty_argv(shell_ctx: ToolContext) -> None:
    with pytest.raises(ToolError):
        await SHELL_EXEC.handler(shell_ctx, {})


@pytest.mark.asyncio
async def test_filter_env_blocks_secrets(shell_ctx: ToolContext, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-leak")
    monkeypatch.setenv("LYRE_DB_PATH", "/x")
    res = await SHELL_EXEC.handler(
        shell_ctx,
        {
            "argv": [
                sys.executable, "-c",
                "import os; print('A=', os.environ.get('ANTHROPIC_API_KEY'));"
                " print('L=', os.environ.get('LYRE_DB_PATH'))",
            ],
        },
    )
    assert res["exit_code"] == 0
    assert "A= None" in res["stdout"]
    assert "L= None" in res["stdout"]
