"""Lyre Dashboard — FastAPI + HTMX + SSE owner UI (Sprint D1)."""

from .app import create_app
from .sse import MailboxBroadcaster

__all__ = ["MailboxBroadcaster", "create_app", "run_dashboard"]


# Re-exported lazily to avoid pulling uvicorn at import-time of code that
# only wants `create_app` (e.g. tests).
def run_dashboard(*args, **kwargs):  # type: ignore[no-untyped-def]
    from .runner import run_dashboard as _run

    return _run(*args, **kwargs)
