"""Inbox tab — owner mailbox, optional urgency filter via ?urgency=high.

By default shows ALL mail to owner so that replies to owner's mail
(typically normal urgency) are visible. The earlier behavior hard-coded
min_urgency='high' which silently hid normal replies — user reported
"reply 到我的邮件的邮件无法看到".
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/inbox", response_class=HTMLResponse)
async def inbox(
    request: Request, urgency: str | None = None
) -> HTMLResponse:
    repos = request.app.state.repos
    if urgency is not None and urgency not in (
        "low", "normal", "high", "blocker"
    ):
        urgency = None  # ignore garbage rather than 500
    msgs = await repos.mailbox.read_messages_paged(
        "owner", before_id=None, limit=100, min_urgency=urgency,
    )
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "inbox.html",
        {"tab": "inbox", "messages": msgs, "urgency_filter": urgency},
    )
