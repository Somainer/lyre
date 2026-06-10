"""Activity tab — chat-bubble timeline with sticky compose dock."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from starlette.templating import _TemplateResponse

from ..activity import build_activity_context, list_active_wakeups
from . import (
    live_folders_from,
    object_store_root_from,
    repos_from,
    templates_from,
)

router = APIRouter()


async def _activity_ctx(
    request: Request, minutes: int, agent_id: str | None = None,
) -> dict[str, Any]:
    """Timeline + active strip + live cards, shared by the page route,
    the legacy partial, and (via the same helper) the SSE renderer."""
    return await build_activity_context(
        repos_from(request),
        minutes_back=minutes,
        agent_id=agent_id,
        include_transcript=agent_id is not None,
        model_context_windows=getattr(
            request.app.state, "model_context_windows", None
        ),
        object_store_root=object_store_root_from(request),
        live_folders=live_folders_from(request),
    )


@router.get("/activity", response_class=HTMLResponse)
async def activity_page(
    request: Request, minutes: int = 30,
) -> _TemplateResponse:
    repos = repos_from(request)
    ctx = await _activity_ctx(request, minutes)
    # Compose dock dropdown: every non-archived agent is a candidate
    # recipient. Owner is the sender; we exclude owner from the list.
    agents = await repos.agents.list_all(include_archived=False)
    recipients = [a for a in agents if a.id != "owner"]

    # Compose dock default — dispatcher persona's seeded agent id from
    # identity.md (display_name fallback to name). Falls back to owner
    # if no dispatcher persona is registered.
    dispatcher = await repos.personas.get("dispatcher")
    if dispatcher is not None:
        default_recipient = dispatcher.display_name or dispatcher.name
    else:
        default_recipient = "owner"
    return templates_from(request).TemplateResponse(
        request, "activity.html",
        {
            "tab": "activity",
            **ctx,
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
) -> _TemplateResponse:
    ctx = await _activity_ctx(request, minutes)
    return templates_from(request).TemplateResponse(
        request, "partials/activity_body.html", ctx,
    )


@router.get("/partials/agent-status", response_class=HTMLResponse)
async def agent_status_partial(request: Request) -> _TemplateResponse:
    """Topnav indicator badge for the Activity tab — shows running count."""
    repos = repos_from(request)
    active = await list_active_wakeups(repos)
    return templates_from(request).TemplateResponse(
        request, "partials/agent_status.html",
        {"active_wakeups": active},
    )
