"""PR1: the needs_input park/resume seam + repos.transaction().

These build the kill-safe primitive a scheduler-driven fan-in barrier needs
WITHOUT a blocking "await children" (deliberately absent — context.py:317/481):
a task parks in 'needs_input', is invisible to dispatch + lease recovery while
it waits, and is resumed by exactly one scheduler phase (Phase 0.7) once its
resume flag is raised. Nothing parks a task yet, so on `main` these paths are
inert; the tests exercise the mechanism directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lyre.config import Config
from lyre.persistence.models import Persona, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.scheduler.scheduler import Scheduler

from .fake_adapter import FakeAdapter
from .helpers import fake_entry, fake_registry


def _make_config(tmp_path: Path) -> Config:
    mem = tmp_path / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    return Config(
        db_path=tmp_path / "x.db",
        object_store_path=tmp_path / "objstore",
        memory_path=mem,
        anthropic_api_key="fake",
        anthropic_base_url=None,
        default_model="m",
    )


def _worker_pref() -> dict:
    return {"tier": "workhorse", "requires": ["tool_use"], "prefer": []}


async def _new_task(repos: SqliteRepositories) -> str:
    return await repos.tasks.create(
        TaskSpec(persona_name="worker", goal="g", acceptance="a")
    )


# --------------------------------------------------------------------------
# park: a parked task is invisible to BOTH dispatch and lease recovery
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_park_hides_task_from_find_pending_and_expired_leases(
    repos: SqliteRepositories,
) -> None:
    task_id = await _new_task(repos)
    # Claim a 0s lease so it would otherwise be recoverable as expired.
    assert await repos.tasks.claim_lease(task_id, "wakeup-1", duration_sec=0)

    assert await repos.tasks.park(task_id) is True
    parked = await repos.tasks.get(task_id)
    assert parked is not None and parked.status == "needs_input"

    # Neither dispatch (pending-only) nor chaos recovery (in_progress-only)
    # picks it up: a parked task waits for an explicit resume.
    assert all(t.id != task_id for t in await repos.tasks.find_pending())
    assert all(t.id != task_id for t in await repos.tasks.find_expired_leases())


@pytest.mark.asyncio
async def test_park_only_affects_live_tasks(repos: SqliteRepositories) -> None:
    task_id = await _new_task(repos)
    await repos.tasks.update_status(task_id, "completed")
    # A terminal task must not be silently dragged back into the wait state.
    assert await repos.tasks.park(task_id) is False
    t = await repos.tasks.get(task_id)
    assert t is not None and t.status == "completed"


# --------------------------------------------------------------------------
# resume: only a flagged parked task flips, exactly once, idempotently
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_resume_phase_flips_flagged_needs_input_to_pending(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    await repos.personas.upsert(
        Persona(
            name="worker",
            role_description="w",
            system_prompt="w",
            model_preference=_worker_pref(),
        )
    )
    task_id = await _new_task(repos)
    await repos.tasks.park(task_id)
    assert await repos.tasks.request_resume(task_id) is True

    sched = Scheduler(
        repos,
        _make_config(tmp_path),
        registry=fake_registry(fake_entry()),
        adapter_for_test=lambda e: FakeAdapter(),
        auto_wake_on_mail=False,
    )
    await sched._resume_parked_tasks()

    t = await repos.tasks.get(task_id)
    assert t is not None and t.status == "pending"


@pytest.mark.asyncio
async def test_unflagged_parked_task_is_not_resumed(
    repos: SqliteRepositories,
) -> None:
    task_id = await _new_task(repos)
    await repos.tasks.park(task_id)
    # No request_resume → find_resumable must not surface it.
    assert await repos.tasks.find_resumable() == []


@pytest.mark.asyncio
async def test_resume_is_guarded_and_idempotent_across_sigkill(
    repos: SqliteRepositories,
) -> None:
    task_id = await _new_task(repos)
    await repos.tasks.park(task_id)
    await repos.tasks.request_resume(task_id)

    # First resume wins; a redundant second call (e.g. a crash re-ran the
    # phase) is a guarded no-op — the loser's RETURNING is empty.
    assert await repos.tasks.resume(task_id) is True
    assert await repos.tasks.resume(task_id) is False
    t = await repos.tasks.get(task_id)
    assert t is not None and t.status == "pending"
    # The flag is cleared, so re-running the phase won't re-resume.
    assert await repos.tasks.find_resumable() == []


@pytest.mark.asyncio
async def test_request_resume_on_unparked_task_is_false(
    repos: SqliteRepositories,
) -> None:
    task_id = await _new_task(repos)  # pending, never parked
    assert await repos.tasks.request_resume(task_id) is False


# --------------------------------------------------------------------------
# Phase 0 suppression: a parked agent is not auto-woken by normal mail
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_parked_task_suppresses_auto_wake_for_its_agent(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    await repos.personas.upsert(
        Persona(
            name="worker",
            role_description="w",
            system_prompt="w",
            model_preference=_worker_pref(),
        )
    )
    await repos.agents.create("worker-1", "worker", parent_agent_id="owner")

    # A parked in-flight task for worker-1 (the coordinator-awaiting-barrier
    # shape). find_active_for_persona counts needs_input as in-flight, so
    # Phase 0 must NOT spin up a 'check inbox' task on top of it.
    task_id = await repos.tasks.create(
        TaskSpec(agent_id="worker-1", goal="g", acceptance="a")
    )
    await repos.tasks.park(task_id)

    await repos.mailbox.ensure_mailbox("worker-1")
    from lyre.persistence.models import MailboxMessage

    await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="worker-1",
            external_id="m1",
            sender="owner",
            urgency="normal",
            body="ping",
        )
    )

    sched = Scheduler(
        repos,
        _make_config(tmp_path),
        registry=fake_registry(fake_entry()),
        adapter_for_test=lambda e: FakeAdapter(),
    )
    await sched._auto_dispatch_for_unread_mail()

    # Exactly the one parked task — no auto-dispatched task was layered on.
    tasks = await repos.tasks.search(persona_name="worker")
    assert [t.id for t in tasks] == [task_id]
    assert not any((t.metadata or {}).get("auto_dispatched") for t in tasks)


# --------------------------------------------------------------------------
# repos.transaction(): atomic multi-row write — all or nothing
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_transaction_commits_both_writes(
    repos: SqliteRepositories,
) -> None:
    t1 = await _new_task(repos)
    t2 = await _new_task(repos)
    async with repos.transaction():
        await repos.tasks.update_status(t1, "completed")
        await repos.tasks.update_status(t2, "failed")
    assert (await repos.tasks.get(t1)).status == "completed"  # type: ignore[union-attr]
    assert (await repos.tasks.get(t2)).status == "failed"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_transaction_rolls_back_partial_supervisory_write(
    repos: SqliteRepositories,
) -> None:
    t1 = await _new_task(repos)
    t2 = await _new_task(repos)
    with pytest.raises(RuntimeError, match="boom"):
        async with repos.transaction():
            await repos.tasks.update_status(t1, "completed")
            await repos.tasks.update_status(t2, "failed")
            raise RuntimeError("boom")
    # Neither write survives — the half-applied state a SIGKILL would
    # otherwise leave is rolled back as one unit.
    assert (await repos.tasks.get(t1)).status == "pending"  # type: ignore[union-attr]
    assert (await repos.tasks.get(t2)).status == "pending"  # type: ignore[union-attr]
