"""Home tab — at-a-glance stat tiles + "needs your attention" + live feed."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..view_helpers import (
    bucket_into,
    fmt_ms,
    fmt_tokens,
    greeting_for,
    rel_time,
    utc_iso_hours_ago,
)

router = APIRouter()


async def _home_card_context(repos, model_context_windows) -> dict:
    """Compute the data block shared by the full page + the HTMX partial."""
    since_24h = utc_iso_hours_ago(24)
    since_1h = utc_iso_hours_ago(1)

    # All four `limit=` values were 200-500 before. The values are
    # only used for chip COUNTS and 12-bucket sparklines — neither
    # needs more than ~100 rows of resolution. Iterating 500
    # MailboxMessage Pydantic models just to count by urgency was a
    # noticeable hot spot during tab-switching (the home stats card
    # re-renders on every change event). Cutting these in half halves
    # the Python iteration cost AND the rows over the wire.
    all_tasks = await repos.tasks.find_recent(limit=100)
    in_progress = sum(1 for t in all_tasks if t.status == "in_progress")
    pending = sum(1 for t in all_tasks if t.status == "pending")
    needs_input = sum(1 for t in all_tasks if t.status == "needs_input")
    completed_24h = await repos.tasks.count_completed_since(since_24h)

    recent_tasks = await repos.tasks.find_recently_changed(
        utc_iso_hours_ago(12), limit=100
    )
    tasks_spark = bucket_into(recent_tasks, buckets=12)
    tasks_in_flight = in_progress + pending + needs_input

    unread_msgs = await repos.mailbox.read_unread("owner", limit=200)
    blockers_unread = sum(1 for m in unread_msgs if m.urgency == "blocker")
    high_unread = sum(1 for m in unread_msgs if m.urgency == "high")
    normal_unread = sum(1 for m in unread_msgs if m.urgency == "normal")
    unread_total = len(unread_msgs)
    recent_mail = await repos.mailbox.read_recent_for_audit(
        utc_iso_hours_ago(12), limit=100
    )
    mail_spark = bucket_into(
        [m for m in recent_mail if m.recipient == "owner"], buckets=12
    )

    active_wakeups = await repos.wakeups.list_active()
    active_preview = [
        {
            "persona_name": w.persona_name,
            "tok_in_fmt": fmt_tokens(w.token_input or 0),
        }
        for w in active_wakeups[:3]
    ]
    tok_rate = 0.0
    for w in active_wakeups:
        seconds = max(1, (w.wall_clock_ms or 0) / 1000)
        tok_rate += (w.token_input or 0) / seconds
    tok_rate_label = f"{int(tok_rate):,} tok/s in" if tok_rate else "0 tok/s"

    wakeup_history = await repos.wakeups.list_since(since_24h, limit=100)
    wakeup_spark = bucket_into(wakeup_history, buckets=12)

    last_wakeup = wakeup_history[0] if wakeup_history else None
    last_w_ctx_pct = None
    if last_wakeup and last_wakeup.context_peak_tokens and last_wakeup.model:
        window = (model_context_windows or {}).get(last_wakeup.model)
        if window:
            last_w_ctx_pct = round(last_wakeup.context_peak_tokens / window * 100)

    recent_unread_in_hour = sum(
        1 for m in unread_msgs
        if m.delivered_at
        and (datetime.now(UTC) - (
            m.delivered_at if m.delivered_at.tzinfo else m.delivered_at.replace(tzinfo=UTC)
        )).total_seconds() < 3600
    )

    completed_recent = (
        await repos.tasks.count_completed_since(since_1h)
    )

    # Single tuple unpack — the previous version awaited
    # sum_tokens_since twice (once per channel), doubling DB pressure
    # for no payoff.
    tokens_in_24h, tokens_out_24h = await repos.wakeups.sum_tokens_since(
        since_24h
    )

    return {
        # tile 1
        "tasks_in_flight": tasks_in_flight,
        "tasks_delta_label": (
            f"+{completed_recent} completed in last 1h"
            if completed_recent else "no recent change"
        ),
        "tasks_spark": tasks_spark or [0] * 12,
        "in_progress": in_progress,
        "pending": pending,
        "needs_input": needs_input,
        # tile 2
        "unread_total": unread_total,
        "mail_delta_label": (
            f"{recent_unread_in_hour} in last 1h"
            if recent_unread_in_hour else "no new mail in 1h"
        ),
        "mail_spark": mail_spark or [0] * 12,
        "blockers_unread": blockers_unread,
        "high_unread": high_unread,
        "normal_unread": normal_unread,
        # tile 3
        "active_wakeups_count": len(active_wakeups),
        "active_wakeups_preview": active_preview,
        "tok_rate_label": tok_rate_label,
        "wakeup_spark": wakeup_spark or [0] * 12,
        # tile 4
        "last_wakeup": last_wakeup,
        "last_wakeup_tok_in": fmt_tokens(
            last_wakeup.token_input if last_wakeup else None
        ),
        "last_wakeup_tok_out": fmt_tokens(
            last_wakeup.token_output if last_wakeup else None
        ),
        "last_wakeup_wall": fmt_ms(
            last_wakeup.wall_clock_ms if last_wakeup else None
        ),
        "last_wakeup_ctx_pct": last_w_ctx_pct,
        # legacy keys for tests that grep for "in-progress" / "blockers unread"
        "completed_24h": completed_24h,
        "tokens_in_24h": tokens_in_24h,
        "tokens_out_24h": tokens_out_24h,
    }


@router.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    repos = request.app.state.repos
    mcw = getattr(request.app.state, "model_context_windows", None)
    ctx = await _home_card_context(repos, mcw)

    agents = await repos.agents.list_all(include_archived=False)
    recent_blockers = await repos.mailbox.read_messages_paged(
        "owner", before_id=None, limit=5, min_urgency="blocker"
    )
    recent_feed_msgs = await repos.mailbox.read_messages_paged(
        "owner", before_id=None, limit=8
    )

    last_w = ctx["last_wakeup"]
    last_wakeup_rel = (
        rel_time(last_w.started_at) if last_w else "—"
    )

    # Header counters used by base.html
    unread_count = ctx["unread_total"]
    needs_input_count = ctx["needs_input"]

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "tab": "home",
            "owner_name": (
                getattr(request.app.state, "owner_name", None) or "owner"
            ),
            "greeting": greeting_for(),
            "agents_online": len(agents),
            "last_wakeup_rel": last_wakeup_rel,
            "recent_blockers": recent_blockers,
            "recent_feed": recent_feed_msgs,
            "unread_count": unread_count,
            "needs_input_count": needs_input_count,
            # Home renders the stats grid + the recent-blockers card.
            # It needs `stats` push (chip counts move) plus the
            # always-on topnav widgets.
            "sse_events": "stats,agent-status,health",
            **ctx,
        },
    )


@router.get("/partials/home/cards", response_class=HTMLResponse)
async def home_cards_partial(request: Request) -> HTMLResponse:
    repos = request.app.state.repos
    mcw = getattr(request.app.state, "model_context_windows", None)
    ctx = await _home_card_context(repos, mcw)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "partials/home_cards.html", ctx
    )


@router.get("/partials/health", response_class=HTMLResponse)
async def health_partial(request: Request) -> HTMLResponse:
    """Topbar health pill: animated dot + 'N live · state.db ok'."""
    repos = request.app.state.repos
    active = await repos.wakeups.list_active()
    from ..view_helpers import fmt_tokens  # local: keep route lean
    _ = fmt_tokens  # silence linter if unused
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "partials/health.html",
        {"active_count": len(active)},
    )
