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
            Persona(name="leader", role_description="lead", system_prompt="b")
        )
        await repos.agents.create(agent_id="leader", persona_name="leader")
        await repos.mailbox.ensure_mailbox("owner")
        # Rich markdown body — the kind leader actually writes in replies
        await repos.mailbox.insert_message(
            MailboxMessage(
                recipient="owner", external_id="md-rich",
                sender="leader", urgency="normal",
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
                sender="leader", urgency="normal",
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


def test_inbox_renders_markdown_bold_and_list(client_with_mail: TestClient) -> None:
    r = client_with_mail.get("/inbox")
    assert r.status_code == 200
    body = r.text
    # Bold + list + inline code all rendered
    assert "<strong>Pi</strong>" in body
    assert "<ul>" in body and "<li>Skills system</li>" in body
    assert "<code>~/.lyre/memory/facts/specs-pi-research.md</code>" in body
    # The raw markdown characters must NOT leak through unrendered
    assert "**Pi**" not in body
    # The container class is in place so CSS picks it up
    assert "md-body" in body


def test_inbox_escapes_raw_html_no_script_tag(
    client_with_mail: TestClient,
) -> None:
    """The hostile body contains a <script> tag. Renderer must escape
    it — only the entity-encoded form may appear, never an actual
    `<script>` element."""
    r = client_with_mail.get("/inbox")
    body = r.text
    # Raw <script> must NOT appear anywhere in the rendered response.
    assert "<script>alert" not in body
    # Entity-encoded form is what we expect (markdown-it-py with
    # html=False renders raw HTML as escaped text).
    assert "&lt;script&gt;" in body or "&lt;script" in body
    # And the markdown around it still works
    assert "<strong>bold</strong>" in body
