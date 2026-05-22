"""Task tools: dispatch_task (subagent), query_task_status."""

from __future__ import annotations

from typing import Any

from ...persistence.models import TaskSpec
from . import Tool, ToolContext, ToolError


async def _dispatch_task(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Assign a new task to an existing agent.

    Post-A3 signature: target is `agent` (an existing agent_id). The legacy
    `persona` keyword is still accepted for back-compat, but only when it
    resolves to a unique non-archived agent of that persona (otherwise the
    caller must pick which one explicitly). Use `create_agent` first if no
    suitable agent exists.
    """
    goal = args.get("goal")
    acceptance = args.get("acceptance")
    if not goal or not isinstance(goal, str):
        raise ToolError("goal required")
    if not acceptance or not isinstance(acceptance, str):
        raise ToolError("acceptance required (verifiable criteria)")

    agent_id = args.get("agent")
    persona_arg = args.get("persona")
    if agent_id is not None:
        if not isinstance(agent_id, str):
            raise ToolError("agent must be a string (agent_id)")
        agent = await ctx.repos.agents.get(agent_id)
        if agent is None or agent.status == "archived":
            raise ToolError(
                f"agent {agent_id!r} not found (or archived). "
                f"Use list_agents() to see live agents, or create_agent() "
                f"to make a new one."
            )
        resolved_agent_id = agent_id
        resolved_persona = agent.persona_name
    elif persona_arg is not None:
        if not isinstance(persona_arg, str):
            raise ToolError("persona must be a string")
        p = await ctx.repos.personas.get(persona_arg)
        if p is None or p.status != "approved":
            raise ToolError(
                f"persona '{persona_arg}' not found or not approved"
            )
        candidates = await ctx.repos.agents.list_by_persona(persona_arg)
        if not candidates:
            raise ToolError(
                f"no live agent of persona {persona_arg!r}. Call "
                f"create_agent(persona='{persona_arg}') first, then "
                f"dispatch_task(agent='<new id>', ...)."
            )
        if len(candidates) > 1:
            ids = [a.id for a in candidates]
            raise ToolError(
                f"persona {persona_arg!r} has multiple agents {ids}; "
                f"pass `agent=<id>` explicitly to disambiguate."
            )
        resolved_agent_id = candidates[0].id
        resolved_persona = persona_arg
    else:
        raise ToolError(
            "either `agent` (preferred) or `persona` is required"
        )

    metadata = args.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise ToolError("metadata must be an object")

    spec = TaskSpec(
        agent_id=resolved_agent_id,
        persona_name=resolved_persona,
        goal=goal,
        acceptance=acceptance,
        parent_task_id=ctx.task_id,
        lease_duration_s=int(args.get("lease_duration_s", 1800)),
        deadline=None,
        metadata=metadata,
    )
    new_task_id = await ctx.repos.tasks.create(spec)
    return {
        "task_id": new_task_id,
        "agent": resolved_agent_id,
        "persona": resolved_persona,
        "status": "pending",
    }


async def _query_task_status(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    task_id = args.get("task_id")
    if not task_id or not isinstance(task_id, str):
        raise ToolError("task_id required")
    t = await ctx.repos.tasks.get(task_id)
    if t is None:
        raise ToolError(f"task '{task_id}' not found")
    return {
        "id": t.id,
        "persona": t.persona_name,
        "status": t.status,
        "checkpoint": t.checkpoint,
        "parent_task_id": t.parent_task_id,
    }


_TERMINAL = {"completed", "failed", "cancelled"}


async def _await_subagents(
    ctx: ToolContext, args: dict[str, Any]
) -> dict[str, Any]:
    """Yield this wakeup until all subagent children terminate.

    Marks the current task `status='needs_input'` and records the list of
    children we're waiting on in the task checkpoint. The scheduler's
    `find_parents_ready_to_wake` query will see this task once all its
    children land in terminal status, and re-pend it for a fresh wakeup.

    Returns immediately (without yielding) if all children are already
    terminal — the agent can then continue and consume their results in the
    same turn.

    Note from the patterns elsewhere in the codebase: this is the
    "wakeup pauses here" tool. After this returns `status='awaiting'`,
    the agent should stop calling tools — the wakeup will close
    naturally on the next no-tool response. The scheduler respects
    needs_input and won't run this task again until the children
    finish, at which point it dispatches a fresh wakeup.
    """
    children = await ctx.repos.tasks.find_children(ctx.task_id)
    if not children:
        raise ToolError(
            "no subagent children found for this task; "
            "dispatch_task first, then await_subagents"
        )

    summary = [
        {"id": c.id, "persona": c.persona_name, "status": c.status}
        for c in children
    ]
    pending = [c for c in summary if c["status"] not in _TERMINAL]

    if not pending:
        # All done already — no yielding needed, agent can use the results.
        return {
            "status": "all_done",
            "children": summary,
        }

    # Persist intent + transition. The current wakeup holds the lease, so the
    # checkpoint update succeeds. Status update advances to needs_input; the
    # scheduler's post-loop logic detects this and won't overwrite it back
    # to completed/failed.
    current_task = await ctx.repos.tasks.get(ctx.task_id)
    # The caller holds the lease — the row must exist.
    assert current_task is not None  # noqa: S101 — narrows for mypy
    existing_checkpoint = current_task.checkpoint or {}
    new_checkpoint = {
        **existing_checkpoint,
        "awaiting_children": [c["id"] for c in pending],
    }
    await ctx.repos.tasks.update_checkpoint(
        ctx.task_id, new_checkpoint, ctx.wakeup_id
    )
    await ctx.repos.tasks.update_status(ctx.task_id, "needs_input")
    return {
        "status": "awaiting",
        "waiting_for": pending,
        "children": summary,
        "note": (
            "Your task has been marked needs_input. End this turn; the "
            "scheduler will wake you when all children terminate."
        ),
    }


DISPATCH_TASK = Tool(
    name="dispatch_task",
    description=(
        "Create a child task for an existing agent. Returns the new task_id; "
        "the scheduler will pick it up. Use query_task_status() to poll. "
        "If no suitable agent exists yet, call create_agent() first, then "
        "pass its id as `agent`."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "agent": {
                "type": "string",
                "description": (
                    "Target AGENT ID (from list_agents()). The preferred "
                    "way to identify a dispatch target."
                ),
            },
            "persona": {
                "type": "string",
                "description": (
                    "Legacy / convenience: target persona name. Resolved to "
                    "an agent only if exactly one live agent of that persona "
                    "exists. Otherwise prefer `agent`."
                ),
            },
            "goal": {"type": "string"},
            "acceptance": {
                "type": "string",
                "description": "Verifiable acceptance criteria — what 'done' means.",
            },
            "lease_duration_s": {"type": "integer", "default": 1800},
            "metadata": {"type": "object"},
        },
        "required": ["goal", "acceptance"],
    },
    handler=_dispatch_task,
)

QUERY_TASK_STATUS = Tool(
    name="query_task_status",
    description="Look up a task's current status and checkpoint.",
    input_schema={
        "type": "object",
        "properties": {"task_id": {"type": "string"}},
        "required": ["task_id"],
    },
    handler=_query_task_status,
)


AWAIT_SUBAGENTS = Tool(
    name="await_subagents",
    description=(
        "Yield this wakeup until ALL subagent children you dispatched "
        "earlier terminate (completed/failed/cancelled). Use this AFTER "
        "dispatch_task calls; after it returns status='awaiting', just "
        "stop calling tools and the wakeup will close. If all children "
        "are already terminal when called, returns immediately so you "
        "can consume their results in this same wakeup. Otherwise, your "
        "task is marked needs_input and the scheduler will wake you "
        "when they're all done — your next wakeup will see their "
        "statuses in the initial user message."
    ),
    input_schema={
        "type": "object",
        "properties": {},
    },
    handler=_await_subagents,
)
