"""Dashboard route modules — one tab per file, plus SSE + send-form."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from fastapi import Request
    from fastapi.templating import Jinja2Templates

    from ...persistence.repositories import Repositories
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
