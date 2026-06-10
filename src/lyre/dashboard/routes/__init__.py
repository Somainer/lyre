"""Dashboard route modules — one tab per file, plus SSE + send-form."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import Request
    from fastapi.templating import Jinja2Templates

    from ...persistence.repositories import Repositories
    from ..activity import LiveTranscriptFolder
    from ..dashboard_broadcaster import DashboardBroadcaster
    from ..sse import MailboxBroadcaster


# These accessors exist purely as a typed front-door for the dynamic
# ``request.app.state.*`` attributes that the app factory populates.
# ``State`` is intentionally untyped (it stores arbitrary objects), so
# every route that read off it leaked ``Any`` into otherwise-typed code
# — the casts here pin the type once per accessor and let downstream
# code stay strict.


def templates_from(request: Request) -> Jinja2Templates:
    return cast("Jinja2Templates", request.app.state.templates)


def repos_from(request: Request) -> Repositories:
    return cast("Repositories", request.app.state.repos)


def broadcaster_from(request: Request) -> MailboxBroadcaster:
    return cast("MailboxBroadcaster", request.app.state.broadcaster)


def dashboard_broadcaster_from(
    request: Request,
) -> DashboardBroadcaster | None:
    return cast(
        "DashboardBroadcaster | None",
        request.app.state.dashboard_broadcaster,
    )


def object_store_root_from(request: Request) -> Path | None:
    return cast(
        "Path | None",
        getattr(request.app.state, "object_store_root", None),
    )


def live_folders_from(request: Request) -> dict[str, LiveTranscriptFolder]:
    """The dashboard broadcaster's per-active-wakeup streaming state —
    empty when no broadcaster is attached (tests / minimal embedding),
    in which case renderers fall back to a bounded file tail."""
    bc = dashboard_broadcaster_from(request)
    return bc.live_folders() if bc is not None else {}
