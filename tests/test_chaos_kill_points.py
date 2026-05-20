"""拔线测试 — 4 个 kill point + 三条通过标准。

The four kill points:

  1. before_action            — context assembled, action not started
  2. mid_action_after_tool    — at least one shell_exec dispatched
  3. post_action_pre_report   — agent finished (incl. remote push/PR) but
                                report_side_effect path not committed
  4. post_outbox_pre_dispatch — outbox row written, dispatcher hasn't run

The three pass criteria:
  A. 重启后状态完全可重建 — DB / disk / remote contains enough to reconstruct
  B. 操作幂等           — re-run never duplicates side effects (push/PR/outbox)
  C. 任务可续做         — task transitions to completed after recovery, not
                          "safely aborted, re-do from scratch"

We use in-process `SimulatedKill` (BaseException) as a kill stand-in: the
scheduler's `finally` detects it via `sys.exc_info()` and skips cleanup, so
DB state matches "real process died mid-task" semantics.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from lyre.adapter.llm_adapter import (
    ContentDelta,
    LyreMessage,
    LyreToolSpec,
    StreamEvent,
    ToolUseComplete,
    TurnComplete,
    Usage,
)
from lyre.config import Config
from lyre.outbox.dispatcher import OutboxDispatcher
from lyre.persistence.models import Persona, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.kill_switch import KillSwitch, SimulatedKill
from lyre.runtime.worktree import WorktreeManager
from lyre.scheduler.scheduler import Scheduler

from .helpers import fake_entry, fake_registry

_HAVE_TOOLS = all(
    shutil.which(t) is not None for t in ("ssh-keygen", "ssh-agent", "ssh-add", "git")
)
pytestmark = pytest.mark.skipif(not _HAVE_TOOLS, reason="git / ssh tooling missing")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class ScriptedAdapter:
    """A FakeAdapter that yields different scripts depending on which call it
    is — useful when 'first wakeup' and 'restart wakeup' need distinct
    sequences (e.g. for kill 3 the restart agent must check the remote)."""

    def __init__(self, scripts: list[list[StreamEvent]]):
        self.scripts = list(scripts)
        self.calls: list[dict] = []

    async def stream_turn(
        self,
        messages: list[LyreMessage],
        tools: list[LyreToolSpec],
        model: str,
        max_tokens: int = 4096,
        temperature: float | None = None,
        system: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.calls.append({"messages": list(messages), "system": system})
        if not self.scripts:
            yield TurnComplete(stop_reason="end_turn")
            return
        for evt in self.scripts.pop(0):
            yield evt


async def _run_cmd(argv: list[str], cwd: Path | None = None) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Lyre Test",
            "GIT_AUTHOR_EMAIL": "test@lyre.local",
            "GIT_COMMITTER_NAME": "Lyre Test",
            "GIT_COMMITTER_EMAIL": "test@lyre.local",
        },
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out, err


@pytest.fixture
async def bare_repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    rc, _, err = await _run_cmd(["git", "init", "--bare", "-b", "main", str(origin)])
    assert rc == 0, err
    seed = tmp_path / "seed"
    seed.mkdir()
    for argv in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "test@lyre.local"],
        ["git", "config", "user.name", "Lyre Test"],
    ):
        rc, _, err = await _run_cmd(argv, cwd=seed)
        assert rc == 0, err
    (seed / "README.md").write_text("# Hello\n", encoding="utf-8")
    for argv in (
        ["git", "add", "README.md"],
        ["git", "commit", "-m", "seed"],
        ["git", "remote", "add", "origin", f"file://{origin}"],
        ["git", "push", "origin", "main"],
    ):
        rc, _, err = await _run_cmd(argv, cwd=seed)
        assert rc == 0, err
    return origin


def _config(tmp_path: Path) -> Config:
    mem = tmp_path / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    return Config(
        db_path=tmp_path / "x.db",
        object_store_path=tmp_path / "objstore",
        memory_path=mem,
        anthropic_api_key="fake",
        anthropic_base_url=None,
        default_model="m",
        model_override=None,
    )


async def _seed_worker(repos: SqliteRepositories) -> None:
    await repos.personas.upsert(
        Persona(
            name="worker",
            role_description="worker",
            system_prompt="you write code",
            allowed_lyre_tools=["shell_exec", "mailbox_send", "report_side_effect"],
            model_preference={
                "tier": "workhorse", "requires": ["tool_use"], "prefer": [],
            },
            needs_worktree=True,
        )
    )
    await repos.mailbox.ensure_mailbox("owner")
    await repos.mailbox.ensure_mailbox("worker")


async def _seed_leader(repos: SqliteRepositories) -> None:
    """Lightweight persona for tests that don't need a worktree."""
    await repos.personas.upsert(
        Persona(
            name="leader",
            role_description="leader",
            system_prompt="you lead",
            allowed_lyre_tools=["mailbox_send"],
            model_preference={
                "tier": "flagship", "requires": ["tool_use"], "prefer": [],
            },
            needs_worktree=False,
        )
    )
    await repos.mailbox.ensure_mailbox("owner")
    await repos.mailbox.ensure_mailbox("leader")


# ---------------------------------------------------------------------------
# Kill point 1: before_action
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_1_before_action_recovers_via_expired_lease(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    """Process dies immediately after lease claim, before any agent action.
    The lease stays held; once it expires, scheduler reclaims and a fresh
    wakeup completes the task. No duplicate side effects (none happened)."""
    cfg = _config(tmp_path)
    cfg.object_store_path.mkdir(parents=True, exist_ok=True)
    await _seed_leader(repos)
    task_id = await repos.tasks.create(
        TaskSpec(
            persona_name="leader", goal="say hi", acceptance="msg sent",
            lease_duration_s=0,  # 0s lease → immediately expired on next poll
        )
    )

    kill = KillSwitch(fire_at="before_action")
    fake1 = ScriptedAdapter([])  # never used because we die before action
    scheduler1 = Scheduler(
        repos, cfg, poll_interval_s=0.05,
        registry=fake_registry(fake_entry(id="m", tier="flagship")),
        adapter_for_test=lambda e: fake1,
        kill_switch=kill,
    )
    with pytest.raises(SimulatedKill):
        await scheduler1._tick()

    # Q5(A): state reconstructable. Task is in_progress, lease held, no
    # outbox / mailbox effects.
    t = await repos.tasks.get(task_id)
    assert t is not None
    assert t.status == "in_progress"
    assert t.lease_holder is not None
    assert await repos.outbox.dequeue_batch(limit=10) == []

    # Wait for lease (0s) to expire under SQLite's second-resolution clock.
    await asyncio.sleep(1.1)

    # Q5(C): restart with a non-killing scheduler. Same task_id ends up
    # completed via expired-lease recovery + fresh wakeup.
    fake2 = ScriptedAdapter([
        [
            ToolUseComplete(
                id="t1", name="mailbox_send",
                input={"to": "owner", "body": "hi"},
            ),
            Usage(input_tokens=10, output_tokens=2),
            TurnComplete(stop_reason="tool_use"),
        ],
        [ContentDelta(text="done"), TurnComplete(stop_reason="end_turn")],
    ])
    scheduler2 = Scheduler(
        repos, cfg, poll_interval_s=0.05,
        registry=fake_registry(fake_entry(id="m", tier="flagship")),
        adapter_for_test=lambda e: fake2,
    )
    await scheduler2._tick()

    t = await repos.tasks.get(task_id)
    assert t is not None
    assert t.status == "completed"

    # Q5(B): idempotency. Exactly one outbox row.
    batch = await repos.outbox.dequeue_batch(limit=10)
    assert len(batch) == 1
    assert batch[0].payload["body"] == "hi"


# ---------------------------------------------------------------------------
# Kill point 2: mid_action_after_tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_2_mid_action_recovers_and_completes(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    """Agent has run at least one shell_exec, then dies. Worker has no
    checkpoint yet, so on restart it starts over. No duplicate work or
    state corruption."""
    cfg = _config(tmp_path)
    cfg.object_store_path.mkdir(parents=True, exist_ok=True)
    await _seed_worker(repos)
    task_id = await repos.tasks.create(
        TaskSpec(
            persona_name="worker", goal="probe", acceptance="probed",
            lease_duration_s=0,
        )
    )

    # 1st wakeup: agent dispatches a benign shell_exec, kill fires.
    fake1 = ScriptedAdapter([
        [
            ToolUseComplete(
                id="t1", name="shell_exec",
                input={"argv": [sys.executable, "-c", "print('alive')"]},
            ),
            TurnComplete(stop_reason="tool_use"),
        ],
        # The kill_switch fires after the tool dispatch, so this 2nd script
        # entry is never reached on the killed run.
    ])
    kill = KillSwitch(fire_at="mid_action_after_tool")
    scheduler1 = Scheduler(
        repos, cfg, poll_interval_s=0.05,
        registry=fake_registry(fake_entry(id="m", tier="workhorse")),
        adapter_for_test=lambda e: fake1,
        kill_switch=kill,
    )
    with pytest.raises(SimulatedKill):
        await scheduler1._tick()

    t = await repos.tasks.get(task_id)
    assert t is not None
    assert t.status == "in_progress"

    await asyncio.sleep(1.1)

    # 2nd wakeup: completes cleanly.
    fake2 = ScriptedAdapter([
        [
            ContentDelta(text="all done"),
            TurnComplete(stop_reason="end_turn"),
        ],
    ])
    scheduler2 = Scheduler(
        repos, cfg, poll_interval_s=0.05,
        registry=fake_registry(fake_entry(id="m", tier="workhorse")),
        adapter_for_test=lambda e: fake2,
    )
    await scheduler2._tick()

    t = await repos.tasks.get(task_id)
    assert t is not None
    assert t.status == "completed"

    # Worktree was cleaned on success; the 1st worktree was NOT cleaned (the
    # simulated kill skipped the finally) but the second wakeup reuses the
    # same task_id so the dir gets re-prepared atop the old one and finally
    # wiped on success.
    worktree_dir = cfg.object_store_path / "worktrees" / task_id
    assert not worktree_dir.exists()


# ---------------------------------------------------------------------------
# Kill point 3: post_action_pre_report (recovery requires remote-aware agent)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_3_post_commit_pre_report_agent_recovers_from_remote(
    repos: SqliteRepositories, tmp_path: Path, bare_repo: Path,
) -> None:
    """Agent has cloned + edited + pushed to a feature branch in the bare
    repo. Kill fires before `report_side_effect` could be written to outbox.
    Recovery: the agent restarts, checks the remote, sees its branch already
    exists, skips re-pushing, and just emits the report_side_effect.

    Pass criteria (Q5):
      A) Remote-side push is visible (state reconstructable from remote)
      B) Second run does NOT re-push or create a 2nd PR (we check that the
         branch only got one commit)
      C) Task transitions to completed; outbox now has exactly one tier-1
         notification.
    """
    cfg = _config(tmp_path)
    cfg.object_store_path.mkdir(parents=True, exist_ok=True)
    await _seed_worker(repos)
    origin_url = f"file://{bare_repo}"
    branch = "lyre/feature-kill3"
    task_id = await repos.tasks.create(
        TaskSpec(
            persona_name="worker",
            goal=f"clone {origin_url}, edit README, push branch {branch}",
            acceptance="branch exists on remote",
            lease_duration_s=0,
        )
    )

    # First run: full clone → edit → commit → push, then post_action_pre_report
    # kill fires (scheduler-level, between agent_loop.run() and wakeups.end).
    edit_script = (
        "import pathlib; p=pathlib.Path('repo/README.md');"
        " p.write_text(p.read_text() + 'managed by lyre\\n')"
    )
    first_run_turns: list[list[StreamEvent]] = [
        [
            ToolUseComplete(id="t1", name="shell_exec",
                input={"argv": ["git", "clone", origin_url, "repo"]}),
            TurnComplete(stop_reason="tool_use"),
        ],
        [
            ToolUseComplete(id="t2", name="shell_exec",
                input={"argv": ["git", "-C", "repo", "config", "user.email", "w@lyre"]}),
            TurnComplete(stop_reason="tool_use"),
        ],
        [
            ToolUseComplete(id="t3", name="shell_exec",
                input={"argv": ["git", "-C", "repo", "config", "user.name", "Lyre"]}),
            TurnComplete(stop_reason="tool_use"),
        ],
        [
            ToolUseComplete(id="t4", name="shell_exec",
                input={"argv": ["git", "-C", "repo", "checkout", "-b", branch]}),
            TurnComplete(stop_reason="tool_use"),
        ],
        [
            ToolUseComplete(id="t5", name="shell_exec",
                input={"argv": [sys.executable, "-c", edit_script]}),
            TurnComplete(stop_reason="tool_use"),
        ],
        [
            ToolUseComplete(id="t6", name="shell_exec",
                input={"argv": ["git", "-C", "repo", "add", "README.md"]}),
            TurnComplete(stop_reason="tool_use"),
        ],
        [
            ToolUseComplete(id="t7", name="shell_exec",
                input={"argv": ["git", "-C", "repo", "commit", "-m", "managed by lyre"]}),
            TurnComplete(stop_reason="tool_use"),
        ],
        [
            ToolUseComplete(id="t8", name="shell_exec",
                input={"argv": ["git", "-C", "repo", "push", "origin", branch]}),
            TurnComplete(stop_reason="tool_use"),
        ],
        # Agent has finished pushing. It hasn't called report_side_effect yet.
        # End the turn with end_turn so agent_loop returns normally. Then the
        # scheduler-level kill_switch fires at post_action_pre_report.
        [ContentDelta(text="pushed"), TurnComplete(stop_reason="end_turn")],
    ]
    fake1 = ScriptedAdapter(first_run_turns)
    kill = KillSwitch(fire_at="post_action_pre_report")
    scheduler1 = Scheduler(
        repos, cfg, poll_interval_s=0.05,
        registry=fake_registry(fake_entry(id="m", tier="workhorse")),
        adapter_for_test=lambda e: fake1,
        kill_switch=kill,
        worktree_manager=WorktreeManager(root=cfg.object_store_path / "worktrees"),
    )
    with pytest.raises(SimulatedKill):
        await scheduler1._tick()

    # Q5(A): Remote-side push is visible.
    rc, out, err = await _run_cmd(["git", "--git-dir", str(bare_repo), "branch", "-a"])
    assert rc == 0, err
    assert branch in out.decode()

    # Outbox is still empty (agent never got to call report_side_effect).
    assert await repos.outbox.dequeue_batch(limit=10) == []

    t = await repos.tasks.get(task_id)
    assert t is not None
    assert t.status == "in_progress"

    await asyncio.sleep(1.1)

    # Restart: agent must detect remote branch exists and just call
    # report_side_effect (skipping the re-push).
    second_run_turns: list[list[StreamEvent]] = [
        # The recovery agent first probes the remote.
        [
            ToolUseComplete(id="r1", name="shell_exec",
                input={"argv": ["git", "ls-remote", "--heads", origin_url, branch]}),
            TurnComplete(stop_reason="tool_use"),
        ],
        # Remote has it. Skip everything else, file the side-effect.
        [
            ToolUseComplete(id="r2", name="report_side_effect",
                input={"kind": "pushed_branch",
                       "payload": {"branch": branch, "url": origin_url}}),
            TurnComplete(stop_reason="tool_use"),
        ],
        [ContentDelta(text="recovered"), TurnComplete(stop_reason="end_turn")],
    ]
    fake2 = ScriptedAdapter(second_run_turns)
    scheduler2 = Scheduler(
        repos, cfg, poll_interval_s=0.05,
        registry=fake_registry(fake_entry(id="m", tier="workhorse")),
        adapter_for_test=lambda e: fake2,
        worktree_manager=WorktreeManager(root=cfg.object_store_path / "worktrees"),
    )
    await scheduler2._tick()

    t = await repos.tasks.get(task_id)
    assert t is not None
    assert t.status == "completed"

    # Q5(B): branch only got pushed ONCE (commit count = 1 above seed).
    rc, out, err = await _run_cmd(
        ["git", "--git-dir", str(bare_repo), "rev-list", "--count", branch]
    )
    assert rc == 0, err
    # Seed has 1 commit on main; the feature branch adds 1 more.
    assert out.strip() == b"2", f"expected 2 commits on {branch}, got {out!r}"

    # Q5: exactly one tier-1 outbox row.
    batch = await repos.outbox.dequeue_batch(limit=10)
    tier1 = [r for r in batch if r.kind == "tier1_notification"]
    assert len(tier1) == 1
    assert tier1[0].payload["kind"] == "pushed_branch"


# ---------------------------------------------------------------------------
# Kill point 4: post_outbox_pre_dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_4_outbox_dispatcher_resumes_after_restart(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    """Agent finished cleanly and wrote one outbox row. The dispatcher dies
    BEFORE delivering. After restart, the row is still there and delivery
    completes, owner mailbox gets exactly one message."""
    cfg = _config(tmp_path)
    cfg.object_store_path.mkdir(parents=True, exist_ok=True)
    await _seed_leader(repos)
    task_id = await repos.tasks.create(
        TaskSpec(
            persona_name="leader", goal="ping owner",
            acceptance="owner got the msg", lease_duration_s=600,
        )
    )

    # Phase A: scheduler runs the task to completion, an outbox row is
    # written by the worker.
    fake = ScriptedAdapter([
        [
            ToolUseComplete(id="t1", name="mailbox_send",
                input={"to": "owner", "body": "hello"}),
            TurnComplete(stop_reason="tool_use"),
        ],
        [ContentDelta(text="done"), TurnComplete(stop_reason="end_turn")],
    ])
    scheduler = Scheduler(
        repos, cfg, poll_interval_s=0.05,
        registry=fake_registry(fake_entry(id="m", tier="flagship")),
        adapter_for_test=lambda e: fake,
    )
    await scheduler._tick()

    t = await repos.tasks.get(task_id)
    assert t is not None and t.status == "completed"
    # Outbox row exists, not yet delivered.
    batch = await repos.outbox.dequeue_batch(limit=10)
    assert len(batch) == 1
    assert (await repos.mailbox.read_messages("owner")) == []

    # Phase B: dispatcher fires the kill BEFORE delivery.
    kill = KillSwitch(fire_at="post_outbox_pre_dispatch")
    dispatcher1 = OutboxDispatcher(repos, kill_switch=kill)
    with pytest.raises(SimulatedKill):
        await dispatcher1.tick()

    # Q5(A): row still there, mailbox still empty.
    batch = await repos.outbox.dequeue_batch(limit=10)
    assert len(batch) == 1
    assert (await repos.mailbox.read_messages("owner")) == []

    # Phase C: fresh dispatcher (no kill) — resumes delivery.
    dispatcher2 = OutboxDispatcher(repos)
    delivered = await dispatcher2.tick()
    assert delivered == 1

    # Q5(B): exactly one message in owner mailbox.
    msgs = await repos.mailbox.read_messages("owner")
    assert len(msgs) == 1
    assert msgs[0].body == "hello"

    # Re-running dispatcher is a no-op (row already marked dispatched).
    assert await dispatcher2.tick() == 0
    assert len(await repos.mailbox.read_messages("owner")) == 1
