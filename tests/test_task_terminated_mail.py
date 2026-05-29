"""``task_terminated`` mail delivery — Lyre's OTP monitor / DOWN analogue.

When a task reaches a terminal state (completed / failed / cancelled),
the scheduler enqueues a structured ``mailbox_send`` outbox row whose
recipient is the parent task's agent (or ``owner`` as fallback). This
lets dispatchers / supervisor personas pattern-match on
``metadata.kind == 'task_terminated'`` and react to child outcomes
without polling ``query_task_status``.

Tests cover:

  * Outcome → recipient resolution (parent → owner fallback)
  * Failure vs success urgency (high vs normal)
  * Metadata shape supervisors will pattern-match on
  * Non-terminal transitions don't fire
  * Exception path still fires (the "sudden failed 没人知道" case)
  * Archived parent agent falls back to owner
  * Idempotency via external_id
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lyre.adapter.llm_adapter import (
    ToolUseComplete,
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


async def _seed(repos: SqliteRepositories) -> None:
    """Owner + dispatcher + worker — the three agents most supervisor
    scenarios touch. Tasks created per-test."""
    await repos.personas.upsert(
        Persona(name="owner", role_description="o", system_prompt="o")
    )
    await repos.personas.upsert(
        Persona(name="dispatcher", role_description="d", system_prompt="d")
    )
    await repos.personas.upsert(
        Persona(
            name="worker", role_description="w", system_prompt="w",
            allowed_lyre_tools=["mailbox_send"],
            model_preference={
                "tier": "workhorse", "requires": ["tool_use"], "prefer": [],
            },
        )
    )
    await repos.agents.create(agent_id="owner", persona_name="owner")
    await repos.agents.create(agent_id="dispatcher", persona_name="dispatcher")
    await repos.agents.create(agent_id="worker", persona_name="worker")


def _build_scheduler(repos: SqliteRepositories, cfg: Config, fake: FakeAdapter) -> Scheduler:
    registry = fake_registry(fake_entry(id="fake.workhorse", tier="workhorse"))
    return Scheduler(
        repos, cfg, poll_interval_s=0.05,
        registry=registry,
        adapter_for_test=lambda e: fake,
    )


# ---------------------------------------------------------------------------
# Happy path: child completes → parent receives task_terminated mail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_child_done_delivers_task_terminated_to_parent(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    cfg = _make_config(tmp_path)
    await _seed(repos)

    # Dispatcher's task is the parent; worker's task is its child.
    parent_id = await repos.tasks.create(
        TaskSpec(agent_id="dispatcher", goal="coordinate", acceptance="a")
    )
    child_id = await repos.tasks.create(
        TaskSpec(
            agent_id="worker", goal="do the thing", acceptance="a",
            parent_task_id=parent_id,
        )
    )

    fake = FakeAdapter()
    fake.push_done(summary="thing done")
    scheduler = _build_scheduler(repos, cfg, fake)

    # The scheduler's Phase 3 will pick the child up (worker has the
    # pending task). We _tick() once to drive it through.
    # Both parent + child are pending; sort by created_at gives parent
    # first, but worker's agent has the child task. Both could run on
    # the same tick — to isolate the child, run child via _run_task_inline.
    await scheduler._run_task_inline(child_id)

    # Inspect the outbox: one row addressed to dispatcher with
    # metadata.kind == 'task_terminated'.
    batch = await repos.outbox.dequeue_batch(limit=10)
    terminated = [
        r for r in batch
        if r.payload.get("metadata", {}).get("kind") == "task_terminated"
    ]
    assert len(terminated) == 1, (
        f"expected exactly one task_terminated mail; "
        f"got {len(terminated)}: {batch}"
    )
    mail = terminated[0]
    assert mail.payload["recipient"] == "dispatcher"
    assert mail.payload["sender"] == "worker"
    assert mail.payload["urgency"] == "normal"  # completed → normal
    assert mail.payload["metadata"]["outcome"] == "completed"
    assert mail.payload["metadata"]["task_id"] == child_id
    assert mail.payload["metadata"]["parent_task_id"] == parent_id
    assert mail.payload["metadata"]["summary"] == "thing done"


@pytest.mark.asyncio
async def test_top_level_task_completion_falls_back_to_owner(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    """Tasks without a parent (top-level dispatch from owner / system)
    target ``owner`` as the supervisor — the human is the root of the
    supervision tree."""
    cfg = _make_config(tmp_path)
    await _seed(repos)

    task_id = await repos.tasks.create(
        TaskSpec(agent_id="worker", goal="top-level work", acceptance="a")
    )

    fake = FakeAdapter()
    fake.push_done(summary="topped out")
    scheduler = _build_scheduler(repos, cfg, fake)
    await scheduler._run_task_inline(task_id)

    batch = await repos.outbox.dequeue_batch(limit=10)
    terminated = [
        r for r in batch
        if r.payload.get("metadata", {}).get("kind") == "task_terminated"
    ]
    assert len(terminated) == 1
    assert terminated[0].payload["recipient"] == "owner"
    assert terminated[0].payload["urgency"] == "normal"


# ---------------------------------------------------------------------------
# Failure path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_child_failed_carries_reason_and_high_urgency(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    """Failure mail uses urgency=high so MailWatcher fires the
    mid-wakeup interrupt — supervisor sees the failure immediately
    even if it's busy with something else."""
    cfg = _make_config(tmp_path)
    await _seed(repos)

    parent_id = await repos.tasks.create(
        TaskSpec(agent_id="dispatcher", goal="parent", acceptance="a")
    )
    child_id = await repos.tasks.create(
        TaskSpec(
            agent_id="worker", goal="risky", acceptance="a",
            parent_task_id=parent_id,
        )
    )

    fake = FakeAdapter()
    fake.push_turn([
        ToolUseComplete(
            id="ew", name="end_wakeup",
            input={
                "status": "failed",
                "summary": "GitHub returned 500",
                "failure_reason": "provider_error",
                "recoverable": True,
            },
        ),
        Usage(input_tokens=10, output_tokens=5),
        TurnComplete(stop_reason="end_turn"),
    ])
    scheduler = _build_scheduler(repos, cfg, fake)
    await scheduler._run_task_inline(child_id)

    batch = await repos.outbox.dequeue_batch(limit=10)
    terminated = [
        r for r in batch
        if r.payload.get("metadata", {}).get("kind") == "task_terminated"
    ]
    assert len(terminated) == 1
    mail = terminated[0]
    assert mail.payload["urgency"] == "high"  # failed → high
    md = mail.payload["metadata"]
    assert md["outcome"] == "failed"
    assert md["failure_reason"] == "provider_error"
    assert md["recoverable"] is True
    # Body carries the same info in plain form (for owner inbox UIs
    # that don't parse metadata).
    body = mail.payload["body"]
    assert "failure_reason: provider_error" in body
    assert "recoverable: True" in body


@pytest.mark.asyncio
async def test_runtime_exception_path_still_fires_terminated_mail(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    """Direct test of the exact 'sudden failed 没人知道' scenario:
    the agent loop raises a Python exception mid-wakeup; the
    scheduler's exception handler must STILL synthesise the
    task_terminated mail so the supervisor isn't silently left
    waiting."""
    cfg = _make_config(tmp_path)
    await _seed(repos)

    parent_id = await repos.tasks.create(
        TaskSpec(agent_id="dispatcher", goal="parent", acceptance="a")
    )
    child_id = await repos.tasks.create(
        TaskSpec(
            agent_id="worker", goal="will crash", acceptance="a",
            parent_task_id=parent_id,
        )
    )

    class BoomAdapter:
        """Raises mid-stream — exercises the scheduler's except path."""

        async def stream_turn(self, *args, **kwargs):
            raise RuntimeError("boom in adapter") from None
            yield  # never reached; makes this an async generator

    scheduler = Scheduler(
        repos, cfg, poll_interval_s=0.05,
        registry=fake_registry(fake_entry(id="fake.workhorse", tier="workhorse")),
        adapter_for_test=lambda e: BoomAdapter(),
    )
    await scheduler._run_task_inline(child_id)

    batch = await repos.outbox.dequeue_batch(limit=10)
    terminated = [
        r for r in batch
        if r.payload.get("metadata", {}).get("kind") == "task_terminated"
    ]
    assert len(terminated) == 1, (
        f"expected runtime-exception path to fire task_terminated; "
        f"outbox had: {batch}"
    )
    mail = terminated[0]
    md = mail.payload["metadata"]
    assert md["outcome"] == "failed"
    assert md["failure_reason"] == "tool_error"  # runtime convention
    assert md["recoverable"] is False
    assert mail.payload["urgency"] == "high"
    assert "RuntimeError" in mail.payload["body"]


# ---------------------------------------------------------------------------
# No-fire cases: non-terminal transitions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_awaiting_does_not_fire_task_terminated(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    """awaiting → tasks.status='needs_input' is mid-flight, NOT
    terminal. No mail; the task is still alive."""
    cfg = _make_config(tmp_path)
    await _seed(repos)
    task_id = await repos.tasks.create(
        TaskSpec(agent_id="worker", goal="paused", acceptance="a")
    )

    fake = FakeAdapter()
    fake.push_awaiting(
        awaiting_on="mail",
        summary="waiting for owner reply",
    )
    scheduler = _build_scheduler(repos, cfg, fake)
    await scheduler._run_task_inline(task_id)

    batch = await repos.outbox.dequeue_batch(limit=10)
    terminated = [
        r for r in batch
        if r.payload.get("metadata", {}).get("kind") == "task_terminated"
    ]
    assert terminated == []


@pytest.mark.asyncio
async def test_in_progress_yield_does_not_fire_task_terminated(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    """in_progress (deliberate yield) keeps the task active. No mail
    until the task actually reaches a terminal state."""
    cfg = _make_config(tmp_path)
    await _seed(repos)
    task_id = await repos.tasks.create(
        TaskSpec(agent_id="worker", goal="batched", acceptance="a")
    )

    fake = FakeAdapter()
    fake.push_turn([
        ToolUseComplete(
            id="ew", name="end_wakeup",
            input={"status": "in_progress", "summary": "yielded"},
        ),
        Usage(input_tokens=10, output_tokens=5),
        TurnComplete(stop_reason="end_turn"),
    ])
    scheduler = _build_scheduler(repos, cfg, fake)
    await scheduler._run_task_inline(task_id)

    batch = await repos.outbox.dequeue_batch(limit=10)
    terminated = [
        r for r in batch
        if r.payload.get("metadata", {}).get("kind") == "task_terminated"
    ]
    assert terminated == []


# ---------------------------------------------------------------------------
# Archived parent falls back to owner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archived_parent_agent_falls_back_to_owner(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    """If the parent task's agent has been archived (operator manually
    retired the supervisor), the failure signal must not silently
    disappear — fall through to the owner."""
    cfg = _make_config(tmp_path)
    await _seed(repos)

    parent_id = await repos.tasks.create(
        TaskSpec(agent_id="dispatcher", goal="parent", acceptance="a")
    )
    await repos.agents.archive("dispatcher")  # supervisor retired

    child_id = await repos.tasks.create(
        TaskSpec(
            agent_id="worker", goal="lonely child", acceptance="a",
            parent_task_id=parent_id,
        )
    )

    fake = FakeAdapter()
    fake.push_done(summary="done despite missing parent")
    scheduler = _build_scheduler(repos, cfg, fake)
    await scheduler._run_task_inline(child_id)

    batch = await repos.outbox.dequeue_batch(limit=10)
    terminated = [
        r for r in batch
        if r.payload.get("metadata", {}).get("kind") == "task_terminated"
    ]
    assert len(terminated) == 1
    assert terminated[0].payload["recipient"] == "owner"


# ---------------------------------------------------------------------------
# Idempotency via external_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_external_id_format_is_deterministic_per_task(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    """external_id is ``task_terminated:<task_id>`` so the outbox
    dispatcher's UNIQUE-on-external_id constraint dedupes if anything
    ever fires the helper twice for the same task (retried tick,
    crash recovery, etc.)."""
    cfg = _make_config(tmp_path)
    await _seed(repos)
    task_id = await repos.tasks.create(
        TaskSpec(agent_id="worker", goal="g", acceptance="a")
    )

    fake = FakeAdapter()
    fake.push_done(summary="ok")
    scheduler = _build_scheduler(repos, cfg, fake)
    await scheduler._run_task_inline(task_id)

    batch = await repos.outbox.dequeue_batch(limit=10)
    terminated = [
        r for r in batch
        if r.payload.get("metadata", {}).get("kind") == "task_terminated"
    ]
    assert len(terminated) == 1
    assert terminated[0].external_id == f"task_terminated:{task_id}"


# ---------------------------------------------------------------------------
# Title carries task goal preview for quick inbox triage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_title_includes_outcome_and_goal_preview(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    """Supervisor's inbox listing shows the title; it should make
    triage possible at a glance — outcome + first ~60 chars of goal."""
    cfg = _make_config(tmp_path)
    await _seed(repos)
    long_goal = (
        "investigate the auth migration regression that "
        "appeared after the v4 rollout last night"
    )
    task_id = await repos.tasks.create(
        TaskSpec(agent_id="worker", goal=long_goal, acceptance="a")
    )

    fake = FakeAdapter()
    fake.push_done(summary="root-caused")
    scheduler = _build_scheduler(repos, cfg, fake)
    await scheduler._run_task_inline(task_id)

    batch = await repos.outbox.dequeue_batch(limit=10)
    terminated = [
        r for r in batch
        if r.payload.get("metadata", {}).get("kind") == "task_terminated"
    ]
    title = terminated[0].payload["title"]
    assert title.startswith("[task-terminated:completed] ")
    # First 60 chars of the goal land in the title (truncated, not full).
    assert "investigate the auth migration regression" in title
    assert len(title) <= 100  # bound for inbox listings


@pytest.mark.asyncio
async def test_delivered_message_preserves_title_round_trip(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    """Round-trip the mail through the outbox dispatcher and assert the
    DELIVERED mailbox_messages row carries our title — not the
    auto-derived-from-body-first-line fallback.

    Regression guard: the payload-level assertions above don't catch
    the dispatcher dropping `title` on insert (it used to), because
    they only inspect the outbox row. This test runs the actual
    delivery so the title contract is verified end-to-end.
    """
    from lyre.outbox.dispatcher import OutboxDispatcher

    cfg = _make_config(tmp_path)
    await _seed(repos)
    task_id = await repos.tasks.create(
        TaskSpec(agent_id="worker", goal="ship the thing", acceptance="a")
    )

    fake = FakeAdapter()
    fake.push_done(summary="shipped")
    scheduler = _build_scheduler(repos, cfg, fake)
    await scheduler._run_task_inline(task_id)

    # Deliver: outbox → mailbox_messages.
    dispatcher = OutboxDispatcher(repos)
    await dispatcher.tick()

    # Owner is the recipient (top-level task). Fetch the delivered row
    # by its deterministic external_id and check the persisted title.
    external_id = f"task_terminated:{task_id}"
    msg_id = await repos.mailbox.find_id_by_external_id("owner", external_id)
    assert msg_id is not None, "task_terminated mail was not delivered to owner"
    delivered = await repos.mailbox.get_message(msg_id)
    assert delivered is not None
    assert delivered.title == "[task-terminated:completed] ship the thing", (
        f"delivered title should be the supervisor-triage subject, not "
        f"the auto-derived body line; got {delivered.title!r}"
    )
    # metadata round-trips too (supervisor personas pattern-match on it).
    assert delivered.metadata is not None
    assert delivered.metadata["kind"] == "task_terminated"
    assert delivered.metadata["outcome"] == "completed"
