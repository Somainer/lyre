"""SSE endpoints.

Two streams are exposed:

  /sse/mailbox     — pushes new owner-mailbox messages as compact JSON.
                     Consumed by the home "Live feed" card's small JS
                     helper, which prepends rows on each event.

  /sse/dashboard   — pushes rendered HTML fragments tagged by event name.
                     Consumed by HTMX's `sse-swap` extension, which
                     replaces the target element's innerHTML on each
                     matching event. Replaces the per-element polling
                     (`hx-trigger="every Ns"`) the dashboard used before.

The /sse/dashboard rendering helpers reuse the same partials the page
routes do — there's no duplicate "send-friendly" template variant.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ..activity import build_activity, list_active_wakeups
from ..dashboard_broadcaster import (
    EVENT_ACTIVITY,
    EVENT_AGENT_STATUS,
    EVENT_HEALTH,
    EVENT_STATS,
)
from ..routes.home import _home_card_context
from . import broadcaster_from, dashboard_broadcaster_from, repos_from, templates_from

router = APIRouter()


# ---------------------------------------------------------------------------
# /sse/mailbox — unchanged from D1, JSON payload for the home Live feed.
# ---------------------------------------------------------------------------


@router.get("/sse/mailbox")
async def sse_mailbox(
    request: Request, recipient: str = "owner",
) -> StreamingResponse:
    broadcaster = broadcaster_from(request)

    async def event_stream() -> AsyncIterator[str]:
        queue = broadcaster.subscribe()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                # Sentinel published by broadcaster.stop() during
                # shutdown — exit the drain loop immediately so uvicorn
                # can finish graceful shutdown without waiting on us.
                if msg is None:
                    break
                if msg.recipient != recipient:
                    continue
                payload = {
                    "id": msg.id,
                    "sender": msg.sender,
                    "recipient": msg.recipient,
                    "urgency": msg.urgency,
                    "title": msg.title,
                    "body": msg.body,
                    "task_id": msg.task_id,
                }
                yield (
                    "event: mailbox\n"
                    f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                )
        finally:
            broadcaster.unsubscribe(queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# /sse/dashboard — HTML fragments, multiplexed by event name.
# ---------------------------------------------------------------------------


def _sse_format(event: str, html: str) -> str:
    """Wrap a multi-line HTML fragment as a single SSE event. HTML lines
    map to `data:` lines per the SSE spec; HTMX's SSE extension reassembles
    them into one swap payload."""
    lines = html.splitlines() or [""]
    data_lines = "\n".join(f"data: {line}" for line in lines)
    return f"event: {event}\n{data_lines}\n\n"


async def _render_stats(request: Request) -> str:
    """Re-render the home stat tiles fragment."""
    repos = repos_from(request)
    mcw = getattr(request.app.state, "model_context_windows", None)
    ctx = await _home_card_context(repos, mcw)
    html = templates_from(request).get_template(
        "partials/home_cards.html"
    ).render(ctx)
    return str(html)


async def _render_activity(
    request: Request, minutes: int, agent_id: str | None
) -> str:
    """Re-render the activity timeline fragment (global or per-agent)."""
    repos = repos_from(request)
    mcw = getattr(request.app.state, "model_context_windows", None)
    events = await build_activity(
        repos,
        minutes_back=minutes,
        agent_id=agent_id,
        include_transcript=agent_id is not None,
        model_context_windows=mcw,
    )
    active = await list_active_wakeups(repos)
    if agent_id is not None:
        active = [
            w for w in active
            if (w.agent_id == agent_id)
            or (w.agent_id is None and w.persona_name == agent_id)
        ]
    html = templates_from(request).get_template(
        "partials/activity_body.html"
    ).render(
        events=events, active_wakeups=active, window_minutes=minutes,
    )
    return str(html)


async def _render_agent_status(request: Request) -> str:
    repos = repos_from(request)
    active = await list_active_wakeups(repos)
    html = templates_from(request).get_template(
        "partials/agent_status.html"
    ).render(active_wakeups=active)
    return str(html)


async def _render_health(request: Request) -> str:
    repos = repos_from(request)
    active = await list_active_wakeups(repos)
    html = templates_from(request).get_template(
        "partials/health.html"
    ).render(active_count=len(active))
    return str(html)


_Renderer = Callable[[Request, str | None, int], Awaitable[str]]

_RENDERERS: dict[str, _Renderer] = {
    EVENT_STATS: lambda req, _aid, _m: _render_stats(req),
    EVENT_ACTIVITY: lambda req, aid, m: _render_activity(req, m, aid),
    EVENT_AGENT_STATUS: lambda req, _aid, _m: _render_agent_status(req),
    EVENT_HEALTH: lambda req, _aid, _m: _render_health(req),
}


_DEFAULT_EVENTS = frozenset(_RENDERERS.keys())


def _parse_events(events_qs: str | None) -> frozenset[str]:
    """Parse `?events=foo,bar` into a set, falling back to all events.

    Pages declare which fragments they actually display via their
    sse_events template var, which flows into the sse-connect URL.
    The Mail / Runs / Agents / Send pages only have the topnav widgets,
    so they declare only `agent-status,health` — saving the heavy
    `stats` (10+ queries) and `activity` (4 queries + 400-row sort)
    renders on every broadcaster tick.
    """
    if not events_qs:
        return _DEFAULT_EVENTS
    return frozenset(
        e.strip() for e in events_qs.split(",")
        if e.strip() in _DEFAULT_EVENTS
    )


@router.get("/sse/dashboard")
async def sse_dashboard(
    request: Request,
    agent_id: str | None = None,
    minutes: int = 30,
    events: str | None = None,
) -> StreamingResponse:
    """Push rendered HTML fragments whenever the relevant tables change.

    **No initial render.** The page route already renders each fragment
    inline via `{% include "partials/..." %}`, so the browser tab shows
    current data the moment HTML arrives. An "initial render" here
    would re-run ~30 sequential repo queries on the single aiosqlite
    connection at the worst possible time (right when the user clicked
    a heavy page like Mail and the broadcasters + scheduler are also
    polling), starving every other coroutine. We push only deltas.

    Query params:
      agent_id   — when set, the `activity` event renders the per-agent
                   scoped timeline (transcript included). When None, the
                   global cross-agent overview.
      minutes    — activity window in minutes (default 30). Matches the
                   /partials/activity contract.
      events     — CSV subset of event names the caller subscribes to
                   ({"stats", "activity", "agent-status", "health"}).
                   Pages opt OUT of fragments they don't display so the
                   handler doesn't render fragments the DOM will discard.
    """
    bc = dashboard_broadcaster_from(request)
    wanted = _parse_events(events)

    async def event_stream() -> AsyncIterator[str]:
        # Fire one immediate keepalive comment so the browser sees the
        # response headers + body byte right away (otherwise some
        # proxies / browsers wait for the first event before flipping
        # the EventSource to OPEN). Costs nothing.
        yield ": connected\n\n"

        # Cheap initial render: the topnav widgets (health pill,
        # agent-status badge) appear on EVERY page and would otherwise
        # stay at their "checking…" placeholder until the first change.
        # Both share a single DB query (list_active_wakeups), so this
        # is a one-query connect overhead — not the 30-query burst the
        # full initial render was. The expensive fragments
        # (stats / activity) skip this path: they're only shown on
        # their own pages and the page route already inlines them.
        if (EVENT_HEALTH in wanted) or (EVENT_AGENT_STATUS in wanted):
            try:
                repos = repos_from(request)
                active = await list_active_wakeups(repos)
                templates = templates_from(request)
                if EVENT_HEALTH in wanted:
                    yield _sse_format(
                        EVENT_HEALTH,
                        str(templates.get_template(
                            "partials/health.html",
                        ).render(active_count=len(active))),
                    )
                if EVENT_AGENT_STATUS in wanted:
                    yield _sse_format(
                        EVENT_AGENT_STATUS,
                        str(templates.get_template(
                            "partials/agent_status.html",
                        ).render(active_wakeups=active)),
                    )
            except Exception:  # noqa: BLE001
                # Don't tear the stream down for a single broken initial
                # render — the subsequent change events will repopulate.
                pass

        # No broadcaster attached (tests / minimal embedding) — just
        # keepalive until the client disconnects.
        if bc is None:
            while True:
                if await request.is_disconnected():
                    break
                await asyncio.sleep(15.0)
                yield ": keepalive\n\n"
            return

        # Subscribe AND drain the queue. No rendering happens here
        # until the broadcaster publishes a change — the page route
        # already shipped the heavy fragments (stats, activity).
        #
        # Disconnect detection: we poll request.is_disconnected() between
        # iterations on a 2s budget (not 15s). That bounds how long a
        # stale handler can sit subscribed after the browser navigates
        # away. Without this, fast tab-switching accumulates several
        # zombie handlers all subscribed to the broadcaster, all
        # rendering fragments on every change event — N× DB load on the
        # shared aiosqlite connection. (Yield-side disconnect also
        # propagates: writing to a closed connection raises on the
        # `yield` and the finally cleans up.)
        queue = bc.subscribe()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    events = await asyncio.wait_for(queue.get(), timeout=2.0)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                # Sentinel published by broadcaster.stop() during
                # shutdown. Exit immediately so uvicorn's graceful
                # shutdown doesn't wait on us.
                if events is None:
                    break
                # Coalesce a burst into one render pass. If the
                # broadcaster fires 5 ticks while we were rendering the
                # previous batch, drain them all into a union set —
                # rendering each event ONCE instead of five times. This
                # is the second half of the "stale handler causes a
                # query storm" fix.
                stop_signaled = False
                while not queue.empty():
                    try:
                        more = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if more is None:
                        # Mid-drain sentinel: render what we've already
                        # coalesced, then exit on the next outer-loop
                        # iteration.
                        stop_signaled = True
                        break
                    events = events | more
                # Cooperative yield so any pending non-SSE request gets a
                # turn on aiosqlite before we queue 5-10 queries.
                await asyncio.sleep(0)
                for event in events:
                    if await request.is_disconnected():
                        # Bail mid-batch the moment we notice the
                        # client is gone, even if we still had events
                        # queued — those renders would just be wasted.
                        return
                    if event not in wanted:
                        # Page didn't subscribe to this event kind —
                        # skip the render so we don't burn DB queries
                        # producing HTML the DOM would discard.
                        continue
                    renderer = _RENDERERS.get(event)
                    if renderer is None:
                        continue
                    try:
                        html = await renderer(request, agent_id, minutes)
                        yield _sse_format(event, html)
                    except Exception:  # noqa: BLE001
                        continue
                if stop_signaled:
                    break
        finally:
            bc.unsubscribe(queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
