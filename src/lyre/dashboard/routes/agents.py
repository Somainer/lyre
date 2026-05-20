"""Agents tab — list every live agent + per-agent activity drill-down.

Pairs with /activity (which is now intentionally high-level): clicking
an agent here gives the noisy per-agent timeline (tool_use, assistant
text, turn_end, notes — every transcript-derived event).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from ..activity import build_activity, list_active_wakeups

router = APIRouter()


@router.get("/agents", response_class=HTMLResponse)
async def agents_list(request: Request) -> HTMLResponse:
    """Listing of all agents (active + archived toggle)."""
    repos = request.app.state.repos
    agents = await repos.agents.list_all(include_archived=False)
    archived = await repos.agents.list_all(include_archived=True)
    archived = [a for a in archived if a.status == "archived"]
    active_wakeups = await list_active_wakeups(repos)
    busy_agents = {w.agent_id or w.persona_name for w in active_wakeups}

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "agents.html",
        {
            "tab": "agents",
            "agents": agents,
            "archived_agents": archived,
            "busy_agents": busy_agents,
            "active_wakeups": active_wakeups,
        },
    )


@router.get("/agents/{agent_id}", response_class=HTMLResponse)
async def agent_detail(
    request: Request, agent_id: str, minutes: int = 60
) -> HTMLResponse:
    """Drill-down: agent's full timeline (incl. transcript events)."""
    repos = request.app.state.repos
    agent = await repos.agents.get(agent_id)
    if agent is None:
        raise HTTPException(
            status_code=404, detail=f"agent {agent_id!r} not found"
        )

    events = await build_activity(
        repos,
        minutes_back=minutes,
        agent_id=agent_id,
        include_transcript=True,
        model_context_windows=getattr(
            request.app.state, "model_context_windows", None
        ),
    )
    # Active wakeups specifically belonging to this agent — for the "is
    # it running now" indicator at the top.
    active = [
        w for w in await list_active_wakeups(repos)
        if (w.agent_id == agent_id)
        or (w.agent_id is None and w.persona_name == agent_id)
    ]

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "agent_detail.html",
        {
            "tab": "agents",
            "agent": agent,
            "events": events,
            "active_wakeups": active,
            "window_minutes": minutes,
        },
    )


@router.get(
    "/partials/agents/{agent_id}/timeline", response_class=HTMLResponse
)
async def agent_timeline_partial(
    request: Request, agent_id: str, minutes: int = 60
) -> HTMLResponse:
    """For htmx auto-refresh on the agent detail page."""
    repos = request.app.state.repos
    if not await repos.agents.exists(agent_id):
        raise HTTPException(
            status_code=404, detail=f"agent {agent_id!r} not found"
        )
    events = await build_activity(
        repos,
        minutes_back=minutes,
        agent_id=agent_id,
        include_transcript=True,
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
