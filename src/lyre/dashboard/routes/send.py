"""Send-message form — owner writes to an agent's mailbox.

Mirrors `lyre send`; bypasses outbox because owner sits at the system
edge (no wakeup attribution). The form takes persona + name and composes
`<persona>/<name>` server-side. If `spawn_if_missing` is checked and the
composed agent_id doesn't exist yet, the route creates the agent with
parent_agent_id="owner" before delivering. ?reply_to=<id> pre-fills
recipient + threads parent_msg_id.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ...persistence.models import MailboxMessage
from ...runtime.identity import (
    compose_id,
    is_bootstrap,
    is_valid_agent_id,
    split_id,
)

router = APIRouter()


def _personas_for_form() -> list[str]:
    """Persona choices shown in the dropdown. Bootstrap personas
    (`owner`, `leader`) appear so the owner can send to them; the rest
    cover the shipped persona set."""
    return [
        "owner",
        "leader",
        "worker-maintainer",
        "reviewer-skill",
        "reviewer-pr",
        "summary-agent",
    ]


_BOOTSTRAP = ("owner", "leader")


async def _ensure_agent(
    repos, persona: str, name: str, *, parent: str = "owner"
) -> str | tuple[None, str]:
    """Compose persona/name → agent_id. Create the agent if missing.

    Returns the agent_id on success or `(None, error)` on failure.
    """
    if persona in _BOOTSTRAP:
        agent_id = persona  # name ignored for bootstrap
    elif not name:
        return None, (
            f"persona {persona!r} needs a name (e.g. 'refactor-auth'); "
            f"leave it blank only for bootstrap personas (owner, leader)."
        )
    else:
        agent_id = compose_id(persona, name)

    if not is_valid_agent_id(agent_id):
        return None, (
            f"invalid agent id {agent_id!r}: lowercase letters, digits, "
            f"hyphens only; persona segment must start with a letter."
        )

    if await repos.agents.exists(agent_id):
        return agent_id

    # Bootstrap should already exist; if not it's a setup bug, not a
    # spawn-on-the-fly case.
    if persona in _BOOTSTRAP:
        return None, f"bootstrap agent {agent_id!r} missing from DB"

    # Validate persona exists & is approved before spawning.
    persona_row = await repos.personas.get(persona)
    if persona_row is None or persona_row.status != "approved":
        return None, f"persona {persona!r} is not approved"

    await repos.agents.create(
        agent_id=agent_id,
        persona_name=persona,
        parent_agent_id=parent,
    )
    return agent_id


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


def _split_preset(preset_to: str) -> tuple[str, str | None]:
    """Initial values for the persona+name pair when the form is opened
    with ?to=<existing-agent-id>. Bootstrap stays bare; spawned splits
    on /. Unknown shapes fall back to leader."""
    if not preset_to:
        return "leader", None
    if is_bootstrap(preset_to):
        return preset_to, None
    persona, name = split_id(preset_to)
    return persona, name


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
    preset_persona, preset_name = _split_preset(preset_to)
    return templates.TemplateResponse(
        request, "send.html",
        {
            "tab": "send",
            "preset_to": preset_to,
            "preset_persona": preset_persona,
            "preset_name": preset_name,
            "reply_to": reply_to,
            "reply_ctx": reply_ctx,
            "sender_default": "owner",
            "known_agents": await _known_agent_ids(repos),
            "personas": _personas_for_form(),
            "bootstrap_personas": list(_BOOTSTRAP),
        },
    )


@router.post("/send", response_class=HTMLResponse)
async def send_post(
    request: Request,
    body: str = Form(...),
    title: str = Form(""),
    urgency: str = Form("normal"),
    sender: str = Form("owner"),
    reply_to: int | None = Form(None),
    # New form fields (persona/name composition). `recipient` accepted as
    # a fallback so callers that still POST a single field (e.g. tests
    # or the CLI's reply link) keep working.
    persona: str | None = Form(None),
    name: str | None = Form(None),
    recipient: str | None = Form(None),
    spawn_if_missing: str | None = Form(None),
) -> HTMLResponse:
    templates = request.app.state.templates
    repos = request.app.state.repos
    spawn = spawn_if_missing in ("1", "on", "true")

    async def render_err(msg: str, status: int = 400) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "send.html",
            {
                "tab": "send",
                "preset_to": recipient or compose_id(persona or "leader", name or ""),
                "preset_persona": persona or "leader",
                "preset_name": name,
                "reply_to": reply_to,
                "error": msg,
                "sender_default": sender or "owner",
                "known_agents": await _known_agent_ids(repos),
                "personas": _personas_for_form(),
                "bootstrap_personas": list(_BOOTSTRAP),
            },
            status_code=status,
        )

    if urgency not in ("blocker", "high", "normal", "low"):
        return await render_err(f"invalid urgency '{urgency}'")

    if persona:
        if spawn:
            resolved = await _ensure_agent(repos, persona, name or "")
        else:
            agent_id = persona if persona in _BOOTSTRAP else compose_id(persona, name or "")
            if not is_valid_agent_id(agent_id):
                resolved = (None, f"invalid agent id {agent_id!r}")
            elif not await repos.agents.exists(agent_id):
                resolved = (None, f"unknown agent {agent_id!r} (spawn disabled)")
            else:
                resolved = agent_id
    elif recipient:
        # Fallback path: caller passed a flat recipient. Owner is always
        # valid; otherwise must exist.
        if recipient == "owner" or await repos.agents.exists(recipient):
            resolved = recipient
        else:
            live = sorted(
                {a.id for a in await repos.agents.list_all()} | {"owner"}
            )
            resolved = (None, (
                f"unknown agent {recipient!r}. Known: {live}. Pass an "
                f"existing agent id; create one first if needed."
            ))
    else:
        return await render_err("recipient required (persona+name or recipient)")

    if isinstance(resolved, tuple):
        return await render_err(resolved[1])
    final_recipient = resolved

    await repos.mailbox.ensure_mailbox(final_recipient)
    msg = MailboxMessage(
        recipient=final_recipient,
        external_id=f"dashboard:{uuid.uuid4()}",
        sender=sender,
        urgency=urgency,  # type: ignore[arg-type]
        title=title.strip() or None,
        body=body,
        parent_msg_id=reply_to,
    )
    msg_id = await repos.mailbox.insert_message(msg)

    success = (
        f"sent [{msg_id}] {urgency} from {sender} → {final_recipient}"
        + (f" (in reply to #{reply_to})" if reply_to else "")
    )
    preset_persona, preset_name = _split_preset(final_recipient)
    return templates.TemplateResponse(
        request, "send.html",
        {
            "tab": "send",
            "preset_to": final_recipient,
            "preset_persona": preset_persona,
            "preset_name": preset_name,
            "reply_to": None,
            "success": success,
            "sender_default": sender or "owner",
            "known_agents": await _known_agent_ids(repos),
            "personas": _personas_for_form(),
            "bootstrap_personas": list(_BOOTSTRAP),
        },
    )


