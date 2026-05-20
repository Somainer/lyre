"""Wakeups tab — recent wakeups with metering."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/wakeups", response_class=HTMLResponse)
async def wakeups(request: Request) -> HTMLResponse:
    repos = request.app.state.repos
    rows = await repos.wakeups.list_recent(limit=100)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "wakeups.html",
        {"tab": "wakeups", "wakeups": rows},
    )
