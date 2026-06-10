"""Orphan-wakeup-row recovery.

Bug class: a wakeup row left at ``ended_at IS NULL`` after the wakeup
process is gone (kill-test crash, host shutdown without graceful
drain, ``claim_lease`` failure on the same tick that INSERTed the
row). Until this fix landed, such a row poisoned
``has_active_for_agent`` for the entire agent — the scheduler's
"agents are sequential actors" gate would latch shut and every future
pending task of that agent would be skipped on every tick.

Three layers covered here:

  * DAO ``close_orphans_for_task`` only touches active rows of the
    target task, leaves siblings alone.
  * DAO ``has_active_for_agent`` ignores wakeups whose task is in a
    terminal state — those orphans must not block dispatch.
  * Scheduler ``_run_task_inline`` sweeps prior orphans before
    opening a fresh wakeup, and closes its own wakeup row if the
    lease claim fails on the same tick (the no-crash leak path).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lyre.adapter.llm_adapter import (
    ContentDelta,
    TurnComplete,
    Usage,
)
from lyre.config import Config
from lyre.persistence.models import Persona, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.scheduler.scheduler import Scheduler

from .fake_adapter import FakeAdapter
from .helpers import fake_entry, fake_registry


def _make_config(tmp_path: Path) -> Config:
    mem = tmp_path / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    obj = tmp_path / "objstore"
    obj.mkdir(parents=True, exist_ok=True)
    return Config(
        db_path=tmp_path / "x.db",
        object_store_path=obj,
        memory_path=mem,
        anthropic_api_key="fake",
        anthropic_base_url=None,
        default_model="m",
        model_override=None,
    )


async def _ended_at(repos: SqliteRepositories, wakeup_id: str) -> str | None:
    async with repos.conn.execute(
        "SELECT ended_at, end_status FROM wakeups WHERE id = ?", (wakeup_id,),
    ) as cur:
        row = await cur.fetchone()
    return row if row is None else (row["ended_at"], row["end_status"])  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# DAO: close_orphans_for_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_orphans_for_task_closes_open_rows_only(
    repos: SqliteRepositories,
) -> None:
    """Sweep only the rows that are actually open: a wakeup that was
    properly ``end``-ed earlier must not get its ``ended_at`` rewritten
    by the sweep, and wakeups of OTHER tasks must not be touched at
    all."""
    await repos.personas.upsert(
        Persona(name="worker", role_description="w", system_prompt="w")
    )
    task_a = await repos.tasks.create(
        TaskSpec(persona_name="worker", goal="a", acceptance="a")
    )
    task_b = await repos.tasks.create(
        TaskSpec(persona_name="worker", goal="b", acceptance="b")
    )

    # task_a: one already-ended wakeup + one orphan
    closed_a = await repos.wakeups.start(task_a, "worker")
    await repos.wakeups.end(closed_a, end_status="completed")
    orphan_a = await repos.wakeups.start(task_a, "worker")
    # task_b: one orphan, must NOT be touched by sweep on task_a
    orphan_b = await repos.wakeups.start(task_b, "worker")

    n = await repos.wakeups.close_orphans_for_task(task_a)
    assert n == 1  # exactly the orphan on task_a

    closed_row = await _ended_at(repos, closed_a)
    assert closed_row is not None
    assert closed_row[1] == "completed"  # untouched

    swept_row = await _ended_at(repos, orphan_a)
    assert swept_row is not None
    assert swept_row[0] is not None  # ended_at filled in
    assert swept_row[1] == "abandoned"

    sibling_row = await _ended_at(repos, orphan_b)
    assert sibling_row is not None
    assert sibling_row[0] is None  # still active — different task
    assert sibling_row[1] is None


@pytest.mark.asyncio
async def test_close_orphans_returns_zero_when_nothing_to_sweep(
    repos: SqliteRepositories,
) -> None:
    """Idempotent / cheap when called speculatively: no rows touched
    returns 0, no side effects, no exception."""
    await repos.personas.upsert(
        Persona(name="worker", role_description="w", system_prompt="w")
    )
    task_id = await repos.tasks.create(
        TaskSpec(persona_name="worker", goal="g", acceptance="a")
    )

    n = await repos.wakeups.close_orphans_for_task(task_id)
    assert n == 0


# ---------------------------------------------------------------------------
# DAO: has_active_for_agent ignores wakeups whose task is terminal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_has_active_for_agent_ignores_terminal_task_orphans(
    repos: SqliteRepositories,
) -> None:
    """The exact production bug: agent ``momoka`` carries an orphan
    wakeup row whose task already reached ``completed``. The dispatch
    gate must report False — otherwise the next pending task for the
    same agent gets skipped forever."""
    await repos.personas.upsert(
        Persona(name="momoka", role_description="m", system_prompt="m")
    )
    await repos.agents.create(agent_id="momoka", persona_name="momoka")
    task_id = await repos.tasks.create(
        TaskSpec(
            persona_name="momoka", agent_id="momoka",
            goal="g", acceptance="a",
        )
    )
    await repos.wakeups.start(task_id, "momoka", agent_id="momoka")
    # Wakeup never gets ended_at written; task transitions to terminal
    # via the recovery path (modelled here as a direct status flip).
    await repos.tasks.update_status(task_id, "completed")

    assert await repos.wakeups.has_active_for_agent("momoka") is False


@pytest.mark.asyncio
async def test_has_active_for_agent_still_blocks_for_inflight_task(
    repos: SqliteRepositories,
) -> None:
    """The JOIN must not over-fire: a genuinely-running wakeup (open
    row + task still in_progress) MUST report True so we don't
    double-dispatch the same agent."""
    await repos.personas.upsert(
        Persona(name="momoka", role_description="m", system_prompt="m")
    )
    await repos.agents.create(agent_id="momoka", persona_name="momoka")
    task_id = await repos.tasks.create(
        TaskSpec(
            persona_name="momoka", agent_id="momoka",
            goal="g", acceptance="a",
        )
    )
    await repos.wakeups.start(task_id, "momoka", agent_id="momoka")
    await repos.tasks.update_status(task_id, "in_progress")

    assert await repos.wakeups.has_active_for_agent("momoka") is True


@pytest.mark.asyncio
async def test_has_active_for_agent_blocks_for_pending_too(
    repos: SqliteRepositories,
) -> None:
    """Edge case: wakeup row open, task still at the initial ``pending``
    status (e.g. the wakeup INSERTed but ``claim_lease`` hasn't
    finished yet, or the same tick is mid-execution). This must still
    block — we'd double-INSERT a wakeup row otherwise."""
    await repos.personas.upsert(
        Persona(name="momoka", role_description="m", system_prompt="m")
    )
    await repos.agents.create(agent_id="momoka", persona_name="momoka")
    task_id = await repos.tasks.create(
        TaskSpec(
            persona_name="momoka", agent_id="momoka",
            goal="g", acceptance="a",
        )
    )
    await repos.wakeups.start(task_id, "momoka", agent_id="momoka")
    # task.status defaults to "pending" — leave it.

    assert await repos.wakeups.has_active_for_agent("momoka") is True


# ---------------------------------------------------------------------------
# DAO: find_terminal_task_orphans
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_terminal_task_orphans_reports_terminal_pairs_only(
    repos: SqliteRepositories,
) -> None:
    """Audit query: list rows where a wakeup is still active against a
    terminal task. Open-on-inflight pairs and closed-on-terminal pairs
    must NOT appear — the combination IS the corruption signal."""
    await repos.personas.upsert(
        Persona(name="w", role_description="w", system_prompt="w")
    )
    await repos.agents.create(agent_id="w", persona_name="w")

    # Pair A: orphan + completed task — IS the bug pattern
    task_a = await repos.tasks.create(
        TaskSpec(persona_name="w", agent_id="w", goal="a", acceptance="a")
    )
    orphan_a = await repos.wakeups.start(task_a, "w", agent_id="w")
    await repos.tasks.update_status(task_a, "completed")

    # Pair B: orphan + in_progress task — legitimate, not the bug
    task_b = await repos.tasks.create(
        TaskSpec(persona_name="w", agent_id="w", goal="b", acceptance="b")
    )
    await repos.wakeups.start(task_b, "w", agent_id="w")
    await repos.tasks.update_status(task_b, "in_progress")

    # Pair C: properly-ended wakeup + completed task — clean
    task_c = await repos.tasks.create(
        TaskSpec(persona_name="w", agent_id="w", goal="c", acceptance="c")
    )
    w_c = await repos.wakeups.start(task_c, "w", agent_id="w")
    await repos.wakeups.end(w_c, end_status="completed")
    await repos.tasks.update_status(task_c, "completed")

    orphans = await repos.wakeups.find_terminal_task_orphans(limit=10)
    assert [o["wakeup_id"] for o in orphans] == [orphan_a]
    assert orphans[0]["task_id"] == task_a
    assert orphans[0]["task_status"] == "completed"


# ---------------------------------------------------------------------------
# Scheduler: _run_task_inline closes prior orphans before new wakeup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_task_inline_sweeps_prior_orphan_before_new_wakeup(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    """Recovery path: a previous wakeup of this task left an orphan
    row (its process died mid-flight). When the scheduler picks the
    task up again, ``_run_task_inline`` must close the prior orphan
    so that ``has_active_for_agent`` doesn't trip on it during this
    very run — and so the dashboard's "active wakeups" listing
    doesn't keep showing a ghost forever."""
    cfg = _make_config(tmp_path)
    await repos.personas.upsert(
        Persona(name="worker", role_description="w", system_prompt="w")
    )
    await repos.agents.create(agent_id="worker", persona_name="worker")
    task_id = await repos.tasks.create(
        TaskSpec(
            persona_name="worker", agent_id="worker",
            goal="g", acceptance="a",
        )
    )
    # Plant the orphan: a prior wakeup row that never got closed.
    orphan_id = await repos.wakeups.start(task_id, "worker", agent_id="worker")

    fake = FakeAdapter()
    fake.push_turn([
        ContentDelta(text="done."),
        Usage(input_tokens=5, output_tokens=2),
        TurnComplete(stop_reason="end_turn"),
    ])
    registry = fake_registry(fake_entry(id="fake.workhorse", tier="workhorse"))
    scheduler = Scheduler(
        repos, cfg, poll_interval_s=0.05,
        registry=registry, adapter_for_test=lambda e: fake,
    )

    await scheduler._run_task_inline(task_id)

    orphan_row = await _ended_at(repos, orphan_id)
    assert orphan_row is not None
    assert orphan_row[0] is not None, "orphan must be closed by sweep"
    assert orphan_row[1] == "abandoned"


@pytest.mark.asyncio
async def test_failed_claim_lease_does_not_leak_wakeup_row(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    """Losing the claim race must leave no open wakeup row. The wakeup
    INSERT and the lease claim now commit in ONE transaction; on a lost
    race the transaction rolls back, so normal lease contention leaves
    no trace at all (previously the row was closed as 'abandoned' in a
    second commit — itself a crash window)."""
    cfg = _make_config(tmp_path)
    await repos.personas.upsert(
        Persona(name="worker", role_description="w", system_prompt="w")
    )
    await repos.agents.create(agent_id="worker", persona_name="worker")
    task_id = await repos.tasks.create(
        TaskSpec(
            persona_name="worker", agent_id="worker",
            goal="g", acceptance="a",
        )
    )
    # Plant an already-held lease that won't expire on its own —
    # claim_lease in _run_task_inline will fail.
    other_holder = "some-other-wakeup-id"
    await repos.conn.execute(
        """
        UPDATE tasks
        SET lease_holder = ?,
            lease_until = strftime('%Y-%m-%dT%H:%M:%fZ','now', '+1 hour'),
            status = 'in_progress'
        WHERE id = ?
        """,
        (other_holder, task_id),
    )
    await repos.conn.commit()

    fake = FakeAdapter()
    registry = fake_registry(fake_entry(id="fake.workhorse", tier="workhorse"))
    scheduler = Scheduler(
        repos, cfg, poll_interval_s=0.05,
        registry=registry, adapter_for_test=lambda e: fake,
    )

    await scheduler._run_task_inline(task_id)

    # Whatever wakeup_id _run_task_inline INSERTed, it must not have
    # left it open. Query directly: zero rows with ended_at IS NULL.
    async with repos.conn.execute(
        "SELECT COUNT(*) AS n FROM wakeups WHERE task_id = ? AND ended_at IS NULL",
        (task_id,),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["n"] == 0, "failed-claim path must not leak active wakeup rows"


@pytest.mark.asyncio
async def test_scheduler_startup_logs_terminal_task_orphans(
    repos: SqliteRepositories, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One-shot audit on ``scheduler.run`` startup: emit a warning when
    the DB carries terminal-task / open-wakeup pairs (the corruption
    pattern). Doesn't auto-repair — visibility is the goal so repeat
    occurrences register on the operator's radar.

    structlog writes to stdout in this project (not stdlib logging),
    so we read capsys instead of caplog.
    """
    cfg = _make_config(tmp_path)
    await repos.personas.upsert(
        Persona(name="momoka", role_description="m", system_prompt="m")
    )
    await repos.agents.create(agent_id="momoka", persona_name="momoka")
    task_id = await repos.tasks.create(
        TaskSpec(
            persona_name="momoka", agent_id="momoka",
            goal="g", acceptance="a",
        )
    )
    orphan_id = await repos.wakeups.start(task_id, "momoka", agent_id="momoka")
    await repos.tasks.update_status(task_id, "completed")

    registry = fake_registry(fake_entry(id="fake.workhorse", tier="workhorse"))
    scheduler = Scheduler(
        repos, cfg, poll_interval_s=0.05,
        registry=registry, adapter_for_test=lambda e: FakeAdapter(),
    )

    await scheduler._log_terminal_task_orphan_wakeups()

    captured = capsys.readouterr()
    assert "scheduler_terminal_task_orphan_wakeups_detected" in captured.out
    # The sample carries enough id-level detail that the operator can
    # find the row from the log line alone.
    assert orphan_id in captured.out
    assert task_id in captured.out


@pytest.mark.asyncio
async def test_scheduler_startup_silent_when_no_orphans(
    repos: SqliteRepositories, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Healthy DB: no rows match, no log line emitted. We don't want
    a noisy "0 orphans detected" message on every startup."""
    cfg = _make_config(tmp_path)
    registry = fake_registry(fake_entry(id="fake.workhorse", tier="workhorse"))
    scheduler = Scheduler(
        repos, cfg, poll_interval_s=0.05,
        registry=registry, adapter_for_test=lambda e: FakeAdapter(),
    )

    await scheduler._log_terminal_task_orphan_wakeups()

    captured = capsys.readouterr()
    assert "terminal_task_orphan_wakeups_detected" not in captured.out


@pytest.mark.asyncio
async def test_claim_lease_crash_window_cannot_brick_the_agent(
    repos: SqliteRepositories, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The C-1 kill window: wakeups.start used to commit BEFORE
    claim_lease, so a SIGKILL (or a claim raising, e.g. busy_timeout)
    between the two left an open wakeup row on a still-'pending'
    leaseless task. has_active_for_agent counts that as in-flight, so
    Phase 3 skipped every future task of the agent forever — and the
    only sweeper sits behind that very gate. With the atomic
    start+claim transaction, a claim failure rolls the row back out:
    the task stays pending and dispatchable."""
    cfg = _make_config(tmp_path)
    await repos.personas.upsert(
        Persona(name="worker", role_description="w", system_prompt="w")
    )
    await repos.agents.create(agent_id="worker", persona_name="worker")
    task_id = await repos.tasks.create(
        TaskSpec(
            persona_name="worker", agent_id="worker",
            goal="g", acceptance="a",
        )
    )

    async def _claim_boom(*args: object, **kwargs: object) -> bool:
        raise RuntimeError("simulated kill between start and claim")

    monkeypatch.setattr(repos.tasks, "claim_lease", _claim_boom)

    fake = FakeAdapter()
    registry = fake_registry(fake_entry(id="fake.workhorse", tier="workhorse"))
    scheduler = Scheduler(
        repos, cfg, poll_interval_s=0.05,
        registry=registry, adapter_for_test=lambda e: fake,
    )

    with pytest.raises(RuntimeError, match="simulated kill"):
        await scheduler._run_task_inline(task_id)

    # The transaction must have rolled the wakeup INSERT back out.
    async with repos.conn.execute(
        "SELECT COUNT(*) AS n FROM wakeups WHERE task_id = ?", (task_id,),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["n"] == 0, "no wakeup row may survive the failed claim"

    # The agent is NOT bricked: the dispatch gate sees it as idle and the
    # task is still pending, so the next tick simply retries.
    assert not await repos.wakeups.has_active_for_agent("worker")
    task = await repos.tasks.get(task_id)
    assert task is not None
    assert task.status == "pending"
