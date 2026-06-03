"""Agents tab — list every live agent (table or lineage mode) + per-agent drill-down."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from starlette.templating import _TemplateResponse

from ...persistence.models import Agent, Wakeup
from ...persistence.repositories import Repositories
from ..activity import build_activity, list_active_wakeups
from . import repos_from, templates_from

router = APIRouter()


@dataclass
class _AgentView:
    """Sidecar wrapper around Agent so the template can render derived
    state (occupancy status, relative-time created) without mutating the
    Pydantic model."""

    id: str
    persona_name: str
    status: str
    model_id: str | None
    description: str | None
    created_at: Any
    parent_agent_id: str | None
    archived_at: Any = None
    archive_reason: str | None = None

    @classmethod
    def of(cls, agent: Agent, derived_status: str) -> _AgentView:
        return cls(
            id=agent.id,
            persona_name=agent.persona_name,
            status=derived_status,
            model_id=agent.model_id,
            description=agent.description,
            created_at=agent.created_at,
            parent_agent_id=agent.parent_agent_id,
            archived_at=agent.archived_at,
            archive_reason=agent.archive_reason,
        )


_IN_FLIGHT = {"pending", "in_progress", "needs_input"}


async def _in_flight_by_agent(repos: Repositories) -> dict[str, int]:
    """How many in-flight tasks each agent owns. Used to compute the
    "queued" occupancy state (idle agent with work waiting) vs "available"
    (idle agent with nothing queued — actually free to take new work).
    """
    tasks = await repos.tasks.find_recent(limit=500)
    counts: dict[str, int] = {}
    for t in tasks:
        if t.status in _IN_FLIGHT:
            key = t.agent_id or t.persona_name
            counts[key] = counts.get(key, 0) + 1
    return counts


def _derive_occupancy_status(
    agent: Agent,
    busy_agent_ids: set[str],
    busy_legacy_personas: set[str],
) -> str:
    """Reflect "running a wakeup right now" back into the Agent record.
    Without this, agent.status (which is lifecycle-only — idle vs
    archived) lags the reality the dashboard sees via
    list_active_wakeups. occupancy_pill() reads .status.

    Two match passes:
      * ``busy_agent_ids`` — exact `<persona>/<name>` match for wakeups
        whose ``agent_id`` column is set (the post-rework normal case).
      * ``busy_legacy_personas`` — fallback for wakeups with NULL
        ``agent_id`` (rows written before WakeupsRepo.start persisted
        agent_id, or by tests). Matches any agent of that persona by
        ``agent.persona_name``. Overmarks when a persona has multiple
        live instances, but legacy NULL rows imply pre-multi-instance
        usage so the ambiguity is unreachable in practice.
    """
    if agent.status == "archived":
        return "archived"
    if agent.id in busy_agent_ids:
        return "busy"
    if (
        busy_legacy_personas
        and getattr(agent, "persona_name", None) in busy_legacy_personas
    ):
        return "busy"
    return "idle"


def _busy_sets(
    active_wakeups: list[Wakeup],
) -> tuple[set[str], set[str]]:
    """Split active wakeups into the two match sets used by
    ``_derive_occupancy_status``."""
    exact = {w.agent_id for w in active_wakeups if w.agent_id}
    legacy = {
        w.persona_name for w in active_wakeups
        if not w.agent_id and w.persona_name
    }
    return exact, legacy


@router.get("/agents", response_class=HTMLResponse)
async def agents_list(
    request: Request, mode: str = "table",
) -> _TemplateResponse:
    repos = repos_from(request)
    agents_all = await repos.agents.list_all(include_archived=True)
    live = [a for a in agents_all if a.status != "archived"]
    archived = [a for a in agents_all if a.status == "archived"]

    active_wakeups = await list_active_wakeups(repos)
    busy_exact, busy_legacy = _busy_sets(active_wakeups)
    in_flight = await _in_flight_by_agent(repos)

    live_views = [
        _AgentView.of(
            a, _derive_occupancy_status(a, busy_exact, busy_legacy)
        )
        for a in live
    ]
    archived_views = [
        _AgentView.of(
            a, _derive_occupancy_status(a, busy_exact, busy_legacy)
        )
        for a in archived
    ]

    # Lineage view groups agents by parent (parent_agent_id support is
    # pending — for now every agent is a root since there's no parent
    # column). Once agent addressing lands the same template works.
    children_by_parent: dict[str, list[_AgentView]] = {}
    roots: list[_AgentView] = []
    for v in live_views:
        if v.parent_agent_id:
            children_by_parent.setdefault(v.parent_agent_id, []).append(v)
        else:
            roots.append(v)

    persona_count = len({v.persona_name for v in live_views})

    return templates_from(request).TemplateResponse(
        request, "agents.html",
        {
            "tab": "agents",
            "mode": mode if mode in ("table", "lineage") else "table",
            "agents": live_views,
            "archived_agents": archived_views,
            "persona_count": persona_count,
            "in_flight_by_agent": in_flight,
            "lineage_roots": roots,
            "children_by_parent": children_by_parent,
        },
    )


# `:path` converter — agent_ids carry a real `/` (persona/name), so
# the route must accept multi-segment values. Without this, `/agents/
# worker-maintainer/coco-skills-import` becomes a 4-segment URL that
# doesn't match a single-segment template.
@router.get("/agents/{agent_id:path}", response_class=HTMLResponse)
async def agent_detail(
    request: Request, agent_id: str, minutes: int = 60,
) -> _TemplateResponse:
    repos = repos_from(request)
    agent = await repos.agents.get(agent_id)
    if agent is None:
        raise HTTPException(
            status_code=404, detail=f"agent {agent_id!r} not found"
        )

    events = await build_activity(
        repos, minutes_back=minutes, agent_id=agent_id, include_transcript=True,
        model_context_windows=getattr(
            request.app.state, "model_context_windows", None
        ),
    )
    active = await list_active_wakeups(repos)
    busy_exact, busy_legacy = _busy_sets(active)
    in_flight = await _in_flight_by_agent(repos)
    agent_view = _AgentView.of(
        agent, _derive_occupancy_status(agent, busy_exact, busy_legacy)
    )

    children_raw = [
        a for a in await repos.agents.list_all(include_archived=False)
        if a.parent_agent_id == agent_id
    ]
    children_views = [
        _AgentView.of(
            c, _derive_occupancy_status(c, busy_exact, busy_legacy)
        )
        for c in children_raw
    ]

    return templates_from(request).TemplateResponse(
        request, "agent_detail.html",
        {
            "tab": "agent_detail",
            "agent": agent_view,
            "events": events,
            "active_wakeups": [
                w for w in active
                if (w.agent_id == agent_id)
                or (w.agent_id is None and w.persona_name == agent_id)
            ],
            "window_minutes": minutes,
            "in_flight_count": in_flight.get(agent_id, 0),
            "in_flight_by_child": in_flight,
            "children": children_views,
            # Per-agent SSE: scope the activity stream to this agent so
            # the broadcaster pushes only events involving them.
            "sse_agent_id": agent_id,
            "sse_minutes": minutes,
            "sse_events": "activity,agent-status,health",
        },
    )


@router.get(
    "/partials/agents/{agent_id:path}/timeline",
    response_class=HTMLResponse,
)
async def agent_timeline_partial(
    request: Request, agent_id: str, minutes: int = 60,
) -> _TemplateResponse:
    repos = repos_from(request)
    if not await repos.agents.exists(agent_id):
        raise HTTPException(
            status_code=404, detail=f"agent {agent_id!r} not found"
        )
    events = await build_activity(
        repos, minutes_back=minutes, agent_id=agent_id, include_transcript=True,
        model_context_windows=getattr(
            request.app.state, "model_context_windows", None
        ),
    )
    active = [
        w for w in await list_active_wakeups(repos)
        if (w.agent_id == agent_id)
        or (w.agent_id is None and w.persona_name == agent_id)
    ]
    return templates_from(request).TemplateResponse(
        request, "partials/activity_body.html",
        {
            "events": events,
            "active_wakeups": active,
            "window_minutes": minutes,
        },
    )
