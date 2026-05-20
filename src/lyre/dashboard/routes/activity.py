"""Activity tab — audit timeline with auto-refresh."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..activity import build_activity, list_active_wakeups

router = APIRouter()


@router.get("/activity", response_class=HTMLResponse)
async def activity_page(
    request: Request, minutes: int = 30
) -> HTMLResponse:
    repos = request.app.state.repos
    # /activity is the **overview**: high-level events only (tasks /
    # wakeup boundaries / mailbox). Per-agent transcript drill-down
    # lives at /agents/<id> so this page stays scan-able.
    events = await build_activity(
        repos, minutes_back=minutes, include_transcript=False,
        model_context_windows=getattr(
            request.app.state, "model_context_windows", None
        ),
    )
    active = await list_active_wakeups(repos)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "activity.html",
        {
            "tab": "activity",
            "events": events,
            "active_wakeups": active,
            "window_minutes": minutes,
        },
    )


@router.get("/partials/activity", response_class=HTMLResponse)
async def activity_partial(
    request: Request, minutes: int = 30
) -> HTMLResponse:
    repos = request.app.state.repos
    events = await build_activity(
        repos, minutes_back=minutes, include_transcript=False,
        model_context_windows=getattr(
            request.app.state, "model_context_windows", None
        ),
    )
    active = await list_active_wakeups(repos)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "partials/activity_body.html",
        {
            "events": events,
            "active_wakeups": active,
            "window_minutes": minutes,
        },
    )


@router.get("/partials/agent-status", response_class=HTMLResponse)
async def agent_status_partial(request: Request) -> HTMLResponse:
    """Header indicator: list of currently active wakeups (or "idle")."""
    repos = request.app.state.repos
    active = await list_active_wakeups(repos)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "partials/agent_status.html",
        {"active_wakeups": active},
    )
