"""Introspection tools — let personas (especially leader) see system state.

Terminology:
  - **persona** = role definition (one markdown file under personas/). Static.
  - **agent**   = running instance of a persona. Dynamic. One persona can
                  have many agents alive at once (e.g. 3 worker-maintainer
                  agents running in parallel share the role file but each
                  has its own id / mailbox / task queue / transcripts).
                  Agents and personas are orthogonal.

Tools:
- `read_memory(rel_path)`: read-only, sandboxed to memory_root.
- `list_personas()`: list role definitions (the roles you CAN spawn agents of).
- `list_agents(include_archived?)`: list running agent instances.
- `list_models()`: list configured LLM models + auth/health.
- `list_tasks(...)`: list current/recent task instances.
- `create_agent(...)` / `archive_agent(...)`: manage the agent population.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from . import Tool, ToolContext, ToolError

_AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

_MAX_BYTES = 64 * 1024  # 64 KiB; the index already shows description, body
# rarely needs more. Truncates with a clear marker so the model doesn't
# silently assume it has the whole thing.


def _resolve_memory_path(ctx: ToolContext, rel_path: str) -> Path:
    root_str = ctx.extras.get("memory_root")
    if not root_str:
        raise ToolError("memory_root not configured for this wakeup")
    root = Path(root_str).resolve()
    if not rel_path or not isinstance(rel_path, str):
        raise ToolError("rel_path required (string)")
    if rel_path.startswith("/") or ".." in Path(rel_path).parts:
        raise ToolError(
            f"rel_path must be relative and stay under memory_root; "
            f"got {rel_path!r}"
        )
    target = (root / rel_path).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ToolError(
            f"rel_path resolves outside memory_root: {target}"
        ) from exc
    return target


async def _read_memory(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    rel_path = args.get("rel_path")
    if not isinstance(rel_path, str):
        raise ToolError("rel_path required (string)")
    target = _resolve_memory_path(ctx, rel_path)
    if not target.exists():
        raise ToolError(f"no such memory entry: {rel_path}")
    if not target.is_file():
        raise ToolError(f"not a file: {rel_path}")
    raw = target.read_bytes()
    truncated = False
    if len(raw) > _MAX_BYTES:
        raw = raw[:_MAX_BYTES]
        truncated = True
    try:
        body = raw.decode("utf-8")
    except UnicodeDecodeError:
        body = raw.decode("utf-8", errors="replace")
    out: dict[str, Any] = {"rel_path": rel_path, "body": body}
    if truncated:
        out["truncated"] = True
        out["note"] = f"body exceeded {_MAX_BYTES} bytes; rest omitted"
    return out


READ_MEMORY = Tool(
    name="read_memory",
    description=(
        "Read the body of one entry under ~/.lyre/memory/. Read-only, "
        "sandboxed: rel_path must be relative and resolve under memory_root. "
        "Use the memory index in your system prompt to discover what's "
        "readable; then call this with the entry's rel_path."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "rel_path": {
                "type": "string",
                "description": "Path relative to memory_root, e.g. 'personas/owner.md'.",
            },
        },
        "required": ["rel_path"],
    },
    handler=_read_memory,
)


async def _list_personas(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """List PERSONAS — role definitions, not running instances.

    Note: persona and agent are orthogonal. One persona can have many
    agents (task instances) running at once. To see live agents, use
    `list_tasks(status='in_progress')`. To see ALL recent task
    instances of a persona, use `list_tasks(persona='<name>')`.
    """
    personas = await ctx.repos.personas.list_active()
    return {
        "personas": [
            {
                "name": p.name,
                "role_description": p.role_description or "",
                "needs_worktree": bool(p.needs_worktree),
            }
            for p in personas
        ],
        "count": len(personas),
        "note": (
            "These are role definitions, not running agents. Each "
            "dispatch_task() spawns a fresh agent instance of the chosen "
            "persona, and multiple instances can run in parallel. "
            "Use list_tasks() to see live agents."
        ),
    }


LIST_PERSONAS = Tool(
    name="list_personas",
    description=(
        "List approved PERSONA definitions (the roles you can dispatch "
        "to). One persona can have many agent instances running at the "
        "same time. To see live agents, use list_tasks(status='in_progress')."
    ),
    input_schema={"type": "object", "properties": {}},
    handler=_list_personas,
)


_VALID_STATUSES = {
    "pending",
    "in_progress",
    "needs_input",
    "completed",
    "failed",
    "cancelled",
}


async def _list_tasks(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    persona = args.get("persona")
    status = args.get("status")
    limit = args.get("limit", 20)
    if persona is not None and not isinstance(persona, str):
        raise ToolError("persona must be a string if provided")
    if status is not None:
        if not isinstance(status, str) or status not in _VALID_STATUSES:
            raise ToolError(
                f"status must be one of {sorted(_VALID_STATUSES)}; got {status!r}"
            )
    if not isinstance(limit, int) or limit < 1 or limit > 200:
        raise ToolError("limit must be int in [1, 200]")

    tasks = await ctx.repos.tasks.search(
        persona_name=persona, status=status, limit=limit
    )
    return {
        "tasks": [
            {
                "id": t.id,
                "persona": t.persona_name,
                "status": t.status,
                "goal": (t.goal or "")[:160],
                "parent_task_id": t.parent_task_id,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in tasks
        ],
        "count": len(tasks),
        "filters": {"persona": persona, "status": status, "limit": limit},
    }


LIST_TASKS = Tool(
    name="list_tasks",
    description=(
        "Search tasks across the whole system, optionally filtered by "
        "persona and/or status. Returns the most recent matches (default "
        "limit 20). Use this to see queue depth, in-flight work, or recent "
        "failures across any persona."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "persona": {
                "type": "string",
                "description": "Filter to one persona name. Omit to see all.",
            },
            "status": {
                "type": "string",
                "enum": sorted(_VALID_STATUSES),
                "description": "Filter to one task status.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "default": 20,
            },
        },
    },
    handler=_list_tasks,
)


# ---------------------------------------------------------------------------
# Agent CRUD + list_models
# ---------------------------------------------------------------------------


def _validate_agent_id(agent_id: str) -> None:
    if not isinstance(agent_id, str) or not _AGENT_ID_RE.match(agent_id):
        raise ToolError(
            f"invalid agent id {agent_id!r}: must be 1–64 chars of "
            f"[a-z0-9_-], starting with a letter or digit"
        )


async def _next_auto_name(ctx: ToolContext, persona_name: str) -> str:
    """Return `<persona>-<n>` for the smallest unused n ≥ 1."""
    existing = await ctx.repos.agents.list_by_persona(
        persona_name, include_archived=True
    )
    used = {a.id for a in existing}
    n = 1
    while f"{persona_name}-{n}" in used:
        n += 1
    return f"{persona_name}-{n}"


async def _create_agent(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Register a new agent instance of a given persona.

    Auto-naming: omit `name` to get `<persona>-<n>` (smallest unused n).
    Specify `model` to pin to a single registered model_id; falls back to
    the persona's model_preference if it's unhealthy/unavailable at wakeup
    time.
    """
    persona_name = args.get("persona")
    if not isinstance(persona_name, str) or not persona_name:
        raise ToolError("persona required (string)")
    persona = await ctx.repos.personas.get(persona_name)
    if persona is None or persona.status != "approved":
        raise ToolError(f"persona '{persona_name}' not found or not approved")

    name = args.get("name")
    if name is None:
        agent_id = await _next_auto_name(ctx, persona_name)
    else:
        if not isinstance(name, str):
            raise ToolError("name must be a string if provided")
        _validate_agent_id(name)
        if await ctx.repos.agents.exists(name):
            raise ToolError(f"agent id {name!r} already exists")
        agent_id = name

    metadata: dict[str, Any] = {}
    description = args.get("description")
    if description is not None:
        if not isinstance(description, str):
            raise ToolError("description must be a string")
        metadata["description"] = description
    model_id = args.get("model")
    if model_id is not None:
        if not isinstance(model_id, str):
            raise ToolError("model must be a string (model_id from list_models)")
        registry = ctx.extras.get("model_registry")
        if registry is not None and registry.by_id(model_id) is None:
            raise ToolError(
                f"model_id {model_id!r} not in registry. "
                f"Call list_models() for the valid set."
            )
        metadata["model_id"] = model_id

    await ctx.repos.agents.create(
        agent_id=agent_id,
        persona_name=persona_name,
        created_by=ctx.persona_name,
        metadata=metadata or None,
    )

    # Pre-create the agent's private notes file so future wakeups of this
    # agent can `read_memory("facts/agent-<id>-notes.md")` without needing
    # to discover the path. Mirrors what seed_default_agents does for
    # owner/leader; covers ad-hoc agents (workers etc.) spawned at runtime.
    notes_path: str | None = None
    root_str = ctx.extras.get("memory_root")
    if root_str:
        from ...personas.seed import ensure_agent_notes_file
        try:
            notes_path = str(ensure_agent_notes_file(Path(root_str), agent_id))
        except OSError:
            notes_path = None  # non-fatal: agent will get the path from prompt

    return {
        "agent_id": agent_id,
        "persona": persona_name,
        "status": "idle",
        "metadata": metadata or {},
        "notes_file": notes_path,
    }


CREATE_AGENT = Tool(
    name="create_agent",
    description=(
        "Create a new agent instance of an existing persona. Agents are the "
        "addressable identity for mailbox + dispatch_task — one persona can "
        "have many agents running in parallel. Pass `name` to choose the "
        "agent id; omit it for auto-naming (<persona>-<n>). Pass `model` to "
        "pin to a specific model_id (use list_models() to discover)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "persona": {
                "type": "string",
                "description": "Persona name from list_personas().",
            },
            "name": {
                "type": "string",
                "description": (
                    "Optional agent id (1–64 chars of [a-z0-9_-]). "
                    "Auto-generated as <persona>-<n> if omitted."
                ),
            },
            "model": {
                "type": "string",
                "description": (
                    "Optional model_id override. Must exist in the model "
                    "registry. Falls back to persona's model_preference if "
                    "this model is unhealthy at wakeup time."
                ),
            },
            "description": {
                "type": "string",
                "description": "Optional free-form note about this agent's purpose.",
            },
        },
        "required": ["persona"],
    },
    handler=_create_agent,
)


async def _archive_agent(
    ctx: ToolContext, args: dict[str, Any]
) -> dict[str, Any]:
    agent_id = args.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id:
        raise ToolError("agent_id required (string)")
    if agent_id in ("owner", "leader"):
        raise ToolError(
            f"refusing to archive well-known agent {agent_id!r}; "
            f"this would break system bootstrap"
        )
    if not await ctx.repos.agents.exists(agent_id):
        raise ToolError(f"agent {agent_id!r} not found")
    changed = await ctx.repos.agents.archive(agent_id)
    return {
        "agent_id": agent_id,
        "archived": bool(changed),
        "note": (
            "Soft delete: mailbox and history preserved. New mail / dispatch "
            "to this agent will be rejected. In-flight tasks finish normally."
        ),
    }


ARCHIVE_AGENT = Tool(
    name="archive_agent",
    description=(
        "Soft-archive an agent. New mail/dispatch is blocked but mailbox and "
        "history stay. In-flight tasks finish. Cannot archive 'owner' or "
        "'leader' — they are bootstrap-pinned."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "agent_id": {"type": "string"},
        },
        "required": ["agent_id"],
    },
    handler=_archive_agent,
)


async def _list_agents(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Live agent instances (not persona templates — those are list_personas)."""
    include_archived = bool(args.get("include_archived", False))
    agents = await ctx.repos.agents.list_all(include_archived=include_archived)
    return {
        "agents": [
            {
                "id": a.id,
                "persona": a.persona_name,
                "status": a.status,
                "created_by": a.created_by,
                "created_at": a.created_at.isoformat() if a.created_at else None,
                "model_id": a.model_id,
                "description": a.description,
            }
            for a in agents
        ],
        "count": len(agents),
        "note": (
            "These are running instances. mailbox_send / dispatch_task / "
            "mailbox_read target an `id` from this list. Use list_personas() "
            "to see role definitions you can spawn new agents of."
        ),
    }


LIST_AGENTS = Tool(
    name="list_agents",
    description=(
        "List currently-active agent instances (id, persona, status, model). "
        "Pass include_archived=true to also see soft-deleted ones. "
        "For role definitions (templates) use list_personas instead."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "include_archived": {"type": "boolean", "default": False},
        },
    },
    handler=_list_agents,
)


async def _list_models(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Describe every configured LLM, plus its auth/health state."""
    registry = ctx.extras.get("model_registry")
    if registry is None:
        raise ToolError(
            "model_registry not available in this context (CLI test mode?)"
        )
    health = ctx.extras.get("health_tracker")
    out = []
    for e in registry.entries:
        auth_ok = bool(os.environ.get(e.endpoint.auth_env))
        healthy = (
            None if health is None else health.is_available(e.id)
        )
        out.append(
            {
                "id": e.id,
                "provider": e.provider,
                "tier": e.tier,
                "capabilities": list(e.capabilities),
                "status": e.status,
                "context_window": e.context_window,
                "auth_env": e.endpoint.auth_env,
                "auth_ok": auth_ok,
                "healthy": healthy,
            }
        )
    return {"models": out, "count": len(out)}


LIST_MODELS = Tool(
    name="list_models",
    description=(
        "List every model in Lyre's model registry, including provider, "
        "tier (flagship/workhorse/cheap), capabilities, whether the auth "
        "env var is set, and current HealthTracker status. Use the `id` "
        "field as the value for create_agent's `model` arg."
    ),
    input_schema={"type": "object", "properties": {}},
    handler=_list_models,
)
