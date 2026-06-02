"""PR5: coordinator-side fan-in aggregation + escalation-handling contracts.

PR2 built the barrier (open → dispatch legs → Phase 0.5 resolves → ready mail).
PR5 wires the CONSUMER side the dispatcher persona relies on:
- `fan_in_results(group_id)` / `read_fan_in_results` — one call returns every
  delivered leg's typed result (latest per leg) + which legs are missing, so the
  coordinator aggregates without hand-scanning low-urgency result-mails.
- the escalation-mail contract the dispatcher pattern-matches on
  (`metadata.kind="supervision_escalation"`, urgency high, deduped per agent).
- the fan-in tools are in the dispatcher's allowlist.
See docs/design/WORKFLOW_ORCHESTRATION.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import lyre.personas as personas_pkg
from lyre.config import Config
from lyre.outbox.dispatcher import OutboxDispatcher
from lyre.persistence.models import MailboxMessage, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.personas.seed import load_persona_from_file
from lyre.runtime.tools import ToolContext, ToolError
from lyre.runtime.tools.fan_in import _fan_in_open, _fan_in_results
from lyre.runtime.tools.mailbox import _mailbox_send
from lyre.runtime.tools.tasks import _dispatch_task
from lyre.scheduler.scheduler import Scheduler

from .fake_adapter import FakeAdapter
from .helpers import fake_entry, fake_registry

_SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string"}, "rationale": {"type": "string"}},
    "required": ["verdict"],
}


def _ctx(repos: SqliteRepositories, agent_id: str, task_id: str, wakeup: str = "wk") -> ToolContext:
    return ToolContext(
        repos=repos, task_id=task_id, wakeup_id=wakeup, persona_name=agent_id, agent_id=agent_id
    )


def _scheduler(repos: SqliteRepositories, tmp_path: Path) -> Scheduler:
    mem = tmp_path / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    cfg = Config(
        db_path=tmp_path / "x.db",
        object_store_path=tmp_path / "obj",
        memory_path=mem,
        anthropic_api_key="fake",
        anthropic_base_url=None,
        default_model="m",
    )
    cfg.object_store_path.mkdir(parents=True, exist_ok=True)
    return Scheduler(
        repos, cfg, registry=fake_registry(fake_entry()),
        adapter_for_test=lambda e: FakeAdapter(), auto_wake_on_mail=False,
    )


async def _open_with_legs(
    repos: SqliteRepositories, *, expect: int, quorum: int, deliver: list[int]
) -> tuple[str, ToolContext]:
    """Coordinator opens a barrier of width `expect`, dispatches `expect` legs,
    and delivers result-mails for the leg_keys in `deliver` (through the real
    outbox). Returns (group_id, coordinator ctx)."""
    await repos.agents.create("coordinator-1", "dispatcher", parent_agent_id="owner")
    coord_task = await repos.tasks.create(
        TaskSpec(agent_id="coordinator-1", goal="aggregate", acceptance="done")
    )
    cctx = _ctx(repos, "coordinator-1", coord_task)
    g = (await _fan_in_open(cctx, {"expect_replies": expect, "quorum": quorum, "result_schema": _SCHEMA}))[
        "group_id"
    ]
    for k in range(expect):
        await repos.agents.create(f"rev-{k}", "reviewer", parent_agent_id="coordinator-1")
        d = await _dispatch_task(
            cctx,
            {"agent": f"rev-{k}", "goal": "g", "acceptance": "a", "fan_in": {"group_id": g, "leg_key": k}},
        )
        if k in deliver:
            wk = await repos.wakeups.start(d["task_id"], f"rev-{k}", agent_id=f"rev-{k}")
            await _mailbox_send(
                _ctx(repos, f"rev-{k}", d["task_id"], wk),
                {
                    "body": f"rev-{k} result",
                    "result_for": g, "leg_key": k,
                    "result": {"verdict": f"v{k}", "rationale": f"r{k}"},
                    "_tool_use_id": f"tu-{k}",
                },
            )
    await OutboxDispatcher(repos).tick()  # deliver result-mails so they're readable
    return g, cctx


# --------------------------------------------------------------------------
# fan_in_results / read_fan_in_results
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fan_in_results_returns_typed_results_per_leg(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    g, cctx = await _open_with_legs(repos, expect=2, quorum=2, deliver=[0, 1])
    out = await _fan_in_results(cctx, {"group_id": g})
    assert out["delivered"] == 2
    assert out["missing_legs"] == []
    by_leg = {r["leg_key"]: r for r in out["results"]}
    assert by_leg[0]["result"] == {"verdict": "v0", "rationale": "r0"}
    assert by_leg[1]["result"] == {"verdict": "v1", "rationale": "r1"}
    assert by_leg[0]["from"] == "rev-0"


@pytest.mark.asyncio
async def test_fan_in_results_reports_missing_legs(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    # Width 3, quorum 2; only legs 0 and 2 delivered → leg 1 is missing.
    g, cctx = await _open_with_legs(repos, expect=3, quorum=2, deliver=[0, 2])
    out = await _fan_in_results(cctx, {"group_id": g})
    assert out["delivered"] == 2
    assert out["missing_legs"] == [1]
    assert sorted(r["leg_key"] for r in out["results"]) == [0, 2]


@pytest.mark.asyncio
async def test_read_fan_in_results_dedups_redelivery_latest_wins(
    repos: SqliteRepositories,
) -> None:
    # Two result-mails for the SAME leg (an idempotent redelivery / retry) must
    # collapse to one — the latest (highest id) wins.
    await repos.mailbox.ensure_mailbox("coordinator-1")
    for ext, verdict in (("a", "stale"), ("b", "fresh")):
        await repos.mailbox.insert_message(
            MailboxMessage(
                recipient="coordinator-1",
                external_id=f"res:{ext}",
                sender="rev-0",
                urgency="low",
                body="result",
                metadata={"fan_in": {"group_id": "g1", "leg_key": 0, "result": {"verdict": verdict}}},
            )
        )
    results = await repos.mailbox.read_fan_in_results("coordinator-1", "g1")
    assert len(results) == 1
    assert results[0].leg_key == 0
    assert results[0].result == {"verdict": "fresh"}  # latest


@pytest.mark.asyncio
async def test_fan_in_results_unknown_group_raises(repos: SqliteRepositories) -> None:
    ctx = _ctx(repos, "coordinator-1", "t")
    with pytest.raises(ToolError):
        await _fan_in_results(ctx, {"group_id": "nope"})


# --------------------------------------------------------------------------
# escalation-mail contract the dispatcher pattern-matches on
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_escalation_mail_high_kind_and_once_per_agent(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    await repos.agents.create("coordinator-1", "dispatcher", parent_agent_id="owner")
    await repos.agents.create(
        "reviewer/r1", "reviewer", parent_agent_id="coordinator-1",
        metadata={"supervision": {"ephemeral": True, "max_restarts": 1, "max_seconds": 60}},
    )
    tid = await repos.tasks.create(TaskSpec(agent_id="reviewer/r1", goal="g", acceptance="a"))
    await repos.tasks.update_status(tid, "failed")
    agent = await repos.agents.get("reviewer/r1")
    task = await repos.tasks.get(tid)
    assert agent is not None and task is not None
    sched = _scheduler(repos, tmp_path)

    await sched._insert_supervision_mail(agent, task, kind="escalation", urgency="high")
    msgs = await repos.mailbox.read_unread("coordinator-1", min_urgency="low", limit=50)
    assert len(msgs) == 1
    m = msgs[0]
    assert m.urgency == "high"
    assert m.sender == "system:supervisor"
    # The persona keys its handling playbook on this exact metadata.kind.
    assert (m.metadata or {}).get("kind") == "supervision_escalation"

    # Deterministic external_id → escalating the same agent again is a no-op.
    await sched._insert_supervision_mail(agent, task, kind="escalation", urgency="high")
    again = await repos.mailbox.read_all_by_recipient("coordinator-1")
    assert sum(1 for x in again if (x.metadata or {}).get("kind") == "supervision_escalation") == 1


# --------------------------------------------------------------------------
# dispatcher allowlist + registry wiring
# --------------------------------------------------------------------------
def test_dispatcher_persona_allowlists_fan_in_tools() -> None:
    shipped = Path(personas_pkg.__file__).parent / "dispatcher.md"
    persona = load_persona_from_file(shipped)
    for tool in ("fan_in_open", "fan_in_status", "fan_in_results", "fan_in_cancel"):
        assert tool in persona.allowed_lyre_tools, f"{tool} missing from dispatcher allowlist"


def test_fan_in_results_tool_registered() -> None:
    from lyre.runtime.tools.builtin import build_default_registry

    reg = build_default_registry()
    assert reg.get("fan_in_results") is not None
