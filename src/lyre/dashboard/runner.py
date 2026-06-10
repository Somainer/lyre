"""Embed-friendly dashboard launcher.

Used by both `lyre dashboard` (standalone) and `lyre serve` (alongside
scheduler + outbox dispatcher). Returns a coroutine you can `await` /
`asyncio.gather` with the other long-lived tasks.

The broadcaster's poll loop and uvicorn's serve loop are independent
asyncio tasks; both share the same Repositories handle.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import structlog
import uvicorn

from ..persistence.repositories import Repositories
from . import MailboxBroadcaster, create_app
from .dashboard_broadcaster import DashboardBroadcaster

log = structlog.get_logger()


async def run_dashboard(
    repos: Repositories,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    stop_event: asyncio.Event | None = None,
    poll_interval_s: float = 0.5,
    on_ready: Callable[[str], None] | None = None,
    model_context_windows: dict[str, int] | None = None,
    owner_name: str | None = None,
    blob_store: object | None = None,
    object_store_root: Path | None = None,
) -> None:
    """Start the broadcaster + uvicorn server until `stop_event` is set
    (or the server otherwise exits). Designed for two callers:

      - `lyre dashboard` — passes no stop_event, registers SIGINT to flip
        `server.should_exit`
      - `lyre serve` — passes a shared stop_event so the dashboard halts
        alongside scheduler + dispatcher when the user Ctrl-C's

    `on_ready` is called once with the URL right before uvicorn starts —
    useful for printing "Lyre dashboard at http://..." in both contexts.
    """
    broadcaster = MailboxBroadcaster(
        repos=repos, recipient="owner", poll_interval_s=poll_interval_s
    )
    await broadcaster.prime()
    await broadcaster.start()

    # Dashboard-wide change broadcaster replaces the per-element HTMX
    # polls (stats / activity / agent-status / health). 1s interval is
    # plenty for owner observation; the broadcaster only emits when a
    # high-water mark actually moves (live wakeup cards: when the
    # transcript grew).
    dashboard_bc = DashboardBroadcaster(
        repos=repos, poll_interval_s=1.0, object_store_root=object_store_root,
    )
    await dashboard_bc.prime()
    await dashboard_bc.start()

    app = create_app(
        repos, broadcaster,
        dashboard_broadcaster=dashboard_bc,
        model_context_windows=model_context_windows,
        owner_name=owner_name,
        blob_store=blob_store,
        object_store_root=object_store_root,
    )
    # lifespan="off": we don't use ASGI lifespan messages (the handler in
    # app.py is a no-op `yield`). With it on, Starlette's lifespan task
    # awaits a receive_queue.get() that uvicorn cancels at shutdown,
    # producing a noisy `asyncio.CancelledError` traceback even though
    # shutdown succeeds. Turning lifespan off removes the noise without
    # changing behavior (we manage broadcaster lifecycle here ourselves).
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
        lifespan="off",
    )
    server = uvicorn.Server(config)

    if on_ready is not None:
        on_ready(f"http://{host}:{port}")

    async def _watch_stop() -> None:
        """Wake SSE subscribers the moment shutdown begins.

        Polls both `stop_event` (set by `lyre serve` on Ctrl-C) and
        uvicorn's own `server.should_exit` (set by uvicorn's signal
        handler when run standalone via `lyre dashboard`). Whichever
        trips first, we immediately stop the broadcasters so they push
        the None sentinel to every subscribed SSE handler. Without
        this, uvicorn's graceful shutdown blocks waiting for handlers
        to notice via their 2s queue.get timeout — a visible 2-5s
        pause on exit.
        """
        while not server.should_exit:
            if stop_event is not None and stop_event.is_set():
                server.should_exit = True
                break
            await asyncio.sleep(0.1)
        # Shutdown begun — wake subscribers so they exit cleanly.
        # broadcaster.stop() is idempotent, so the finally below
        # remains safe.
        await broadcaster.stop()
        await dashboard_bc.stop()

    watcher = asyncio.create_task(_watch_stop(), name="dashboard_stop_watch")

    try:
        await server.serve()
    finally:
        watcher.cancel()
        try:
            await watcher
        except (asyncio.CancelledError, Exception):
            pass
        # Idempotent — already called from the watcher when shutdown
        # was detected. This handles the rare case where serve() exits
        # without the watcher having noticed (e.g. unhandled exception
        # from uvicorn itself).
        await broadcaster.stop()
        await dashboard_bc.stop()


async def serve_until_signal(
    coro: Coroutine[Any, Any, None],
    stop_event: asyncio.Event,
) -> None:
    """Run `coro` in an asyncio task; resolve when either it finishes or
    `stop_event` is set. Useful for composing services in `lyre serve`."""
    inner: asyncio.Task[None] = asyncio.create_task(coro)
    waiter = asyncio.create_task(stop_event.wait())
    done, pending = await asyncio.wait(
        {inner, waiter}, return_when=asyncio.FIRST_COMPLETED
    )
    for t in pending:
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    if inner in done:
        # Surface any exception from the inner coro.
        inner.result()
