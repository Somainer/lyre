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

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse

from ...persistence.models import Blob, MailboxMessage
from ...persistence.repositories import Repositories
from ...runtime.identity import (
    compose_id,
    is_valid_agent_id,
)

# Hard cap on a single upload. The model burns vision tokens by
# image area, the dashboard SSE wakes up on every mail insert, and
# `mailbox_messages.attachments` is a JSON column on the row — a
# 50MB attachment would bloat all three. 10 MiB is large enough for
# any reasonable screenshot or document; if the owner needs to send
# more they can split or compress.
_MAX_BLOB_BYTES = 10 * 1024 * 1024

# Recognized upload types. Restricting the whitelist matters less for
# correctness (the adapter only knows what to do with images + PDFs)
# than for stopping accidental .zip / executable uploads that would
# pollute the blob store without ever being usable.
_ACCEPTED_MEDIA_TYPES: frozenset[str] = frozenset({
    "image/png", "image/jpeg", "image/jpg",
    "image/gif", "image/webp", "image/heic", "image/heif",
    "application/pdf",
})

router = APIRouter()


async def _personas_for_form(repos: Repositories) -> list[str]:
    """Persona choices shown in the dropdown — every approved persona in
    the DB. Returning a live query (instead of the old hardcoded list)
    means custom personas the owner adds under
    ``~/.lyre/personas/<name>/identity.md`` show up here without any
    code change, alongside the shipped ones.

    Ordering: ``owner`` first (it's the human, not a role); everything
    else alphabetical. Deprecated personas are filtered out by
    list_active's default ``status="approved"`` filter.
    """
    rows = await repos.personas.list_active()
    names = sorted(p.name for p in rows if p.name != "owner")
    return ["owner", *names] if any(p.name == "owner" for p in rows) else names


async def _persona_seed_agent_id(
    repos: Repositories, persona_name: str,
) -> str | None:
    """Resolve persona name → the bootstrap-seeded agent id, or None if
    the persona doesn't have one (``kind == "spawn_only"``).

    SSOT path: the persona's ``display_name`` (from identity.md) is the
    agent id at seed time. Returns the live id by querying the persona
    row (display_name fallback to name) — re-onboards that change
    display_name propagate here on the next call.
    """
    p = await repos.personas.get(persona_name)
    if p is None or p.kind == "spawn_only":
        return None
    return p.display_name or p.name


async def _singleton_persona_names(repos: Repositories) -> list[str]:
    """Persona names whose ``kind == "singleton"`` — exactly one agent
    exists per persona, no spawning. Drives the name-input-disabled JS
    in the send form so the owner can't try to address a non-existent
    ``dispatcher/foo``.
    """
    rows = await repos.personas.list_active()
    return sorted(p.name for p in rows if p.kind == "singleton")


async def _resolve_preset(
    repos: Repositories, preset_to: str | None,
) -> tuple[str, str, str | None]:
    """Decide form defaults (recipient, persona, name) from ?to=...

    No ``to`` → fall back to the dispatcher persona's seeded agent id
    (its display_name, e.g. ``dispatcher`` or ``luna``).  ``to`` with a
    ``/`` splits to persona/name.  Bare ``to`` resolves to its persona
    via the agents table (so an agent with display_name ``luna`` selects
    the ``dispatcher`` row in the dropdown).
    """
    if not preset_to:
        dispatcher = await repos.personas.get("dispatcher")
        if dispatcher is not None:
            seed_id = dispatcher.display_name or dispatcher.name
            return seed_id, "dispatcher", None
        return "owner", "owner", None
    if "/" in preset_to:
        persona, _, name = preset_to.partition("/")
        return preset_to, persona, name or None
    agent = await repos.agents.get(preset_to)
    if agent is not None:
        return preset_to, agent.persona_name, None
    return preset_to, preset_to, None


async def _ensure_agent(
    repos: Repositories, persona: str, name: str,
    *, parent: str = "owner",
) -> str | tuple[None, str]:
    """Compose persona/name → agent_id. Create the agent if missing.

    Returns the agent_id on success or `(None, error)` on failure.

    For ``singleton`` / ``seeded`` personas, blank ``name`` resolves to
    the persona's current bootstrap-seeded agent id (display_name in
    identity.md). For ``spawn_only`` personas a name is required.
    """
    seed_id = await _persona_seed_agent_id(repos, persona)
    if seed_id is not None and not name:
        agent_id = seed_id
    elif not name:
        return None, (
            f"persona {persona!r} needs a name (e.g. 'refactor-auth'); "
            f"only singleton / seeded personas can be addressed without one."
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

    # Bootstrap-seeded agent should already exist; if not it's a setup
    # bug, not a spawn-on-the-fly case.
    if seed_id is not None and agent_id == seed_id:
        return None, f"bootstrap-seeded agent {agent_id!r} missing from DB"

    # Validate persona exists, is approved, and isn't a singleton role
    # (which forbids further spawning).
    persona_row = await repos.personas.get(persona)
    if persona_row is None or persona_row.status != "approved":
        return None, f"persona {persona!r} is not approved"
    if persona_row.kind == "singleton":
        return None, (
            f"persona {persona!r} is a singleton role — its one agent "
            f"already exists and no more can be spawned"
        )

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


@router.get("/send", response_class=HTMLResponse)
async def send_form(
    request: Request,
    to: str | None = None,
    reply_to: int | None = None,
) -> HTMLResponse:
    repos = request.app.state.repos
    templates = request.app.state.templates
    reply_ctx = None
    requested = to
    if reply_to is not None:
        reply_ctx = await _load_reply_context(repos, reply_to)
        if reply_ctx is not None:
            requested = reply_ctx["sender"]
    preset_to, preset_persona, preset_name = await _resolve_preset(repos, requested)
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
            "personas": await _personas_for_form(repos),
            "singleton_personas": await _singleton_persona_names(repos),
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
    # Multimodal upload — accepts 0..N files. Each uploaded file is
    # hashed (sha256) and written via BlobStore; the resulting
    # blob_ids ride alongside the mail in `attachments`. The File()
    # default-arg is FastAPI's idiomatic declaration for a form field
    # (ruff B008 is a false positive against this pattern — the call
    # builds a parameter descriptor, not a shared mutable default).
    attachments: list[UploadFile] = File(default=[]),  # noqa: B008
) -> HTMLResponse:
    templates = request.app.state.templates
    repos = request.app.state.repos
    blob_store = getattr(request.app.state, "blob_store", None)
    spawn = spawn_if_missing in ("1", "on", "true")

    async def render_err(msg: str, status: int = 400) -> HTMLResponse:
        preset_to, preset_persona, preset_name = await _resolve_preset(
            repos, recipient or (compose_id(persona, name or "") if persona else None),
        )
        return templates.TemplateResponse(
            request, "send.html",
            {
                "tab": "send",
                "preset_to": preset_to,
                "preset_persona": preset_persona,
                "preset_name": preset_name,
                "reply_to": reply_to,
                "error": msg,
                "sender_default": sender or "owner",
                "known_agents": await _known_agent_ids(repos),
                "personas": await _personas_for_form(repos),
                "singleton_personas": await _singleton_persona_names(repos),
            },
            status_code=status,
        )

    if urgency not in ("blocker", "high", "normal", "low"):
        return await render_err(f"invalid urgency '{urgency}'")

    if persona:
        if spawn:
            resolved = await _ensure_agent(repos, persona, name or "")
        else:
            seed_id = await _persona_seed_agent_id(repos, persona)
            if seed_id is not None and not (name or "").strip():
                agent_id = seed_id
            else:
                agent_id = compose_id(persona, name or "")
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

    # Process file uploads BEFORE inserting the mail row so a bad
    # upload aborts the whole send rather than silently dropping the
    # attachment and confusing the recipient.
    blob_ids: list[str] = []
    for upload in attachments or []:
        if not upload or not upload.filename:
            continue  # empty form field
        if blob_store is None:
            return await render_err(
                "attachments require BlobStore — pass blob_store= when "
                "creating the app, or restart lyre serve."
            )
        media_type = upload.content_type or "application/octet-stream"
        if media_type not in _ACCEPTED_MEDIA_TYPES:
            return await render_err(
                f"unsupported attachment type {media_type!r} "
                f"({upload.filename}). Allowed: images (PNG/JPG/GIF/"
                f"WebP/HEIC) and PDF."
            )
        data = await upload.read()
        if len(data) > _MAX_BLOB_BYTES:
            return await render_err(
                f"attachment {upload.filename!r} is "
                f"{len(data) // 1024} KiB; cap is "
                f"{_MAX_BLOB_BYTES // 1024 // 1024} MiB. Compress or "
                f"split."
            )
        blob_id = blob_store.write(data, media_type)
        await repos.blobs.upsert(Blob(
            id=blob_id,
            media_type=media_type,
            size_bytes=len(data),
            filename=upload.filename,
            source=sender or "owner",
        ))
        blob_ids.append(blob_id)

    await repos.mailbox.ensure_mailbox(final_recipient)
    msg = MailboxMessage(
        recipient=final_recipient,
        external_id=f"dashboard:{uuid.uuid4()}",
        sender=sender,
        urgency=urgency,  # type: ignore[arg-type]
        title=title.strip() or None,
        body=body,
        parent_msg_id=reply_to,
        attachments=blob_ids or None,
    )
    msg_id = await repos.mailbox.insert_message(msg)

    success = (
        f"sent [{msg_id}] {urgency} from {sender} → {final_recipient}"
        + (f" (in reply to #{reply_to})" if reply_to else "")
        + (f" — {len(blob_ids)} attachment(s)" if blob_ids else "")
    )
    preset_to, preset_persona, preset_name = await _resolve_preset(repos, final_recipient)
    return templates.TemplateResponse(
        request, "send.html",
        {
            "tab": "send",
            "preset_to": preset_to,
            "preset_persona": preset_persona,
            "preset_name": preset_name,
            "reply_to": None,
            "success": success,
            "sender_default": sender or "owner",
            "known_agents": await _known_agent_ids(repos),
            "personas": await _personas_for_form(repos),
            "singleton_personas": await _singleton_persona_names(repos),
        },
    )


