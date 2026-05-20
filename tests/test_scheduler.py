"""Integration tests for Scheduler with FakeAdapter (no LLM API needed)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from lyre.adapter.llm_adapter import (
    ContentDelta,
    ToolUseComplete,
    TurnComplete,
    Usage,
)
from lyre.config import Config
from lyre.outbox.dispatcher import OutboxDispatcher
from lyre.persistence.models import Persona, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.scheduler.scheduler import Scheduler

from .fake_adapter import FakeAdapter
from .helpers import fake_entry, fake_registry


def _make_config(tmp_path: Path, *, model_override: str | None = None) -> Config:
    mem = tmp_path / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    return Config(
        db_path=tmp_path / "x.db",
        object_store_path=tmp_path / "objstore",
        memory_path=mem,
        anthropic_api_key="fake",
        anthropic_base_url=None,
        default_model="m",
        model_override=model_override,
    )


def _flagship_pref() -> dict:
    return {"tier": "flagship", "requires": ["tool_use"], "prefer": []}


def _workhorse_pref() -> dict:
    return {"tier": "workhorse", "requires": ["tool_use"], "prefer": []}


@pytest.mark.asyncio
async def test_scheduler_runs_task_with_fake_adapter(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    cfg = _make_config(tmp_path)
    cfg.object_store_path.mkdir(parents=True, exist_ok=True)

    await repos.personas.upsert(
        Persona(
            name="worker",
            role_description="worker",
            system_prompt="you write code",
            allowed_lyre_tools=["mailbox_send"],
            model_preference=_workhorse_pref(),
        )
    )
    task_id = await repos.tasks.create(
        TaskSpec(persona_name="worker", goal="say hello to owner", acceptance="msg sent")
    )

    fake = FakeAdapter()
    fake.push_turn(
        [
            ContentDelta(text="OK, sending..."),
            ToolUseComplete(
                id="tu_1", name="mailbox_send",
                input={"to": "owner", "body": "hello owner"},
            ),
            Usage(input_tokens=50, output_tokens=10),
            TurnComplete(stop_reason="tool_use"),
        ]
    )
    fake.push_turn(
        [ContentDelta(text="done."), Usage(input_tokens=60, output_tokens=2), TurnComplete(stop_reason="end_turn")]
    )

    registry = fake_registry(fake_entry(id="fake.workhorse", tier="workhorse"))
    scheduler = Scheduler(
        repos,
        cfg,
        poll_interval_s=0.05,
        registry=registry,
        adapter_for_test=lambda entry: fake,
    )

    await scheduler._tick()
    t = await repos.tasks.get(task_id)
    assert t is not None
    assert t.status == "completed"

    # The outbox got the mailbox_send.
    batch = await repos.outbox.dequeue_batch(limit=10)
    assert len(batch) == 1
    assert batch[0].kind == "mailbox_send"

    disp = OutboxDispatcher(repos)
    await disp.tick()
    owner_msgs = await repos.mailbox.read_messages("owner")
    assert any(m.body == "hello owner" for m in owner_msgs)


@pytest.mark.asyncio
async def test_scheduler_records_wakeup_metering_and_model(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    cfg = _make_config(tmp_path)
    cfg.object_store_path.mkdir(parents=True, exist_ok=True)
    await repos.personas.upsert(
        Persona(
            name="worker", role_description="w", system_prompt="w",
            model_preference=_workhorse_pref(),
        )
    )
    task_id = await repos.tasks.create(
        TaskSpec(persona_name="worker", goal="g", acceptance="a")
    )
    fake = FakeAdapter()
    fake.push_turn(
        [
            ContentDelta(text="ok"),
            Usage(input_tokens=42, output_tokens=7),
            TurnComplete(stop_reason="end_turn"),
        ]
    )
    registry = fake_registry(
        fake_entry(id="fake.workhorse", tier="workhorse", provider="anthropic")
    )
    scheduler = Scheduler(
        repos, cfg, poll_interval_s=0.05,
        registry=registry,
        adapter_for_test=lambda entry: fake,
    )
    await scheduler._tick()

    async with repos.conn.execute(
        "SELECT * FROM wakeups WHERE task_id = ?", (task_id,)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["end_status"] == "completed"
    assert row["token_input"] == 42
    assert row["token_output"] == 7
    assert row["transcript_uri"].startswith("file://")
    assert row["model"] == "fake.workhorse"
    assert row["provider"] == "anthropic"


@pytest.mark.asyncio
async def test_model_override_beats_persona_preference(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    cfg = _make_config(tmp_path, model_override="deepseek.deepseek-v4-pro")
    cfg.object_store_path.mkdir(parents=True, exist_ok=True)
    await repos.personas.upsert(
        Persona(
            name="worker", role_description="w", system_prompt="w",
            # Persona requests flagship, but override pulls us elsewhere.
            model_preference={
                "tier": "flagship",
                "requires": ["tool_use"],
                "prefer": ["anthropic.claude-opus-4-7"],
            },
        )
    )
    task_id = await repos.tasks.create(
        TaskSpec(persona_name="worker", goal="g", acceptance="a")
    )
    fake = FakeAdapter()
    fake.push_turn([ContentDelta(text="ok"), TurnComplete(stop_reason="end_turn")])
    registry = fake_registry(
        fake_entry(id="anthropic.claude-opus-4-7", tier="flagship"),
        fake_entry(id="deepseek.deepseek-v4-pro", tier="workhorse"),
    )
    scheduler = Scheduler(
        repos, cfg, poll_interval_s=0.05,
        registry=registry, adapter_for_test=lambda e: fake,
    )
    await scheduler._tick()

    # AdapterFactory.model_name_for strips the provider prefix
    assert fake.calls[0]["model"] == "deepseek-v4-pro"

    async with repos.conn.execute(
        "SELECT model FROM wakeups WHERE task_id = ?", (task_id,)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["model"] == "deepseek.deepseek-v4-pro"


@pytest.mark.asyncio
async def test_scheduler_stops_cleanly(repos: SqliteRepositories, tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    registry = fake_registry(fake_entry())
    scheduler = Scheduler(
        repos, cfg, poll_interval_s=0.02,
        registry=registry, adapter_for_test=lambda e: FakeAdapter(),
    )
    task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0.1)
    scheduler.request_stop()
    await asyncio.wait_for(task, timeout=2.0)
