"""Skills proposal → approval → reuse loop, end-to-end through the Scheduler.

Tests the dogfood path that closes Lyre's plugin feedback loop (B1 / PI
Agent Skills standard):

  Worker (task #1)
    → python_exec writes ~/.lyre/skills/proposed/<name>/SKILL.md
    → mailbox_send to leader "I proposed skill <name>"

  Reviewer-skill (task #2; in production dispatched by leader)
    → shell_exec mv proposed/<name>  approved/<name>   (or rm -r to reject)

  Worker (task #3) — fresh wakeup
    → assemble_system_prompt finds the approved skill at
      ~/.lyre/skills/approved/<name>/SKILL.md and adds it to the
      <available_skills> XML block
    → the location attribute points at SKILL.md so the next worker can
      read the body via read_memory / file-read tool
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lyre.adapter.llm_adapter import (
    ContentDelta,
    ToolUseComplete,
    TurnComplete,
)
from lyre.config import Config
from lyre.persistence.models import Persona, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.memory import ensure_skeleton
from lyre.runtime.skills import ensure_skills_skeleton
from lyre.scheduler.scheduler import Scheduler

from .fake_adapter import FakeAdapter
from .helpers import fake_entry, fake_registry


def _config(tmp_path: Path) -> tuple[Config, Path]:
    """Returns (cfg, lyre_home). memory_root = lyre_home/memory; skills =
    lyre_home/skills."""
    lyre_home = tmp_path / ".lyre"
    mem = lyre_home / "memory"
    ensure_skeleton(mem)
    ensure_skills_skeleton(lyre_home)
    return (
        Config(
            db_path=tmp_path / "x.db",
            object_store_path=tmp_path / "objstore",
            memory_path=mem,
            anthropic_api_key="fake",
            anthropic_base_url=None,
            default_model="m",
            model_override=None,
        ),
        lyre_home,
    )


async def _seed_personas(repos: SqliteRepositories) -> None:
    await repos.personas.upsert(
        Persona(
            name="worker-maintainer",
            role_description="worker",
            system_prompt="you write code",
            allowed_lyre_tools=[
                "python_exec", "shell_exec", "mailbox_send", "report_progress",
            ],
            model_preference={
                "tier": "workhorse", "requires": ["tool_use"], "prefer": [],
            },
            needs_worktree=True,
        )
    )
    await repos.personas.upsert(
        Persona(
            name="reviewer-skill",
            role_description="skill reviewer",
            system_prompt="you review skills",
            allowed_lyre_tools=["shell_exec", "mailbox_send"],
            model_preference={
                "tier": "workhorse", "requires": ["tool_use"], "prefer": [],
            },
            needs_worktree=False,
        )
    )
    await repos.personas.upsert(
        Persona(
            name="leader", role_description="leader",
            system_prompt="l", allowed_lyre_tools=["mailbox_send"],
            model_preference={
                "tier": "flagship", "requires": ["tool_use"], "prefer": [],
            },
            needs_worktree=False,
        )
    )
    # Post-A3: scheduler/router/Phase 0 all key off agents.
    await repos.agents.create(agent_id="leader", persona_name="leader")
    await repos.agents.create(agent_id="owner", persona_name="leader")
    await repos.agents.create(
        agent_id="worker-1", persona_name="worker-maintainer"
    )
    await repos.agents.create(
        agent_id="reviewer-1", persona_name="reviewer-skill"
    )
    await repos.mailbox.ensure_mailbox("owner")
    await repos.mailbox.ensure_mailbox("leader")


def _registry():
    return fake_registry(
        fake_entry(id="m-workhorse", tier="workhorse"),
        fake_entry(id="m-flagship", tier="flagship"),
    )


def _proposed_dir(lyre_home: Path, name: str) -> Path:
    return lyre_home / "skills" / "proposed" / name


def _approved_dir(lyre_home: Path, name: str) -> Path:
    return lyre_home / "skills" / "approved" / name


def _make_proposal_code(target_dir: Path) -> str:
    """python_exec body the worker would run to write a PI-style skill
    (a directory containing SKILL.md)."""
    return f"""
import pathlib
d = pathlib.Path({str(target_dir)!r})
d.mkdir(parents=True, exist_ok=True)
(d / "SKILL.md").write_text('''---
name: {target_dir.name}
description: How to add a builtin function to lisa-lang
scope: global
---

# Add a lisa-lang builtin

1. Open src/main/scala/BuiltInFunctions.scala
2. Add a `case "fnname" =>` arm returning a Value
3. Add a test in src/test/lisa/builtins.lisa
4. Run `sbt test`
''')
print('proposal written')
"""


# ---------------------------------------------------------------------------
# Core dogfood test: propose → approve → next worker sees skill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skill_proposal_approve_reuse_loop(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    cfg, lyre_home = _config(tmp_path)
    cfg.object_store_path.mkdir(parents=True, exist_ok=True)
    await _seed_personas(repos)

    name = "add-lisa-builtin"
    proposed_d = _proposed_dir(lyre_home, name)
    approved_d = _approved_dir(lyre_home, name)

    # === TICK 1: worker proposes ===
    worker1_task = await repos.tasks.create(
        TaskSpec(
            agent_id="worker-1",
            goal="implement square builtin in lisa-lang",
            acceptance="square works + tested + PR open",
        )
    )

    worker_adapter1 = FakeAdapter()
    worker_adapter1.push_turn([
        ToolUseComplete(
            id="w1-t1", name="python_exec",
            input={"code": _make_proposal_code(proposed_d)},
        ),
        TurnComplete(stop_reason="tool_use"),
    ])
    worker_adapter1.push_turn([
        ToolUseComplete(
            id="w1-t2", name="mailbox_send",
            input={
                "to": "leader",
                "body": f"I proposed skill {name}, please request review.",
            },
        ),
        TurnComplete(stop_reason="tool_use"),
    ])
    worker_adapter1.push_turn([
        ContentDelta(text="proposal sent"),
        TurnComplete(stop_reason="end_turn"),
    ])

    scheduler1 = Scheduler(
        repos, cfg, poll_interval_s=0.05,
        registry=_registry(),
        adapter_for_test=lambda e: worker_adapter1,
    )
    await scheduler1._tick()

    skill_md = proposed_d / "SKILL.md"
    assert skill_md.exists(), f"proposed SKILL.md missing: {skill_md}"
    assert not approved_d.exists()
    body = skill_md.read_text()
    assert "How to add a builtin function to lisa-lang" in body

    t = await repos.tasks.get(worker1_task)
    assert t is not None and t.status == "completed"

    # === TICK 2: reviewer-skill approves ===
    reviewer_task = await repos.tasks.create(
        TaskSpec(
            agent_id="reviewer-1",
            goal=f"review the proposed skill named {name}",
            acceptance=f"{name}/ moved to approved/ or removed",
        )
    )

    reviewer_adapter = FakeAdapter()
    reviewer_adapter.push_turn([
        ToolUseComplete(
            id="r1", name="shell_exec",
            input={"argv": ["cat", str(skill_md)]},
        ),
        TurnComplete(stop_reason="tool_use"),
    ])
    reviewer_adapter.push_turn([
        ToolUseComplete(
            id="r2", name="shell_exec",
            input={"argv": ["mv", str(proposed_d), str(approved_d)]},
        ),
        TurnComplete(stop_reason="tool_use"),
    ])
    reviewer_adapter.push_turn([
        ToolUseComplete(
            id="r3", name="mailbox_send",
            input={
                "to": "leader",
                "body": f"approved skill {name} — moved to approved/",
            },
        ),
        TurnComplete(stop_reason="tool_use"),
    ])
    reviewer_adapter.push_turn([
        ContentDelta(text="approved"),
        TurnComplete(stop_reason="end_turn"),
    ])

    scheduler2 = Scheduler(
        repos, cfg, poll_interval_s=0.05,
        registry=_registry(),
        adapter_for_test=lambda e: reviewer_adapter,
    )
    await scheduler2._tick()

    assert not proposed_d.exists(), "proposal dir should have been moved"
    assert (approved_d / "SKILL.md").exists(), "approved SKILL.md should exist after mv"

    t = await repos.tasks.get(reviewer_task)
    assert t is not None and t.status == "completed"

    # === TICK 3: a NEW worker wakeup must see the approved skill ===
    await repos.tasks.create(
        TaskSpec(
            agent_id="worker-1",
            goal="add another builtin (cube)",
            acceptance="cube works",
        )
    )
    worker_adapter2 = FakeAdapter()
    worker_adapter2.push_turn([
        ContentDelta(text="ok using approved skill"),
        TurnComplete(stop_reason="end_turn"),
    ])
    scheduler3 = Scheduler(
        repos, cfg, poll_interval_s=0.05,
        registry=_registry(),
        adapter_for_test=lambda e: worker_adapter2,
    )
    await scheduler3._tick()

    # The system prompt must include the approved skill in the
    # <available_skills> XML block.
    assert worker_adapter2.calls, "scheduler never called the adapter"
    system_prompt = worker_adapter2.calls[0]["system"]
    assert system_prompt is not None
    assert "<available_skills>" in system_prompt
    assert f"<name>{name}</name>" in system_prompt
    assert "How to add a builtin function to lisa-lang" in system_prompt
    # location attribute should point at the SKILL.md so the agent can read it
    assert str(approved_d / "SKILL.md") in system_prompt
    # Proposed skills must NOT appear in the menu — they're under review.
    assert "proposed" not in system_prompt or "skills/proposed" not in system_prompt


# ---------------------------------------------------------------------------
# Rejection path: reviewer rm -r's the dir; next worker doesn't see it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skill_rejection_removes_proposal_and_hides_from_index(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    cfg, lyre_home = _config(tmp_path)
    cfg.object_store_path.mkdir(parents=True, exist_ok=True)
    await _seed_personas(repos)

    name = "too-specific"
    proposed_d = _proposed_dir(lyre_home, name)
    approved_d = _approved_dir(lyre_home, name)

    # Tick 1: worker writes a proposal
    await repos.tasks.create(
        TaskSpec(agent_id="worker-1", goal="g", acceptance="a")
    )
    worker_adapter = FakeAdapter()
    worker_adapter.push_turn([
        ToolUseComplete(
            id="t1", name="python_exec",
            input={"code": _make_proposal_code(proposed_d)},
        ),
        TurnComplete(stop_reason="tool_use"),
    ])
    worker_adapter.push_turn([
        ContentDelta(text="done"), TurnComplete(stop_reason="end_turn"),
    ])
    scheduler1 = Scheduler(
        repos, cfg, poll_interval_s=0.05,
        registry=_registry(),
        adapter_for_test=lambda e: worker_adapter,
    )
    await scheduler1._tick()
    assert proposed_d.exists()

    # Tick 2: reviewer rejects → rm -r + mailbox notify
    await repos.tasks.create(
        TaskSpec(
            agent_id="reviewer-1", goal=f"review {name}", acceptance="a",
        )
    )
    reviewer_adapter = FakeAdapter()
    reviewer_adapter.push_turn([
        ToolUseComplete(id="r1", name="shell_exec",
                        input={"argv": ["rm", "-r", str(proposed_d)]}),
        TurnComplete(stop_reason="tool_use"),
    ])
    reviewer_adapter.push_turn([
        ToolUseComplete(id="r2", name="mailbox_send",
                        input={"to": "leader",
                               "body": f"rejected {name}: too task-specific"}),
        TurnComplete(stop_reason="tool_use"),
    ])
    reviewer_adapter.push_turn([
        ContentDelta(text="rejected"), TurnComplete(stop_reason="end_turn"),
    ])
    scheduler2 = Scheduler(
        repos, cfg, poll_interval_s=0.05,
        registry=_registry(),
        adapter_for_test=lambda e: reviewer_adapter,
    )
    await scheduler2._tick()

    assert not proposed_d.exists()
    assert not approved_d.exists()

    # Tick 3: next worker doesn't see the skill anywhere
    await repos.tasks.create(
        TaskSpec(agent_id="worker-1", goal="next", acceptance="a")
    )
    next_adapter = FakeAdapter()
    next_adapter.push_turn([
        ContentDelta(text="ok"), TurnComplete(stop_reason="end_turn"),
    ])
    scheduler3 = Scheduler(
        repos, cfg, poll_interval_s=0.05,
        registry=_registry(),
        adapter_for_test=lambda e: next_adapter,
    )
    await scheduler3._tick()
    system_prompt = next_adapter.calls[0]["system"]
    assert name not in system_prompt


# ---------------------------------------------------------------------------
# Approved-only: proposed skills never leak into the menu
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proposed_skills_never_appear_in_prompt(
    repos: SqliteRepositories, tmp_path: Path,
) -> None:
    """B1 design choice: only `approved/` skills are surfaced in the
    <available_skills> XML block. Proposed = under review. This stops the
    model from invoking half-baked playbooks before a reviewer has
    sanity-checked them."""
    cfg, lyre_home = _config(tmp_path)
    cfg.object_store_path.mkdir(parents=True, exist_ok=True)
    await _seed_personas(repos)

    # By hand: one approved skill and one proposed skill.
    approved_d = _approved_dir(lyre_home, "approved-one")
    approved_d.mkdir(parents=True, exist_ok=True)
    (approved_d / "SKILL.md").write_text(
        "---\nname: approved-one\ndescription: Already-approved skill\n---\n\nbody"
    )
    proposed_d = _proposed_dir(lyre_home, "pending-one")
    proposed_d.mkdir(parents=True, exist_ok=True)
    (proposed_d / "SKILL.md").write_text(
        "---\nname: pending-one\ndescription: Still under review\n---\n\nbody"
    )

    await repos.tasks.create(
        TaskSpec(agent_id="worker-1", goal="g", acceptance="a")
    )
    fake = FakeAdapter()
    fake.push_turn([ContentDelta(text="ok"), TurnComplete(stop_reason="end_turn")])
    scheduler = Scheduler(
        repos, cfg, poll_interval_s=0.05,
        registry=_registry(),
        adapter_for_test=lambda e: fake,
    )
    await scheduler._tick()

    sp = fake.calls[0]["system"]
    assert "<available_skills>" in sp
    assert "approved-one" in sp
    # Proposed skill must not appear under any guise.
    assert "pending-one" not in sp
