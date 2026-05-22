"""Mail tab — merged inbox + feed view with urgency-band filter chips.

Replaces the old `/inbox` (high-urgency) and `/feed` (all urgencies)
split. They were semantically the same thing (mail to owner, time
desc) just with different filters; the chip set lets the owner shift
band without losing context.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from starlette.templating import _TemplateResponse

from ...persistence.models import MailboxMessage
from ..view_helpers import rel_time
from . import repos_from, templates_from

router = APIRouter()


# Map filter key → set of urgencies it covers.
_FILTER_BANDS: dict[str, set[str]] = {
    "all":     {"blocker", "high", "normal", "low"},
    "blocker": {"blocker"},
    "high":    {"blocker", "high"},
    "normal":  {"blocker", "high", "normal"},
    "feed":    {"normal", "low"},
}


def _filter_msgs(
    msgs: list[MailboxMessage], key: str,
) -> list[MailboxMessage]:
    band = _FILTER_BANDS.get(key, _FILTER_BANDS["all"])
    return [m for m in msgs if m.urgency in band]


@router.get("/mail", response_class=HTMLResponse)
async def mail_view(
    request: Request, u: str = "all", before_id: int | None = None,
) -> _TemplateResponse:
    repos = repos_from(request)
    # 50-row default — each row markdown-renders its body, and 200 of
    # those would lock the event loop for a noticeable beat on a busy
    # mailbox. Mail's primary use is scanning recent items + copying
    # text out; the filter chips narrow within the page, and
    # ?before_id= paginates for archive browsing.
    msgs = await repos.mailbox.read_messages_paged(
        "owner", before_id=before_id, limit=50,
    )
    counts = {k: len([m for m in msgs if m.urgency in band])
              for k, band in _FILTER_BANDS.items()}
    counts["all"] = len(msgs)

    filtered = _filter_msgs(msgs, u if u in _FILTER_BANDS else "all")

    unread_total = sum(1 for m in msgs if not m.read_at)
    last_delivery_rel = rel_time(msgs[0].delivered_at) if msgs else "—"

    return templates_from(request).TemplateResponse(
        request, "mail.html",
        {
            "tab": "mail",
            "messages": filtered,
            "filter": u if u in _FILTER_BANDS else "all",
            "counts": counts,
            "unread_total": unread_total,
            "last_delivery_rel": last_delivery_rel,
            # base.html header pill
            "unread_count": unread_total,
        },
    )


@router.get("/mail/{msg_id}", response_class=HTMLResponse)
async def mail_detail(
    request: Request, msg_id: int,
) -> _TemplateResponse:
    """Single-message view with rendered markdown.

    The list view (`/mail`) keeps bodies as fast plain text in a <pre>
    so tab-switching stays snappy — markdown rendering for every row
    is synchronous CPU work that adds up. This detail view is where
    full markdown rendering lives: click a row's title to land here.

    Page exposes the body twice — once rendered (for reading), once
    raw (for copying via the one-click Copy button) — so the user
    never has to choose between "looks nice" and "copies clean".
    """
    repos = repos_from(request)
    msg = await repos.mailbox.get_message(msg_id)
    if msg is None:
        raise HTTPException(
            status_code=404, detail=f"mail #{msg_id} not found"
        )
    # Resolve attachment blob metadata for the inline preview. Bulk
    # call to avoid N+1; preserves the order in msg.attachments.
    attachments = (
        await repos.blobs.list_ids(msg.attachments)
        if msg.attachments else []
    )
    return templates_from(request).TemplateResponse(
        request, "mail_detail.html",
        {
            "tab": "mail",
            "msg": msg,
            "attachments": attachments,
        },
    )
