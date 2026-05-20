"""Home tab — at-a-glance cards + last-5 blockers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()


def _utc_iso_24h_ago() -> str:
    return (datetime.now(UTC) - timedelta(hours=24)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )[:-4] + "Z"


@router.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    repos = request.app.state.repos
    since = _utc_iso_24h_ago()
    in_progress = await repos.tasks.count_in_progress()
    completed_24h = await repos.tasks.count_completed_since(since)
    blockers = await repos.mailbox.count_unread_blockers("owner")
    tok_in, tok_out = await repos.wakeups.sum_tokens_since(since)
    recent_blockers = await repos.mailbox.read_messages_paged(
        "owner", before_id=None, limit=5, min_urgency="blocker"
    )
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "tab": "home",
            "in_progress": in_progress,
            "completed_24h": completed_24h,
            "blockers_unread": blockers,
            "tokens_in_24h": tok_in,
            "tokens_out_24h": tok_out,
            "recent_blockers": recent_blockers,
        },
    )


@router.get("/partials/home/cards", response_class=HTMLResponse)
async def home_cards(request: Request) -> HTMLResponse:
    repos = request.app.state.repos
    since = _utc_iso_24h_ago()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "partials/home_cards.html",
        {
            "in_progress": await repos.tasks.count_in_progress(),
            "completed_24h": await repos.tasks.count_completed_since(since),
            "blockers_unread": await repos.mailbox.count_unread_blockers("owner"),
            "tokens_in_24h": (await repos.wakeups.sum_tokens_since(since))[0],
            "tokens_out_24h": (await repos.wakeups.sum_tokens_since(since))[1],
        },
    )
