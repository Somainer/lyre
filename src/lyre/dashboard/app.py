"""FastAPI app factory.

Mounts read-only routes for Home / Inbox / Feed / Tasks / Wakeups, an SSE
endpoint for mailbox push, a tiny send-message form (Sprint D1 minimum-write
seam), and static assets. The same `SqliteRepositories` instance is shared
with Scheduler / OutboxDispatcher — SQLite WAL handles single-process,
multi-coroutine concurrency.

Sprint D2 will fold reply / approve / dispatch / cancel into the same shape.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markupsafe import Markup

from ..persistence.repositories import Repositories
from .dashboard_broadcaster import DashboardBroadcaster
from .routes import (
    activity,
    agents,
    blobs,
    home,
    mail,
    runs,
    send,
    sse_route,
)
from .sse import MailboxBroadcaster
from .view_helpers import (
    clock_time,
    context_peak_pct,
    fmt_ms,
    fmt_tokens,
    rel_time,
)

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

# CommonMark renderer with strikethrough + autolink. `html=False` makes
# raw HTML in source render as ESCAPED text (the literal `<script>` etc.),
# so even mail bodies authored by the model can't inject script tags into
# the dashboard. Strikethrough + linkify are useful (~~done~~ and bare
# URLs) and don't open new attack surface.
_MD = MarkdownIt("commonmark", {"html": False, "linkify": True}).enable(
    ["strikethrough", "table"]
)


def _render_markdown(text: str | None) -> Markup:
    """Jinja filter — text → safe HTML. Empty / None → empty string."""
    if not text:
        return Markup("")
    return Markup(_MD.render(text))


def create_app(
    repos: Repositories,
    broadcaster: MailboxBroadcaster,
    *,
    dashboard_broadcaster: DashboardBroadcaster | None = None,
    model_context_windows: dict[str, int] | None = None,
    owner_name: str | None = None,
    blob_store: Any = None,
    object_store_root: Path | None = None,
) -> FastAPI:
    """`model_context_windows` is a `{model_id_or_alias: context_window_tokens}`
    map used by the activity feed to compute "context usage %" for each
    wakeup. Either pass it explicitly (production), or leave None and
    the dashboard will show absolute token counts only (tests).

    `dashboard_broadcaster` drives the SSE event stream that replaces
    per-element HTMX polling. Optional for tests / minimal embedding —
    when None, the /sse/dashboard endpoint returns a stub that just
    keepalive-pings.
    """
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Broadcaster lifecycle is managed externally (by `lyre serve`)
        # because it shares the asyncio loop with Scheduler etc.; we just
        # validate it's ready.
        yield

    app = FastAPI(title="Lyre Dashboard", lifespan=lifespan)
    app.state.repos = repos
    app.state.broadcaster = broadcaster
    app.state.dashboard_broadcaster = dashboard_broadcaster
    # Object-store root for deriving ACTIVE wakeups' transcript paths
    # (their transcript_uri column is NULL until end-of-wakeup). None →
    # live cards degrade to transcript_uri-based fallback (ended only).
    app.state.object_store_root = object_store_root
    app.state.model_context_windows = model_context_windows or {}
    app.state.owner_name = owner_name
    # Multimodal: optional BlobStore used by /send (upload) and the
    # mail-detail / /blobs/<id> routes. None disables those features
    # gracefully — useful for tests that don't exercise multimodal.
    app.state.blob_store = blob_store
    app.state.templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    env = app.state.templates.env
    env.filters["markdown"] = _render_markdown
    env.filters["rel_time"] = rel_time
    env.filters["clock_time"] = clock_time
    env.filters["fmt_tokens"] = fmt_tokens
    env.filters["fmt_ms"] = fmt_ms
    env.filters["context_peak_pct"] = lambda peak, window: context_peak_pct(peak, window)

    app.mount(
        "/static",
        StaticFiles(directory=str(STATIC_DIR)),
        name="static",
    )

    app.include_router(home.router)
    app.include_router(activity.router)
    app.include_router(agents.router)
    app.include_router(blobs.router)
    app.include_router(mail.router)
    app.include_router(runs.router)
    app.include_router(send.router)
    app.include_router(sse_route.router)

    return app
