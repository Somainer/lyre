"""Task tools: dispatch_task (subagent), query_task_status."""

from __future__ import annotations

from typing import Any

from ...persistence.models import GitContext, TaskSpec
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

    spec = TaskSpec(
        agent_id=resolved_agent_id,
        persona_name=resolved_persona,
        goal=goal,
        acceptance=acceptance,
        parent_task_id=ctx.task_id,
        lease_duration_s=int(args.get("lease_duration_s", 1800)),
        deadline=None,
        metadata=metadata,
        git_context=git_ctx,
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
    description="Look up a task's current status and checkpoint.",
    input_schema={
        "type": "object",
        "properties": {"task_id": {"type": "string"}},
        "required": ["task_id"],
    },
    handler=_query_task_status,
)
