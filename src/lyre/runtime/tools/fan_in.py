"""Fan-in barrier tools: fan_in_open / fan_in_status / fan_in_cancel.

The mailbox-driven workflow barrier (see docs/design/WORKFLOW_ORCHESTRATION.md).
A coordinator opens a group, dispatches N children into it (via
``dispatch_task(fan_in=...)``), then STOPS calling tools — its wakeup ends and
its task COMPLETES (it is never parked, so urgent owner mail can still wake it,
honoring the Dispatcher-not-blocked rule). Children return typed result-mails
(``mailbox_send(result_for=...)``); the scheduler's Phase 0.5 counts delivered
result-mails and, at quorum (or deadline), pings the coordinator to resume.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from ...persistence.models import FanInGroup
from . import Tool, ToolContext, ToolError

# A group must always be reapable, so a dead coordinator can't leak an open
# group forever. If the caller omits a deadline we apply this default.
_DEFAULT_DEADLINE_S = 1800
_MAX_DEADLINE_S = 24 * 3600


async def _fan_in_open(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    expect = args.get("expect_replies")
    if not isinstance(expect, int) or expect < 1:
        raise ToolError("expect_replies must be a positive integer")
    quorum = args.get("quorum", expect)
    if not isinstance(quorum, int) or not (1 <= quorum <= expect):
        raise ToolError(f"quorum must be an integer in 1..{expect} (got {quorum!r})")
    schema = args.get("result_schema")
    if not isinstance(schema, dict) or not schema:
        raise ToolError(
            "result_schema must be a non-empty JSON-Schema object; every child "
            "result-mail is validated against it at send time"
        )
    deadline_s = args.get("deadline_in_s", _DEFAULT_DEADLINE_S)
    if not isinstance(deadline_s, int) or not (1 <= deadline_s <= _MAX_DEADLINE_S):
        raise ToolError(f"deadline_in_s must be an integer in 1..{_MAX_DEADLINE_S}")
    budget = args.get("budget_tokens")
    if budget is not None and (not isinstance(budget, int) or budget < 0):
        raise ToolError("budget_tokens must be a non-negative integer if provided")

    group = FanInGroup(
        id=f"fanin-{uuid.uuid4().hex[:16]}",
        coordinator_agent_id=ctx.self_mailbox,
        parent_task_id=ctx.task_id,
        expect_replies=expect,
        quorum=quorum,
        result_schema=schema,
        budget_tokens=budget,
        deadline=datetime.now(tz=UTC) + timedelta(seconds=deadline_s),
    )
    await ctx.repos.fan_in.create_group(group)
    return {
        "group_id": group.id,
        "expect_replies": expect,
        "quorum": quorum,
        "deadline": group.deadline.isoformat(),
        "next": (
            "dispatch_task(..., fan_in={'group_id': <id>, 'leg_key': <0..N-1>}) "
            "for each child, then STOP calling tools to end your wakeup. You'll "
            "be re-woken by mail when the barrier resolves."
        ),
    }


async def _fan_in_status(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    group_id = args.get("group_id")
    if not group_id or not isinstance(group_id, str):
        raise ToolError("group_id required")
    g = await ctx.repos.fan_in.get(group_id)
    if g is None:
        raise ToolError(f"fan-in group {group_id!r} not found")
    delivered = await ctx.repos.mailbox.count_fan_in_results(
        g.coordinator_agent_id, g.id
    )
    return {
        "group_id": g.id,
        "status": g.status,
        "expect_replies": g.expect_replies,
        "quorum": g.quorum,
        "delivered": delivered,
        "deadline": g.deadline.isoformat() if g.deadline else None,
    }


async def _fan_in_cancel(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    group_id = args.get("group_id")
    if not group_id or not isinstance(group_id, str):
        raise ToolError("group_id required")
    g = await ctx.repos.fan_in.get(group_id)
    if g is None:
        raise ToolError(f"fan-in group {group_id!r} not found")
    # Guard on 'open' so a cancel races cleanly against a concurrent resolve:
    # whoever wins, the group lands in exactly one terminal state.
    cancelled = await ctx.repos.fan_in.set_status(group_id, "cancelled", guard="open")
    return {"group_id": group_id, "cancelled": cancelled, "status": g.status}


FAN_IN_OPEN = Tool(
    name="fan_in_open",
    description=(
        "Open a fan-in barrier for a parallel workflow step. Returns a "
        "group_id. Then call dispatch_task(..., fan_in={'group_id': <id>, "
        "'leg_key': k}) for each of your N children (leg_key 0..N-1) and STOP "
        "calling tools — your wakeup ends and your task COMPLETES. When `quorum` "
        "children have returned a result-mail (or the deadline passes), the "
        "scheduler delivers you a 'fan-in ready' mail that wakes you to "
        "aggregate the results. Each child returns its result via "
        "mailbox_send(result_for=<group_id>, leg_key=k, result={...}); the "
        "result is validated against `result_schema` at send time."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "expect_replies": {
                "type": "integer",
                "description": "Number of children (legs) you will dispatch.",
            },
            "quorum": {
                "type": "integer",
                "description": (
                    "Resolve once this many distinct legs have replied. "
                    "Defaults to expect_replies (await-all). Use < expect_replies "
                    "for a judge panel (e.g. majority)."
                ),
            },
            "result_schema": {
                "type": "object",
                "description": (
                    "JSON Schema every child result is validated against at "
                    "send time (fail-closed). Keep it small and typed, e.g. "
                    "{type:object, properties:{verdict:{enum:[...]}, rationale:{type:string}}}."
                ),
            },
            "deadline_in_s": {
                "type": "integer",
                "description": (
                    "Soft timeout in seconds (default 1800). Past it the barrier "
                    "resolves with whatever arrived so it never hangs forever."
                ),
            },
            "budget_tokens": {
                "type": "integer",
                "description": "Reserved for loop-until-budget (not enforced yet).",
            },
        },
        "required": ["expect_replies", "result_schema"],
    },
    handler=_fan_in_open,
)

FAN_IN_STATUS = Tool(
    name="fan_in_status",
    description=(
        "Read a fan-in group's progress: status, expected/quorum width, and how "
        "many distinct legs have delivered a result-mail so far. Counts delivered "
        "mail, not completed tasks."
    ),
    input_schema={
        "type": "object",
        "properties": {"group_id": {"type": "string"}},
        "required": ["group_id"],
    },
    handler=_fan_in_status,
)

FAN_IN_CANCEL = Tool(
    name="fan_in_cancel",
    description=(
        "Abandon an open fan-in group (e.g. you decided not to wait). Late "
        "result-mails then just sit in your inbox as ordinary low-urgency mail."
    ),
    input_schema={
        "type": "object",
        "properties": {"group_id": {"type": "string"}},
        "required": ["group_id"],
    },
    handler=_fan_in_cancel,
)
