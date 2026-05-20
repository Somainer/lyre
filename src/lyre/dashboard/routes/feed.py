"""Feed tab — owner mailbox, all urgencies, time desc."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/feed", response_class=HTMLResponse)
async def feed(request: Request, before_id: int | None = None) -> HTMLResponse:
    repos = request.app.state.repos
    msgs = await repos.mailbox.read_messages_paged(
        "owner", before_id=before_id, limit=100,
    )
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "feed.html",
        {"tab": "feed", "messages": msgs},
    )
