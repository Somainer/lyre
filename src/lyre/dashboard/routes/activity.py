"""Activity tab — chat-bubble timeline with sticky compose dock."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..activity import build_activity, list_active_wakeups

router = APIRouter()


@router.get("/activity", response_class=HTMLResponse)
async def activity_page(
    request: Request, minutes: int = 30,
) -> HTMLResponse:
    repos = request.app.state.repos
    events = await build_activity(
        repos, minutes_back=minutes, include_transcript=False,
        model_context_windows=getattr(
            request.app.state, "model_context_windows", None
        ),
    )
    active = await list_active_wakeups(repos)
    # Compose dock dropdown: every non-archived agent is a candidate
    # recipient. Owner is the sender; we exclude owner from the list.
    agents = await repos.agents.list_all(include_archived=False)
    recipients = [a for a in agents if a.id != "owner"]

    templates = request.app.state.templates
    # Compose dock default — dispatcher persona's seeded agent id from
    # identity.md (display_name fallback to name). Falls back to owner
    # if no dispatcher persona is registered.
    dispatcher = await repos.personas.get("dispatcher")
    if dispatcher is not None:
        default_recipient = dispatcher.display_name or dispatcher.name
    else:
        default_recipient = "owner"
    return templates.TemplateResponse(
        request, "activity.html",
        {
            "tab": "activity",
            "events": events,
            "active_wakeups": active,
            "window_minutes": minutes,
            "compose_recipients": recipients,
            "default_recipient": default_recipient,
            # SSE stream URL params — global view, but honor the picked
            # time window so the broadcaster renders the same fragment
            # the page initially showed.
            "sse_minutes": minutes,
            # Activity page only needs the activity timeline + topnav.
            # No stats grid on this page.
            "sse_events": "activity,agent-status,health",
        },
    )


@router.get("/partials/activity", response_class=HTMLResponse)
async def activity_partial(
    request: Request, minutes: int = 30,
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
    """Topnav indicator badge for the Activity tab — shows running count."""
    repos = request.app.state.repos
    active = await list_active_wakeups(repos)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "partials/agent_status.html",
        {"active_wakeups": active},
    )
