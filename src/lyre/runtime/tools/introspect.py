"""Introspection tools — let personas (especially dispatcher) see system state.

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
from pathlib import Path
from typing import Any

from ...persistence.models import Agent, Task
from ..future_mail import now_utc
from ..identity import compose_id, is_valid_agent_id
from . import Tool, ToolContext, ToolError

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


# Hard cap on the scratchpad size we'll accept on append/overwrite.
# Larger than this and the model is hoarding instead of curating —
# scratchpad is working memory, not lifetime archive. Long-term content
# belongs in ``facts/agent-<id>-notes.md``.
_SCRATCHPAD_MAX_BYTES = 32 * 1024


async def _update_scratchpad(
    ctx: ToolContext, args: dict[str, Any],
) -> dict[str, Any]:
    """Write to the calling agent's own scratchpad. Sandboxed to one
    file: ``memory/scratchpad/<flat-self-id>.md``. Read via
    ``read_memory(<that-path>)``.

    Modes:
      - ``append`` (default) — add content to the end. A trailing
        newline is inserted between existing content and the new chunk
        so concatenated calls don't smash together.
      - ``overwrite`` — replace the entire file. The curation path:
        you read, decide what stays, write back the pruned version.
        Done items must be removed this way — otherwise they keep
        coming back into context every wakeup.
    """
    content = args.get("content")
    if not isinstance(content, str):
        raise ToolError("content required (string)")
    mode = args.get("mode", "append")
    if mode not in ("append", "overwrite"):
        raise ToolError("mode must be 'append' or 'overwrite'")

    root_str = ctx.extras.get("memory_root")
    if not root_str:
        raise ToolError("memory_root not configured for this wakeup")
    if not ctx.agent_id:
        # ToolContext.agent_id is structurally optional but in any
        # real wakeup the scheduler populates it. A None here implies
        # an unwired test harness, not a runtime case.
        raise ToolError("agent_id not configured for this wakeup")
    root = Path(root_str)

    from ...personas.seed import (
        ensure_agent_scratchpad_file,
        scratchpad_rel_path,
    )

    # Sandboxes by construction: path comes from helpers, never from
    # caller. The scratchpad is per-agent — agents can't read or write
    # each other's working memory.
    path = ensure_agent_scratchpad_file(root, ctx.agent_id)
    rel = scratchpad_rel_path(ctx.agent_id)

    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if mode == "append":
        sep = "" if (not existing or existing.endswith("\n")) else "\n"
        new_body = existing + sep + content
    else:
        new_body = content

    if len(new_body.encode("utf-8")) > _SCRATCHPAD_MAX_BYTES:
        raise ToolError(
            f"scratchpad would exceed {_SCRATCHPAD_MAX_BYTES // 1024} "
            f"KiB after this write. Scratchpad is working memory, not "
            f"archive — call again with mode='overwrite' and a pruned "
            f"version, or move long-term content to "
            f"facts/agent-<id>-notes.md."
        )

    path.write_text(new_body, encoding="utf-8")
    return {
        "rel_path": rel,
        "mode": mode,
        "bytes": len(new_body.encode("utf-8")),
    }


UPDATE_SCRATCHPAD = Tool(
    name="update_scratchpad",
    description=(
        "Write to your scratchpad — a private markdown file at "
        "`memory/scratchpad/<your-flat-id>.md` that persists across "
        "wakeups. This is your short-term / working memory: what "
        "you're tracking right now, commitments you've made, the next "
        "step you planned. Read it with `read_memory(<that-path>)` at "
        "the start of every wakeup. "
        "\n\n"
        "mode='append' adds to the bottom (default — for new items). "
        "mode='overwrite' replaces the whole file (for curation — when "
        "done items pile up, read first, then write back only what's "
        "still active). Done items MUST be removed via overwrite — "
        "otherwise they pollute context every wakeup. "
        "\n\n"
        "Sandbox: each agent can only write its own scratchpad. The "
        "rel_path is computed from your agent_id, not from arguments."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "Markdown content to write or append.",
            },
            "mode": {
                "type": "string",
                "enum": ["append", "overwrite"],
                "description": (
                    "'append' (default) adds to file end. "
                    "'overwrite' replaces file body — use this when "
                    "curating / removing completed items."
                ),
            },
        },
        "required": ["content"],
    },
    handler=_update_scratchpad,
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


def _validate_short_name(name: str) -> None:
    """Validate just the right-hand side of `persona/name`.

    The caller passes a bare token (e.g. `refactor-auth`); we compose
    `<persona>/<name>` ourselves. So this validates the name-segment
    grammar only, not the full id.
    """
    if not isinstance(name, str) or not name:
        raise ToolError("name must be a non-empty string")
    # Probe with a throwaway persona so the full-id check exercises the
    # name-segment rules.
    if not is_valid_agent_id(f"p/{name}"):
        raise ToolError(
            f"invalid agent name {name!r}: must start with a letter or "
            f"digit and contain only lowercase letters, digits, and hyphens"
        )


async def _next_auto_name(ctx: ToolContext, persona_name: str) -> str:
    """Return `<persona>/<n>` for the smallest unused n ≥ 1.

    Used when the model doesn't supply `name`. The numeric suffix is
    intentionally bland — a meaningful name is the model's job. See
    dispatcher.md's "派活前先盘点" section for the reuse-vs-spawn discipline.
    """
    existing = await ctx.repos.agents.list_by_persona(
        persona_name, include_archived=True
    )
    used = {a.id for a in existing}
    n = 1
    while compose_id(persona_name, str(n)) in used:
        n += 1
    return compose_id(persona_name, str(n))


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
    if persona.kind == "singleton":
        raise ToolError(
            f"persona {persona_name!r} is a singleton role — only one "
            f"agent of this persona ever exists (the bootstrap-seeded "
            f"one). Pick a different persona to spawn."
        )

    name = args.get("name")
    if name is None:
        agent_id = await _next_auto_name(ctx, persona_name)
    else:
        _validate_short_name(name)
        agent_id = compose_id(persona_name, name)
        if not is_valid_agent_id(agent_id):
            raise ToolError(
                f"composed agent id {agent_id!r} is invalid; the persona "
                f"name probably contains an unsupported character"
            )
        if await ctx.repos.agents.exists(agent_id):
            raise ToolError(f"agent id {agent_id!r} already exists")

    metadata: dict[str, Any] = {}
    description = args.get("description")
    if description is not None:
        if not isinstance(description, str):
            raise ToolError("description must be a string")
        metadata["description"] = description
    model_id = args.get("model")
    # Treat empty-string and None equivalently: both mean "use this
    # persona's model_preference". Models occasionally emit
    # ``model=""`` as a way of saying "no override" — without this
    # coercion the empty string slipped past the None-check, then
    # failed registry lookup, surfacing as a confusing
    # ``model_id '' not in registry`` error.
    if isinstance(model_id, str) and not model_id.strip():
        model_id = None
    if model_id is not None:
        if not isinstance(model_id, str):
            raise ToolError("model must be a string (model_id from list_models)")
        registry = ctx.extras.get("model_registry")
        if registry is not None and registry.by_id(model_id) is None:
            raise ToolError(
                f"model_id {model_id!r} not in registry. "
                f"Call list_models() for the valid set, or omit "
                f"`model` entirely to let the new agent use its "
                f"persona's model_preference (the common case)."
            )
        metadata["model_id"] = model_id

    # Supervision / lifecycle spec (OTP child_spec analogue). Today only
    # `ephemeral` is acted on: an ephemeral agent is automatically reclaimed
    # (archived) by the scheduler's reaper once it has run at least one task
    # and has no in-flight task. The restart-policy fields are accepted and
    # stored for the forthcoming supervisor PR.
    supervision = args.get("supervision")
    if supervision is not None:
        if not isinstance(supervision, dict):
            raise ToolError("supervision must be an object")
        if "ephemeral" in supervision and not isinstance(
            supervision["ephemeral"], bool
        ):
            raise ToolError("supervision.ephemeral must be a boolean")
        restart = supervision.get("restart")
        if restart is not None and restart not in (
            "temporary", "transient", "permanent"
        ):
            raise ToolError(
                "supervision.restart must be temporary / transient / permanent"
            )
        for k in ("max_restarts", "max_seconds"):
            v = supervision.get(k)
            if v is not None and (not isinstance(v, int) or v < 0):
                raise ToolError(f"supervision.{k} must be a non-negative integer")
        metadata["supervision"] = supervision

    await ctx.repos.agents.create(
        agent_id=agent_id,
        persona_name=persona_name,
        # The agent that called this tool becomes the parent. Used by
        # the dashboard's lineage view, by the identity preamble's
        # "escalate to your parent" hint, and by list_agents output.
        parent_agent_id=ctx.self_mailbox,
        metadata=metadata or None,
    )

    # Pre-create the agent's private notes file so future wakeups of this
    # agent can `read_memory("facts/agent-<id>-notes.md")` without needing
    # to discover the path. Mirrors what seed_default_agents does for
    # bootstrap agents; covers ad-hoc agents (workers etc.) spawned at runtime.
    notes_path: str | None = None
    scratchpad_path: str | None = None
    root_str = ctx.extras.get("memory_root")
    if root_str:
        from ...personas.seed import (
            ensure_agent_notes_file,
            ensure_agent_scratchpad_file,
        )
        root = Path(root_str)
        try:
            notes_path = str(ensure_agent_notes_file(root, agent_id))
        except OSError:
            notes_path = None  # non-fatal: agent will get the path from prompt
        try:
            scratchpad_path = str(ensure_agent_scratchpad_file(root, agent_id))
        except OSError:
            scratchpad_path = None

    return {
        "agent_id": agent_id,
        "persona": persona_name,
        "parent_agent_id": ctx.self_mailbox,
        "status": "idle",
        "metadata": metadata or {},
        "notes_file": notes_path,
        "scratchpad_file": scratchpad_path,
    }


CREATE_AGENT = Tool(
    name="create_agent",
    description=(
        "Create a new agent instance of an existing persona. Agents are the "
        "addressable identity for mailbox + dispatch_task — one persona can "
        "have many agents running in parallel. Agent id is composed as "
        "`<persona>/<name>`; pass `name` to pick the right-hand side, omit "
        "for auto-naming (<persona>/<n>). Before calling this, prefer "
        "`list_agents` and reuse an available agent of the same persona "
        "(see `occupancy` field) — spawning unnecessary agents wastes mailbox "
        "and model budget. Pass `model` to pin to a specific model_id "
        "(use list_models() to discover)."
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
                    "Right-hand side of the agent id, composed as "
                    "`<persona>/<name>` (e.g. `refactor-auth`, `pr-142`). "
                    "Lowercase letters, digits, hyphens; must start with "
                    "letter or digit. Auto-generated as a numeric suffix "
                    "if omitted."
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
            "supervision": {
                "type": "object",
                "description": (
                    "Optional lifecycle spec. Set `{\"ephemeral\": true}` for a "
                    "short-lived worker (e.g. a fan-in panel member): the "
                    "runtime auto-reclaims it once it has finished its task(s) "
                    "and has none in flight, so spawned agents don't accumulate."
                ),
                "properties": {
                    "ephemeral": {
                        "type": "boolean",
                        "description": "Auto-reclaim this agent when its work is done.",
                    },
                    "restart": {
                        "type": "string",
                        "enum": ["temporary", "transient", "permanent"],
                        "description": (
                            "Restart policy on terminal outcome: temporary "
                            "(default, never restart), transient (restart only "
                            "on failure), permanent (restart on any outcome). "
                            "Bounded by max_restarts/max_seconds, then escalated."
                        ),
                    },
                    "max_restarts": {
                        "type": "integer",
                        "description": "Max restarts in the window (default 3).",
                    },
                    "max_seconds": {
                        "type": "integer",
                        "description": "Restart-intensity window in seconds (default 60).",
                    },
                },
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
    # Bootstrap-seeded singletons (parent_agent_id IS NULL) are pinned —
    # the runtime expects owner / dispatcher / analyst-1 / reviewer-1
    # (or their custom-renamed equivalents) to always exist. Archiving
    # one breaks auto-wake-on-mail, Phase 0, etc.
    target = await ctx.repos.agents.get(agent_id)
    if target is None:
        raise ToolError(f"agent {agent_id!r} not found")
    if target.parent_agent_id is None:
        raise ToolError(
            f"refusing to archive bootstrap-seeded agent {agent_id!r}; "
            f"this would break system bootstrap"
        )
    # Reason is recorded for observability (list_agents / dashboard). The
    # Dispatcher passes "idle_reclaimed" when collecting a stale agent; a bare
    # archive defaults to "manual".
    reason = args.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ToolError("reason must be a string if provided")
    changed = await ctx.repos.agents.archive(agent_id, reason=reason or "manual")
    return {
        "agent_id": agent_id,
        "archived": bool(changed),
        "reason": reason or "manual",
        "note": (
            "Soft delete: mailbox and history preserved. New mail / dispatch "
            "to this agent will be rejected. In-flight tasks finish normally."
        ),
    }


ARCHIVE_AGENT = Tool(
    name="archive_agent",
    description=(
        "Soft-archive an agent. New mail/dispatch is blocked but mailbox and "
        "history stay. In-flight tasks finish. Cannot archive bootstrap-pinned "
        "agents (owner, dispatcher, analyst-1, reviewer-1). Pass reason="
        "'idle_reclaimed' when collecting a stale agent (defaults to 'manual') "
        "so the dashboard can tell idle-reclaim from a hand archive."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "agent_id": {"type": "string"},
            "reason": {
                "type": "string",
                "description": (
                    "Why you're archiving (observability label). Use "
                    "'idle_reclaimed' for a stale-agent collection; omit for a "
                    "manual archive."
                ),
            },
        },
        "required": ["agent_id"],
    },
    handler=_archive_agent,
)


_IN_FLIGHT = frozenset({"pending", "in_progress", "needs_input"})


async def _list_agents(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Live agent instances (not persona templates — those are list_personas).

    Each agent is annotated with `occupancy` ∈ {available, queued, busy,
    archived} so leader can make reuse-vs-spawn decisions without a
    separate query:

      - available: idle AND zero in-flight tasks. Free to take new work.
      - queued:    idle AND ≥1 in-flight task waiting. Don't add more.
      - busy:      currently inside a wakeup (an open ``wakeups`` row).
      - archived:  retired; cannot accept new work.

    "busy" is derived from ``wakeups.list_active()``, not from any
    column on the agent record — the runtime doesn't write back a
    ``busy`` status anywhere, so reading agent.status would only ever
    see the stale post-onboard ``idle``. Matching the dashboard's
    ``_derive_occupancy_status`` keeps both surfaces consistent.

    `active_task_id` / `last_active_at` give the leader enough context
    to write a meaningful kick-off mail without round-tripping
    `list_tasks` / `mailbox_read`.

    `idle_seconds` (since the last wakeup) and `stale` support idle-reclaim:
    `stale=true` flags a spawned, NON-ephemeral agent that has been idle past
    `LYRE_IDLE_RECLAIM_AGE` with no in-flight task and no open fan-in leg — a
    HINT that the Dispatcher may `archive_agent` it. It is advisory only (the
    runtime never auto-archives on it) and is always False when the knob is 0
    (the default). Ephemeral agents are reclaimed automatically by the reaper
    and are never flagged here.
    """
    include_archived = bool(args.get("include_archived", False))
    agents = await ctx.repos.agents.list_all(include_archived=include_archived)

    # In-flight task fan-out: pull once, group in Python.
    tasks = await ctx.repos.tasks.find_recent(limit=500)
    in_flight_by_agent: dict[str, list[Task]] = {}
    for t in tasks:
        key = t.agent_id or t.persona_name
        if t.status in _IN_FLIGHT:
            in_flight_by_agent.setdefault(key, []).append(t)

    # Active wakeups: anything still open (ended_at IS NULL) is a
    # genuine running wakeup. Used to mark agents as ``busy`` — the
    # only authoritative signal, since agent.status never flips.
    active_wakeups = await ctx.repos.wakeups.list_active()
    busy_agent_ids = {w.agent_id for w in active_wakeups if w.agent_id}

    # last_active_at: most recent wakeup_started_at per agent.
    recent_wakeups = await ctx.repos.wakeups.list_recent(limit=200)
    last_active: dict[str, str] = {}
    for w in recent_wakeups:
        key = w.agent_id or w.persona_name
        ts = (
            w.started_at.isoformat()
            if hasattr(w.started_at, "isoformat")
            else str(w.started_at)
        )
        # list_recent returns newest-first; keep the first hit per agent.
        last_active.setdefault(key, ts)

    # idle_seconds (since last wakeup) + `stale` reclaim hint, computed in SQL.
    # Threshold 0 (default) → `stale` is always False (feature disabled). This
    # is a PULL signal: the Dispatcher decides whether to archive; nothing here
    # acts on it. Ephemeral agents are handled by the orthogonal reaper, so they
    # are never flagged stale.
    idle_threshold = int(ctx.extras.get("idle_reclaim_age_s", 0) or 0)
    idle = await ctx.repos.agents.idle_report(now_utc(), idle_threshold)

    def _occupancy(agent: Agent) -> str:
        if agent.status == "archived":
            return "archived"
        if agent.id in busy_agent_ids:
            return "busy"
        return "queued" if in_flight_by_agent.get(agent.id) else "available"

    enriched = []
    for a in agents:
        in_flight = in_flight_by_agent.get(a.id, [])
        # idle_report excludes archived agents by design (they need no reclaim
        # hint), so rep is None for them under include_archived=true → report
        # idle_seconds=None / stale=False.
        rep = idle.get(a.id)
        enriched.append({
            "id": a.id,
            "persona": a.persona_name,
            "status": a.status,
            "occupancy": _occupancy(a),
            "parent_agent_id": a.parent_agent_id,
            "in_flight_count": len(in_flight),
            "active_task_id": in_flight[0].id if in_flight else None,
            "last_active_at": last_active.get(a.id),
            "idle_seconds": rep.idle_seconds if rep else None,
            "stale": bool(rep.stale) if rep else False,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            # Why an archived agent was retired (reaped / storm_halted /
            # idle_reclaimed / manual). None while live. Only visible with
            # include_archived=true.
            "archive_reason": a.archive_reason,
            "archived_at": a.archived_at.isoformat() if a.archived_at else None,
            "model_id": a.model_id,
            "description": a.description,
        })
    note = (
        "Reuse-vs-spawn: prefer dispatch_task to an agent with "
        "occupancy='available'. Only call create_agent when no live "
        "agent of the right persona is available AND queued/busy ones "
        "would block this work."
    )
    # `idle_threshold > 0` is redundant with the SQL (stale is always False when
    # disabled) but guards the hint application-side too; `any(stale)` keeps the
    # note out when nothing is reclaimable.
    if idle_threshold > 0 and any(e["stale"] for e in enriched):
        note += (
            " Housekeeping: agents with stale=true have been idle past the "
            "reclaim threshold with no work in flight — archive_agent them to "
            "free the population, UNLESS you expect to reuse one shortly."
        )
    return {
        "agents": enriched,
        "count": len(enriched),
        "note": note,
    }


LIST_AGENTS = Tool(
    name="list_agents",
    description=(
        "List currently-active agent instances (id, persona, status, model, "
        "occupancy, idle_seconds, stale). `stale=true` marks a spawned, "
        "non-ephemeral agent idle past the reclaim threshold with no work in "
        "flight — a hint you may archive_agent it. Pass include_archived=true "
        "to also see soft-deleted ones. For role definitions (templates) use "
        "list_personas instead."
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
        # ``auth_env`` is None for header-only auth (e.g. internal
        # gateways that authenticate via custom headers configured in
        # config.toml, not via an env-var-backed API key). Naive
        # ``os.environ.get(None)`` raises ``TypeError: str expected``
        # — which used to crash list_models() entirely the moment a
        # header-only model entered the registry. Treat header-only
        # as auth_ok=True since startup already validated the headers.
        auth_ok = (
            bool(os.environ.get(e.endpoint.auth_env))
            if e.endpoint.auth_env else True
        )
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
