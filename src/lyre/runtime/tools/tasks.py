"""Task tools: dispatch_task (subagent), query_task_status."""

from __future__ import annotations

from typing import Any

from ...persistence.models import FanInMember, GitContext, TaskSpec
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

    # Fan-in: dispatch this child INTO a barrier group. We validate the group
    # is open + the slot is free here, then (below) write the child task and
    # its roster row in ONE transaction so a crash can't leave a child that
    # owns no leg (its result-mail would then fail the lineage check forever).
    fan_in_arg = args.get("fan_in")
    fan_in_slot: tuple[str, int] | None = None
    if fan_in_arg is not None:
        if not isinstance(fan_in_arg, dict):
            raise ToolError("fan_in must be an object {group_id, leg_key}")
        fg = fan_in_arg.get("group_id")
        lk = fan_in_arg.get("leg_key")
        if not isinstance(fg, str) or not fg:
            raise ToolError("fan_in.group_id required (string)")
        if not isinstance(lk, int) or lk < 0:
            raise ToolError("fan_in.leg_key required (non-negative integer)")
        group = await ctx.repos.fan_in.get(fg)
        if group is None or group.status != "open":
            raise ToolError(
                f"fan-in group {fg!r} is not open; cannot dispatch into it"
            )
        if await ctx.repos.fan_in.get_member(fg, lk) is not None:
            raise ToolError(f"leg_key={lk} already taken in fan-in group {fg!r}")
        # Stamp the child so it knows which barrier/leg it serves and what
        # shape its result must take.
        metadata = {
            **(metadata or {}),
            "fan_in_group": fg,
            "leg_key": lk,
            "result_schema": group.result_schema,
        }
        fan_in_slot = (fg, lk)

    # Inherit the dispatching wakeup's 主线 so the child task — and every wakeup
    # it runs — stays on-thread. Mechanical; the agent never sets it by hand.
    if ctx.thread_id is not None and (metadata is None or "thread_id" not in metadata):
        metadata = {**(metadata or {}), "thread_id": ctx.thread_id}

    git_ctx_arg = args.get("git_context")
    git_ctx: GitContext | None = None
    if git_ctx_arg is not None:
        if not isinstance(git_ctx_arg, dict):
            raise ToolError(
                "git_context must be an object "
                "{repo_url, target_branch, base_branch?}"
            )
        repo_url = git_ctx_arg.get("repo_url")
        target_branch = git_ctx_arg.get("target_branch")
        if not isinstance(repo_url, str) or not repo_url:
            raise ToolError("git_context.repo_url required (string)")
        if not isinstance(target_branch, str) or not target_branch:
            raise ToolError("git_context.target_branch required (string)")
        base_branch = git_ctx_arg.get("base_branch", "main")
        if not isinstance(base_branch, str):
            raise ToolError("git_context.base_branch must be a string")
        git_ctx = GitContext(
            repo_url=repo_url,
            target_branch=target_branch,
            base_branch=base_branch,
        )

    raw_lease = args.get("lease_duration_s", 1800)
    try:
        lease_duration_s = int(raw_lease)
    except (TypeError, ValueError):
        raise ToolError(f"lease_duration_s must be an integer (got {raw_lease!r})") from None

    spec = TaskSpec(
        agent_id=resolved_agent_id,
        persona_name=resolved_persona,
        goal=goal,
        acceptance=acceptance,
        parent_task_id=ctx.task_id,
        lease_duration_s=lease_duration_s,
        deadline=None,
        metadata=metadata,
        git_context=git_ctx,
    )
    if fan_in_slot is not None:
        # Atomic: the child task and its roster slot land together. Both
        # writes auto-suppress their own commit inside the transaction block.
        async with ctx.repos.transaction():
            new_task_id = await ctx.repos.tasks.create(spec)
            await ctx.repos.fan_in.add_member(
                FanInMember(
                    group_id=fan_in_slot[0],
                    leg_key=fan_in_slot[1],
                    child_task_id=new_task_id,
                    child_agent_id=resolved_agent_id,
                )
            )
    else:
        new_task_id = await ctx.repos.tasks.create(spec)
    return {
        "task_id": new_task_id,
        "agent": resolved_agent_id,
        "persona": resolved_persona,
        "status": "pending",
        "fan_in_group": fan_in_slot[0] if fan_in_slot else None,
    }


async def _query_task_status(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    task_id = args.get("task_id")
    if not task_id or not isinstance(task_id, str):
        raise ToolError("task_id required")
    t = await ctx.repos.tasks.get(task_id)
    if t is None:
        raise ToolError(f"task '{task_id}' not found")
    # "Is it running?" is answered by an OPEN wakeup row (ended_at IS NULL), not
    # by task.status — a 'completed' task with no open wakeup is not running.
    # Returning this (plus the children it spawned) is the 019e8d7d fix: a
    # coordinator gets evidence instead of inferring run-state from memory.
    wakeups = await ctx.repos.wakeups.list_for_task(task_id, limit=5)
    active = next((w for w in wakeups if w.ended_at is None), None)
    children = await ctx.repos.tasks.find_children(task_id)
    return {
        "id": t.id,
        "persona": t.persona_name,
        "agent_id": t.agent_id,
        "status": t.status,
        "is_running": active is not None,
        "active_wakeup_id": active.id if active else None,
        # True if this is the very wakeup you're asking from — your own running
        # session, not delegated work. (Same guard as list_tasks.)
        "is_current_wakeup": t.id == (ctx.task_id or None),
        "checkpoint": t.checkpoint,
        "parent_task_id": t.parent_task_id,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        "lease_holder": t.lease_holder,
        "lease_until": t.lease_until.isoformat() if t.lease_until else None,
        "children": [
            {"id": c.id, "persona": c.persona_name, "agent_id": c.agent_id, "status": c.status}
            for c in children
        ],
        "recent_wakeups": [
            {
                "id": w.id,
                "started_at": w.started_at.isoformat() if w.started_at else None,
                "ended_at": w.ended_at.isoformat() if w.ended_at else None,
                "end_status": w.end_status,
                "transcript_uri": w.transcript_uri,
            }
            for w in wakeups
        ],
    }


DISPATCH_TASK = Tool(
    name="dispatch_task",
    description=(
        "Create a child task for an existing agent. Returns the new task_id; "
        "the scheduler will pick it up. Use query_task_status() to poll. "
        "If no suitable agent exists yet, call create_agent() first, then "
        "pass its id as `agent`. "
        "\n\n"
        "**git_context** (optional): if this task needs the worker to "
        "operate on a git working copy (code change → push → PR), pass "
        "``git_context={'repo_url': ..., 'target_branch': ..., "
        "'base_branch': 'main'}``. The runtime provisions an SSH key + "
        "agent and clones the repo onto the worker's worktree before "
        "the worker wakes up. **Omit git_context** for non-code tasks "
        "(research, skill migration, data shaping, log parsing) — the "
        "worker gets a clean tmpdir sandbox with no git binding."
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
            "fan_in": {
                "type": "object",
                "description": (
                    "Dispatch this child into a fan-in barrier opened with "
                    "fan_in_open(). The child must return its result via "
                    "mailbox_send(result_for=group_id, leg_key=…, result=…). "
                    "Each leg_key must be unique within the group."
                ),
                "properties": {
                    "group_id": {"type": "string"},
                    "leg_key": {
                        "type": "integer",
                        "description": "This child's slot, 0..expect_replies-1.",
                    },
                },
                "required": ["group_id", "leg_key"],
            },
            "git_context": {
                "type": "object",
                "description": (
                    "Optional. Provision a git working copy on the "
                    "worker's worktree before it wakes up. Required "
                    "for code-edit tasks; omit for non-git work."
                ),
                "properties": {
                    "repo_url": {
                        "type": "string",
                        "description": (
                            "ssh:// or https:// URL of the repo. "
                            "SSH preferred; runtime generates the key."
                        ),
                    },
                    "target_branch": {
                        "type": "string",
                        "description": (
                            "Branch to check out (will be created from "
                            "base_branch). Conventionally semantic, "
                            "e.g. 'claude/<feature>'."
                        ),
                    },
                    "base_branch": {
                        "type": "string",
                        "default": "main",
                        "description": "Branch to clone from. Defaults to 'main'.",
                    },
                },
                "required": ["repo_url", "target_branch"],
            },
        },
        "required": ["goal", "acceptance"],
    },
    handler=_dispatch_task,
)

QUERY_TASK_STATUS = Tool(
    name="query_task_status",
    description=(
        "Look up a task by id: status, agent_id, is_running (true ONLY if an "
        "open wakeup exists — not merely status), the children it dispatched, "
        "checkpoint, and recent wakeups. Verify by id here before telling "
        "anyone a task is 'running'; status alone is not run-state."
    ),
    input_schema={
        "type": "object",
        "properties": {"task_id": {"type": "string"}},
        "required": ["task_id"],
    },
    handler=_query_task_status,
)
