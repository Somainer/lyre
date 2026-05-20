"""Markdown rendering in dashboard mail bodies.

Mail bodies the model writes are markdown (headers, bold, code spans,
bullet lists, …). Rendering them raw as `<pre>` shows the asterisks +
backticks as literal junk. The dashboard registers a Jinja `markdown`
filter that runs the body through `markdown-it-py` and emits safe HTML.

Two invariants that this file pins:
  1. Markdown actually renders (bold → <strong>, lists → <ul>/<li>, etc.)
  2. Raw HTML in the source is **escaped**, not passed through — even a
     malicious mail body must not be able to inject `<script>` into the
     dashboard.
"""

from __future__ import annotations

import asyncio as _asyncio
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from lyre.dashboard.app import create_app
from lyre.dashboard.sse import MailboxBroadcaster
from lyre.persistence.db import init_db
from lyre.persistence.models import MailboxMessage, Persona
from lyre.persistence.sqlite_impl import SqliteRepositories


@pytest.fixture
def client_with_mail(tmp_path: Path):
    """Spin a dashboard with a couple of mail rows whose bodies contain
    rich markdown + one with raw HTML for the XSS check."""

    async def _setup():
        db = tmp_path / "lyre.db"
        conn = await init_db(db)
        repos = SqliteRepositories(conn)
        await repos.personas.upsert(
            Persona(name="dispatcher", role_description="lead", system_prompt="b")
        )
        await repos.agents.create(agent_id="dispatcher", persona_name="dispatcher")
        await repos.mailbox.ensure_mailbox("owner")
        # Rich markdown body — the kind dispatcher actually writes in replies
        await repos.mailbox.insert_message(
            MailboxMessage(
                recipient="owner", external_id="md-rich",
                sender="dispatcher", urgency="normal",
                title="Pi research summary",
                body=(
                    "**Pi** = github.com/earendil-works/pi\n\n"
                    "- Skills system\n"
                    "- Global facts/skills/soul\n"
                    "- Progressive disclosure\n\n"
                    "Path: `~/.lyre/memory/facts/specs-pi-research.md`\n"
                ),
            )
        )
        # Hostile body — the model (or compromised sender) tries to
        # inject script tags. Filter MUST escape, not pass through.
        await repos.mailbox.insert_message(
            MailboxMessage(
                recipient="owner", external_id="md-xss",
                sender="dispatcher", urgency="normal",
                title="hostile",
                body='<script>alert("pwned")</script>\n\n**bold** still works',
            )
        )
        broadcaster = MailboxBroadcaster(
            repos=repos, recipient="owner", poll_interval_s=0.05,
        )
        await broadcaster.prime()
        app = create_app(repos, broadcaster)
        return app, conn

    app, conn = _asyncio.get_event_loop().run_until_complete(_setup())
    client = TestClient(app)
    try:
        yield client
    finally:
        _asyncio.get_event_loop().run_until_complete(conn.close())


def test_mail_list_shows_body_as_plain_text(client_with_mail: TestClient) -> None:
    """Mail list bodies render as PLAIN TEXT in <pre>, not as rendered
    markdown. Two reasons:

    1. Per-row CommonMark parsing was a hot path that blocked the
       event loop on busy mailboxes — the user reported tab-switch
       freezes that pointed here.
    2. The user's stated goal is *copying body text out*. Plain text
       in <pre> copies cleanly; rendered markdown HTML pollutes the
       paste with `<strong>` / `<ul>` tags depending on selection.

    The `markdown` filter is still registered (mail bodies *may* be
    rendered elsewhere — e.g. a future detail view) — this test just
    pins the list behavior so the perf fix doesn't regress.
    """
    r = client_with_mail.get("/mail")
    assert r.status_code == 200
    body = r.text
    # Raw markdown chars survive into the rendered HTML (auto-escaped
    # by Jinja so they're safe, but visible as authored).
    assert "**Pi**" in body
    # The fast-path <pre> container is in place.
    assert "mail-body-text" in body
    # And rendered-markdown tags must NOT be there (this list view
    # never invokes the markdown filter).
    assert "<strong>Pi</strong>" not in body
    assert "<li>Skills system</li>" not in body


def test_mail_escapes_raw_html_no_script_tag(
    client_with_mail: TestClient,
) -> None:
    """Whether the body is rendered as markdown HTML or plain `<pre>`
    text, Jinja's auto-escape (or our explicit escape) must turn raw
    HTML in the source into entity-encoded text. A hostile
    `<script>alert("pwned")</script>` body must never appear as a live
    `<script>` element in the response."""
    r = client_with_mail.get("/mail")
    body = r.text
    # Raw <script> must NOT appear anywhere in the rendered response.
    assert "<script>alert" not in body
    # Entity-encoded form is what we expect (Jinja auto-escapes the
    # body when interpolating into the <pre>).
    assert "&lt;script&gt;" in body or "&lt;script" in body


def test_mail_detail_renders_markdown(client_with_mail: TestClient) -> None:
    """/mail/<id> is the detail view that re-introduces full markdown
    rendering. The list view (/mail) keeps bodies as fast plain
    <pre>; this route is where the user lands when they click a row
    title and wants formatted output (or to copy the raw body via the
    one-click button)."""
    # The fixture seeds two messages — md-rich (markdown content) and
    # md-xss (hostile content). The list is newest-first, so we can't
    # just take the first href; find the link sitting under the
    # "Pi research summary" title instead.
    import re
    list_resp = client_with_mail.get("/mail")
    # The title link sits next to the title text; match the href whose
    # text content is the rich title.
    match = re.search(
        r'href="/mail/(\d+)"[^>]*>Pi research summary',
        list_resp.text,
    )
    assert match is not None, (
        "list page must link rows to /mail/<id> with the title as text"
    )
    msg_id = int(match.group(1))

    r = client_with_mail.get(f"/mail/{msg_id}")
    assert r.status_code == 200
    body = r.text
    # Rendered markdown — same assertions the list test used to hold.
    assert "<strong>Pi</strong>" in body
    assert "<ul>" in body and "<li>Skills system</li>" in body
    assert "<code>~/.lyre/memory/facts/specs-pi-research.md</code>" in body
    # Copy-raw button + the raw <pre> it targets must both exist.
    assert "mail-copy-btn" in body
    assert 'id="mail-raw-body"' in body
    # Reply link points back at the sender via the standard reply_to flow.
    assert f"/send?reply_to={msg_id}" in body


def test_mail_detail_404_for_unknown_id(client_with_mail: TestClient) -> None:
    r = client_with_mail.get("/mail/999999")
    assert r.status_code == 404
