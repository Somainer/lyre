"""Subprocess isolation tests (per 铁律 2 + AGENT_RUNTIME §3.5).

These run REAL child Python processes via the `lyre run-task <id>` CLI
against a shared SQLite DB. The Scheduler's subprocess-mode just spawns +
waits; everything else (lease, wakeup, agent loop, tools, outbox) happens
inside the subprocess against the same DB.

Covered:
  - Happy path: subprocess runs a scripted task to completion
  - Crash path: subprocess exits non-zero → task stays in_progress with lease
    held → in-process Scheduler then recovers via find_expired_leases
  - Real SIGKILL: subprocess kills itself mid-task → equivalent to the chaos
    test scenarios but with TRUE OS kill (no Python finally runs)

Tests are macOS / Linux only (`asyncio.create_subprocess_exec`); they should
work on both. Each test creates an isolated tmp DB + memory dir.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from lyre.config import Config
from lyre.persistence.db import init_db
from lyre.persistence.models import TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.personas.seed import seed_personas
from lyre.runtime.memory import ensure_skeleton
from lyre.scheduler.scheduler import Scheduler


@pytest.fixture
def sandbox(tmp_path: Path) -> Iterator[dict]:
    """Tmp DB + object_store + memory + script path, all isolated.

    Returns a dict of paths + an env-dict the subprocess should inherit so
    `Config.from_env()` finds the same DB.
    """
    db = tmp_path / "lyre.db"
    obj = tmp_path / "objstore"
    mem = tmp_path / "memory"
    obj.mkdir()
    ensure_skeleton(mem)
    env = {
        **os.environ,
        "LYRE_DB_PATH": str(db),
        "LYRE_OBJECT_STORE": str(obj),
        "LYRE_MEMORY_PATH": str(mem),
        # Subprocess Config.from_env() requires ANTHROPIC_API_KEY check to
        # NOT abort run-task; run-task doesn't actually verify it.
        "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", "fake-test-key"),
    }
    # Disable any project-level .env from leaking into the test subprocess.
    env.setdefault("LYRE_DOTENV_DISABLE", "1")  # cosmetic; not currently honored
    yield {
        "db": db,
        "obj": obj,
        "mem": mem,
        "env": env,
        "tmp": tmp_path,
    }


async def _seed(repos: SqliteRepositories) -> None:
    await seed_personas(repos.personas)
    await repos.mailbox.ensure_mailbox("owner")


def _write_jsonl_script(path: Path, turns: list[list[dict]]) -> None:
    """Each inner list is one turn's events."""
    lines = [json.dumps(t) for t in turns]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def _run_subprocess_task(
    env: dict, task_id: str, *, expect_returncode: int = 0,
) -> tuple[int, bytes, bytes]:
    """Spawn `lyre run-task <id>` exactly as the Scheduler would."""
    argv = [sys.executable, "-m", "lyre.main", "run-task", task_id]
    proc = await asyncio.create_subprocess_exec(
        *argv,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    rc = proc.returncode if proc.returncode is not None else -1
    return rc, stdout or b"", stderr or b""


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subprocess_runs_scripted_task_to_completion(
    sandbox: dict,
) -> None:
    conn = await init_db(sandbox["db"])
    try:
        repos = SqliteRepositories(conn)
        await _seed(repos)

        # Override worker-maintainer's worktree to off so we don't need
        # ssh-keygen in this minimal smoke. The persona is already seeded;
        # update its model_preference path is fine — we use a flagship-
        # tier wrap via the dispatcher path instead. Simpler: use the dispatcher.
        task_id = await repos.tasks.create(
            TaskSpec(
                persona_name="dispatcher",
                goal="say hi to owner",
                acceptance="message sent",
            )
        )

        # Script: one turn = mailbox_send + tool_use_complete; then end.
        # dispatcher is allowed mailbox_send.
        script_path = sandbox["tmp"] / "script.jsonl"
        _write_jsonl_script(script_path, [
            [
                {"type": "tool_use_complete", "id": "t1", "name": "mailbox_send",
                 "input": {"to": "owner", "body": "hi from subprocess",
                           "urgency": "normal"}},
                {"type": "turn_complete", "stop_reason": "tool_use"},
            ],
            [
                {"type": "content_delta", "text": "done"},
                {"type": "turn_complete", "stop_reason": "end_turn"},
            ],
        ])
        env = {**sandbox["env"], "LYRE_MOCK_ADAPTER_SCRIPT": str(script_path)}

        rc, stdout, stderr = await _run_subprocess_task(env, task_id)
        assert rc == 0, (
            f"subprocess exited {rc}\nstdout:{stdout!r}\nstderr:{stderr!r}"
        )

        # Verify side effects landed in the shared DB
        t = await repos.tasks.get(task_id)
        assert t is not None
        assert t.status == "completed"

        # Outbox row was written by the subprocess
        batch = await repos.outbox.dequeue_batch(limit=10)
        assert len(batch) == 1
        assert batch[0].kind == "mailbox_send"
        assert batch[0].payload["body"] == "hi from subprocess"
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Real SIGKILL chaos: subprocess self-kills mid-task; in-process scheduler
# then recovers it via the standard expired-lease path. This is the Q5
# chaos contract executed with REAL OS-level kill — no SimulatedKill trick.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subprocess_sigkill_mid_task_recovers_via_expired_lease(
    sandbox: dict,
) -> None:
    """Worker subprocess SIGKILLs itself mid-task (after claim_lease but
    before completion). Asserts:
      (a) subprocess exits non-zero
      (b) task left status=in_progress, lease still held (real `finally`
          did NOT run — that's the SIGKILL signature)
      (c) after lease expires, an inline scheduler tick picks it up and
          completes the task using a fresh FakeAdapter
    """
    conn = await init_db(sandbox["db"])
    try:
        repos = SqliteRepositories(conn)
        await _seed(repos)

        # Worker-maintainer normally needs a worktree (ssh-keygen +
        # ssh-agent). Override that for this isolation test — we're
        # testing subprocess lifecycle, not worktree.
        worker = await repos.personas.get("worker-maintainer")
        assert worker is not None
        await repos.personas.upsert(
            worker.model_copy(update={"needs_worktree": False})
        )

        task_id = await repos.tasks.create(
            TaskSpec(
                persona_name="worker-maintainer",
                goal="self-destruct mid-task",
                acceptance="lease released on recovery",
                lease_duration_s=0,
            )
        )

        # `$PPID` inside bash is the Python subprocess that launched bash.
        # `kill -9 $PPID` SIGKILLs the Python — no finally runs there.
        kill_script = sandbox["tmp"] / "script.jsonl"
        _write_jsonl_script(kill_script, [
            [
                {"type": "tool_use_complete", "id": "k1", "name": "shell_exec",
                 "input": {"argv": ["bash", "-c", "kill -9 $PPID"]}},
                {"type": "turn_complete", "stop_reason": "tool_use"},
            ],
        ])
        env = {**sandbox["env"], "LYRE_MOCK_ADAPTER_SCRIPT": str(kill_script)}

        rc, _, _ = await _run_subprocess_task(env, task_id)
        assert rc != 0, "subprocess should have been SIGKILL'd"

        # (a) Task left in_progress, lease held, NO finally ran.
        t = await repos.tasks.get(task_id)
        assert t is not None
        assert t.status == "in_progress", (
            f"expected in_progress (finally didn't run), got {t.status}"
        )
        assert t.lease_holder is not None

        # (b) Lease expires under SQLite second-resolution
        await asyncio.sleep(1.2)

        # (c) Inline scheduler recovers
        from lyre.adapter.llm_adapter import ContentDelta, TurnComplete
        from lyre.runtime.model_registry import (
            ModelCost,
            ModelEndpoint,
            ModelEntry,
            ModelRegistry,
        )
        from tests.fake_adapter import FakeAdapter

        entry = ModelEntry(
            id="fake.workhorse", provider="fake",
            # Use the conftest-provided FAKE_API_KEY env var so the
            # router's reachability filter (added when header-only auth
            # mode landed) sees this entry as authenticatable. The
            # actual auth doesn't matter — adapter_for_test below
            # substitutes a fake adapter — but the router runs first
            # and drops anything with no usable auth.
            endpoint=ModelEndpoint(None, "FAKE_API_KEY"),
            capabilities=("tool_use", "streaming"),
            tier="workhorse",
            cost_per_mtok=ModelCost(None, None),
        )
        registry = ModelRegistry(entries=[entry])
        fake = FakeAdapter()
        fake.push_turn([
            ContentDelta(text="recovered cleanly"),
            TurnComplete(stop_reason="end_turn"),
        ])
        cfg = Config(
            db_path=sandbox["db"],
            object_store_path=sandbox["obj"],
            memory_path=sandbox["mem"],
            anthropic_api_key="fake",
            anthropic_base_url=None,
            default_model="m",
            model_override=None,
        )
        scheduler = Scheduler(
            repos, cfg, poll_interval_s=0.05,
            registry=registry,
            adapter_for_test=lambda e: fake,
        )
        await scheduler._tick()
        t = await repos.tasks.get(task_id)
        assert t is not None and t.status == "completed", (
            f"expected completed after recovery, got {t.status}"
        )
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Scheduler subprocess-mode end-to-end (no fancy chaos, just routing)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_subprocess_mode_dispatches_and_completes(
    sandbox: dict,
) -> None:
    """Run the actual Scheduler in subprocess mode; verify it spawns
    `lyre run-task` and the task completes."""
    conn = await init_db(sandbox["db"])
    try:
        repos = SqliteRepositories(conn)
        await _seed(repos)

        task_id = await repos.tasks.create(
            TaskSpec(persona_name="dispatcher", goal="g", acceptance="a")
        )

        script_path = sandbox["tmp"] / "s.jsonl"
        _write_jsonl_script(script_path, [
            [
                {"type": "content_delta", "text": "done"},
                {"type": "turn_complete", "stop_reason": "end_turn"},
            ],
        ])

        env = {**sandbox["env"], "LYRE_MOCK_ADAPTER_SCRIPT": str(script_path)}

        cfg = Config(
            db_path=sandbox["db"],
            object_store_path=sandbox["obj"],
            memory_path=sandbox["mem"],
            anthropic_api_key="fake",
            anthropic_base_url=None,
            default_model="m",
            model_override=None,
        )
        scheduler = Scheduler(
            repos, cfg, poll_interval_s=0.05,
            spawn_subprocess=True,
        )

        # Patch the subprocess spawn to inherit our env (Scheduler uses
        # os.environ by default; tmp DB paths are in os.environ once we
        # set them via the sandbox fixture's `env`).
        for k, v in env.items():
            os.environ[k] = v

        try:
            await scheduler._tick()
            t = await repos.tasks.get(task_id)
            assert t is not None
            assert t.status == "completed", f"status was {t.status}"
        finally:
            # Restore env (tests run in same process).
            for k in env:
                if k not in {"PATH", "HOME", "USER", "LOGNAME"}:
                    os.environ.pop(k, None)
    finally:
        await conn.close()
