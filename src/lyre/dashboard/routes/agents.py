"""Agents tab — list every live agent (table or lineage mode) + per-agent drill-down."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from ..activity import build_activity, list_active_wakeups

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

    @classmethod
    def of(cls, agent, derived_status: str) -> _AgentView:
        return cls(
            id=agent.id,
            persona_name=agent.persona_name,
            status=derived_status,
            model_id=agent.model_id,
            description=agent.description,
            created_at=agent.created_at,
            parent_agent_id=agent.parent_agent_id,
            archived_at=agent.archived_at,
        )


_IN_FLIGHT = {"pending", "in_progress", "needs_input"}


async def _in_flight_by_agent(repos) -> dict[str, int]:
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


def _derive_occupancy_status(agent, busy_ids: set[str]) -> str:
    """Reflect "running a wakeup right now" back into the Agent record.
    Without this, agent.status (which only flips on idle/busy/archived
    transitions written by the runtime) lags the reality the dashboard
    sees via list_active_wakeups. occupancy_pill() reads .status."""
    if agent.status == "archived":
        return "archived"
    if agent.id in busy_ids:
        return "busy"
    return "idle"


@router.get("/agents", response_class=HTMLResponse)
async def agents_list(
    request: Request, mode: str = "table",
) -> HTMLResponse:
    repos = request.app.state.repos
    agents_all = await repos.agents.list_all(include_archived=True)
    live = [a for a in agents_all if a.status != "archived"]
    archived = [a for a in agents_all if a.status == "archived"]

    active_wakeups = await list_active_wakeups(repos)
    busy_ids = {w.agent_id or w.persona_name for w in active_wakeups}
    in_flight = await _in_flight_by_agent(repos)

    live_views = [
        _AgentView.of(a, _derive_occupancy_status(a, busy_ids)) for a in live
    ]
    archived_views = [
        _AgentView.of(a, _derive_occupancy_status(a, busy_ids))
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

    templates = request.app.state.templates
    return templates.TemplateResponse(
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


@router.get("/agents/{agent_id}", response_class=HTMLResponse)
async def agent_detail(
    request: Request, agent_id: str, minutes: int = 60,
) -> HTMLResponse:
    repos = request.app.state.repos
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
    busy_ids = {w.agent_id or w.persona_name for w in active}
    in_flight = await _in_flight_by_agent(repos)
    agent_view = _AgentView.of(agent, _derive_occupancy_status(agent, busy_ids))

    children_raw = [
        a for a in await repos.agents.list_all(include_archived=False)
        if a.parent_agent_id == agent_id
    ]
    children_views = [
        _AgentView.of(c, _derive_occupancy_status(c, busy_ids))
        for c in children_raw
    ]

    templates = request.app.state.templates
    return templates.TemplateResponse(
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
        },
    )


@router.get(
    "/partials/agents/{agent_id}/timeline", response_class=HTMLResponse
)
async def agent_timeline_partial(
    request: Request, agent_id: str, minutes: int = 60,
) -> HTMLResponse:
    repos = request.app.state.repos
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
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "partials/activity_body.html",
        {
            "events": events,
            "active_wakeups": active,
            "window_minutes": minutes,
        },
    )
