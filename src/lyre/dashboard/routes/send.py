"""Send-message form — owner writes to an agent's mailbox.

Mirrors the `lyre send` CLI; bypasses outbox because owner sits at the
system edge (no wakeup attribution). Single-recipient form for now —
the persona/name + spawn-if-missing UX lands as a follow-up that wires
the form to the agent addressing primitives.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ...persistence.models import MailboxMessage

router = APIRouter()


async def _validate_recipient(repos, recipient: str) -> str | None:
    """Return None if valid, else an error string. 'owner' is always
    valid; anything else must be a known (non-archived) agent — matches
    the CLI validation that catches hallucination / typo'd recipients."""
    if recipient == "owner":
        return None
    if not await repos.agents.exists(recipient):
        live = sorted(
            {a.id for a in await repos.agents.list_all()} | {"owner"}
        )
        return (
            f"unknown agent {recipient!r}. Known: {live}. Pass an "
            f"existing agent id; create one first if needed."
        )
    return None


async def _load_reply_context(repos, reply_to_id: int) -> dict | None:
    msg = await repos.mailbox.get_message(reply_to_id)
    if msg is None:
        return None
    body = msg.body or ""
    return {
        "id": msg.id,
        "sender": msg.sender,
        "recipient": msg.recipient,
        "urgency": msg.urgency,
        "title": msg.title,
        "preview": body if len(body) <= 400 else body[:400] + "…",
    }


async def _known_agent_ids(repos) -> list[str]:
    return sorted(
        {a.id for a in await repos.agents.list_all(include_archived=False)}
    )


@router.get("/send", response_class=HTMLResponse)
async def send_form(
    request: Request,
    to: str = "leader",
    reply_to: int | None = None,
) -> HTMLResponse:
    repos = request.app.state.repos
    templates = request.app.state.templates
    reply_ctx = None
    preset_to = to
    if reply_to is not None:
        reply_ctx = await _load_reply_context(repos, reply_to)
        if reply_ctx is not None:
            preset_to = reply_ctx["sender"]
    return templates.TemplateResponse(
        request, "send.html",
        {
            "tab": "send",
            "preset_to": preset_to,
            "reply_to": reply_to,
            "reply_ctx": reply_ctx,
            "sender_default": "owner",
            "known_agents": await _known_agent_ids(repos),
        },
    )


@router.post("/send", response_class=HTMLResponse)
async def send_post(
    request: Request,
    recipient: str = Form(...),
    body: str = Form(...),
    title: str = Form(""),
    urgency: str = Form("normal"),
    sender: str = Form("owner"),
    reply_to: int | None = Form(None),
) -> HTMLResponse:
    templates = request.app.state.templates
    repos = request.app.state.repos

    if urgency not in ("blocker", "high", "normal", "low"):
        return templates.TemplateResponse(
            request, "send.html",
            {
                "tab": "send",
                "preset_to": recipient,
                "reply_to": reply_to,
                "error": f"invalid urgency '{urgency}'",
                "sender_default": sender or "owner",
                "known_agents": await _known_agent_ids(repos),
            },
            status_code=400,
        )

    err = await _validate_recipient(repos, recipient)
    if err is not None:
        return templates.TemplateResponse(
            request, "send.html",
            {
                "tab": "send",
                "preset_to": recipient,
                "reply_to": reply_to,
                "error": err,
                "sender_default": sender or "owner",
                "known_agents": await _known_agent_ids(repos),
            },
            status_code=400,
        )

    await repos.mailbox.ensure_mailbox(recipient)
    msg = MailboxMessage(
        recipient=recipient,
        external_id=f"dashboard:{uuid.uuid4()}",
        sender=sender,
        urgency=urgency,  # type: ignore[arg-type]
        title=title.strip() or None,
        body=body,
        parent_msg_id=reply_to,
    )
    msg_id = await repos.mailbox.insert_message(msg)

    success = (
        f"sent [{msg_id}] {urgency} from {sender} → {recipient}"
        + (f" (in reply to #{reply_to})" if reply_to else "")
    )
    return templates.TemplateResponse(
        request, "send.html",
        {
            "tab": "send",
            "preset_to": recipient,
            "reply_to": None,
            "success": success,
            "sender_default": sender or "owner",
            "known_agents": await _known_agent_ids(repos),
        },
    )
