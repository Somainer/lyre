"""Mailbox tools: mailbox_send / mailbox_read / mark_read / mailbox_get_message.

Send goes through outbox (Tx boundary §3.2) so that a crash between the LLM
returning a tool_use and the actual delivery doesn't lose the message — the
outbox dispatcher will pick it up on restart.

External_id is deterministic on (wakeup_id, tool_use_id) so retries are
idempotent: same logical send produces the same outbox row.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ...persistence.models import OutboxRow, ScheduledMail
from ..future_mail import (
    PastDeliveryError,
    default_recur_until,
    iso,
    now_utc,
    parse_duration,
    resolve_first_fire,
    validate_cron,
)
from . import Tool, ToolContext, ToolError


async def _mailbox_send(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    raw_to = args.get("to")
    body = args.get("body")
    urgency = args.get("urgency", "normal")
    raw_title = args.get("title")

    # `to` accepts a single string or a list of strings (broadcast).
    if isinstance(raw_to, str):
        recipients = [raw_to]
    elif isinstance(raw_to, list) and all(isinstance(x, str) for x in raw_to):
        recipients = list(dict.fromkeys(raw_to))  # dedupe, preserve order
    else:
        raise ToolError("'to' must be a string or a list of strings")
    if not recipients:
        raise ToolError("'to' is empty")
    self_mailbox = ctx.self_mailbox
    if self_mailbox in recipients:
        raise ToolError(
            f"refusing to send to self ({self_mailbox}); drop yourself "
            f"from the recipient list"
        )

    # Recipients are AGENT IDs (post-A3). Validate every one against the
    # agents table — surfaces typos and model hallucinations (e.g. inventing
    # `dispatcher-scheduler`) as a tool error, so the agent loop hands the
    # model the error and lets it retry on the next turn instead of
    # silently dropping mail into a non-existent inbox.
    known: set[str] = {a.id for a in await ctx.repos.agents.list_all()}
    known.add("owner")  # bootstrap fallback if owner agent isn't registered
    unknown = [r for r in recipients if r not in known]
    if unknown:
        raise ToolError(
            f"unknown recipient(s) {unknown}. These must be AGENT IDs, not "
            f"persona names. Known: {sorted(known)}. Use list_agents() to "
            f"see live agents, or create_agent() if you need a fresh one. "
            f"Do NOT invent names."
        )

    if not body or not isinstance(body, str):
        raise ToolError("missing 'body'")
    if urgency not in ("blocker", "high", "normal", "low"):
        raise ToolError(
            f"invalid urgency '{urgency}' (allowed: blocker/high/normal/low)"
        )

    # Title: agent should always provide one — readers see ONLY the title in
    # `mailbox_read` listings. If absent we silently derive from body's first
    # non-empty line (no LLM call — Lyre stays deterministic + cache-friendly).
    title: str | None
    if raw_title is None:
        title = None  # repository will derive
    elif not isinstance(raw_title, str):
        raise ToolError("title must be a string if provided")
    elif len(raw_title) > 140:
        raise ToolError(
            f"title exceeds 140 chars ({len(raw_title)}). Keep it subject-"
            f"line short — readers see this in inbox listings."
        )
    else:
        title = raw_title.strip() or None

    reply_to = args.get("reply_to")
    if reply_to is not None and not isinstance(reply_to, int):
        raise ToolError("reply_to must be an integer msg_id")
    forward_msg_id = args.get("forward_msg_id")
    if forward_msg_id is not None and not isinstance(forward_msg_id, int):
        raise ToolError("forward_msg_id must be an integer msg_id")

    # Attachments: list of existing blob_ids the agent has seen (via
    # mail it received). Forwarding-only by construction — the model
    # can't fabricate a sha256 it hasn't been shown, so an existence
    # check IS the trust boundary. Verify every id exists in the
    # blobs table; refuse the whole send if any are missing.
    raw_attachments = args.get("attachments")
    attachments: list[str] | None = None
    if raw_attachments is not None:
        if (
            not isinstance(raw_attachments, list)
            or not all(isinstance(a, str) for a in raw_attachments)
        ):
            raise ToolError(
                "attachments must be a list of blob_id strings"
            )
        if raw_attachments:
            present = {
                b.id for b in
                await ctx.repos.blobs.list_ids(list(raw_attachments))
            }
            missing = [a for a in raw_attachments if a not in present]
            if missing:
                raise ToolError(
                    f"unknown attachment blob_id(s): {missing}. "
                    f"Attachments can only reference blobs you've "
                    f"seen in mail you received — you can't manufacture "
                    f"new bytes here. Use the blob_id strings from a "
                    f"prior mailbox_get_message."
                )
            attachments = list(raw_attachments)

    tool_use_id = args.get("_tool_use_id")
    if not tool_use_id:
        raise ToolError("internal: missing tool_use_id (agent loop bug)")

    # ------------------------------------------------------------------
    # Scheduling branch — agent passed any of deliver_*/recur_* params.
    # Diverts off the immediate-outbox path to scheduled_mail table; the
    # scheduler's Phase -1 will deliver when due.
    # ------------------------------------------------------------------
    if _has_scheduling_args(args):
        return await _schedule_future_mail(
            ctx, args, recipients, body, urgency,
            title=title,
            reply_to=reply_to,
            forward_msg_id=forward_msg_id,
            user_meta=args.get("metadata") or {},
        )

    # When fanout > 1, mint a broadcast_id and stamp every copy.
    is_broadcast = len(recipients) > 1
    broadcast_id = f"bc-{ctx.wakeup_id}-{tool_use_id}" if is_broadcast else None
    recipients_all = list(recipients) if is_broadcast else None

    # Merge user metadata (if any) with forward marker.
    user_meta = args.get("metadata") or {}
    if not isinstance(user_meta, dict):
        raise ToolError("metadata must be an object")
    metadata: dict[str, Any] | None = None
    if forward_msg_id is not None or user_meta:
        metadata = {**user_meta}
        if forward_msg_id is not None:
            metadata["forwarded_from_msg_id"] = forward_msg_id

    rows: list[OutboxRow] = []
    external_ids: list[str] = []
    for r in recipients:
        # Per-recipient external_id so outbox idempotency is per delivery
        # (one rate-limited retry doesn't double-deliver to the others).
        ext = f"{ctx.wakeup_id}:{tool_use_id}:{r}"
        external_ids.append(ext)
        payload = {
            "recipient": r,
            "sender": ctx.self_mailbox,
            "urgency": urgency,
            "title": title,
            "body": body,
            "task_id": ctx.task_id,
            "external_id": ext,
            "parent_msg_id": reply_to,
            "broadcast_id": broadcast_id,
            "recipients_all": recipients_all,
            "metadata": metadata,
            "attachments": attachments,
        }
        rows.append(
            OutboxRow(
                task_id=ctx.task_id,
                wakeup_id=ctx.wakeup_id,
                kind="mailbox_send",
                payload=payload,
                external_id=ext,
            )
        )
    await ctx.repos.outbox.enqueue(rows)

    return {
        "status": "queued",
        "recipients": recipients,
        "broadcast_id": broadcast_id,
        "reply_to": reply_to,
        "forwarded_from_msg_id": forward_msg_id,
        "external_ids": external_ids,
    }


# ---------------------------------------------------------------------------
# Scheduling branch helpers
# ---------------------------------------------------------------------------

_SCHEDULING_KEYS = (
    "deliver_at", "deliver_in", "recur_every", "recur_cron", "recur_until",
)


def _has_scheduling_args(args: dict[str, Any]) -> bool:
    return any(args.get(k) is not None for k in _SCHEDULING_KEYS)


async def _schedule_future_mail(
    ctx: ToolContext,
    args: dict[str, Any],
    recipients: list[str],
    body: str,
    urgency: str,
    *,
    title: str | None,
    reply_to: int | None,
    forward_msg_id: int | None,
    user_meta: dict[str, Any],
) -> dict[str, Any]:
    """Persist one scheduled_mail row per recipient. Broadcast splits like
    the immediate path: one row per delivery so each can bounce/cancel
    independently and so per-recipient mailbox cursors stay consistent.

    All time inputs are validated (PastDeliveryError on S3 rejection,
    cron syntax, duration range). Errors propagate as ToolError so the
    agent loop hands them back to the model for a fix.
    """
    deliver_at = args.get("deliver_at")
    deliver_in = args.get("deliver_in")
    recur_every = args.get("recur_every")
    recur_cron = args.get("recur_cron")
    recur_until_raw = args.get("recur_until")

    if recur_every is not None and recur_cron is not None:
        raise ToolError("pass at most one of recur_every / recur_cron")

    # Validate the duration shape early — surfaces nice messages for typos.
    if recur_every is not None and not isinstance(recur_every, str):
        raise ToolError("recur_every must be a string like '1h' / '1w'")
    if recur_cron is not None:
        if not isinstance(recur_cron, str):
            raise ToolError("recur_cron must be a string")
        try:
            validate_cron(recur_cron)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    try:
        first_fire = resolve_first_fire(
            deliver_at=deliver_at,
            deliver_in=deliver_in,
            recur_cron=recur_cron,
            now=now_utc(),
        )
    except PastDeliveryError as exc:
        raise ToolError(str(exc)) from exc
    except ValueError as exc:
        raise ToolError(str(exc)) from exc

    recur_kind: str | None = None
    recur_value: str | None = None
    if recur_every is not None:
        # Validate by parsing (raises on bad shape / too short / too long).
        try:
            parse_duration(recur_every)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        recur_kind = "interval"
        recur_value = recur_every
    elif recur_cron is not None:
        recur_kind = "cron"
        recur_value = recur_cron

    # Default recur_until to first_fire + 1y; honor explicit value if given.
    recur_until = None
    if recur_kind is not None:
        if recur_until_raw is None:
            recur_until = default_recur_until(first_fire)
        else:
            try:
                recur_until = datetime.fromisoformat(
                    str(recur_until_raw).replace("Z", "+00:00")
                )
            except ValueError as exc:
                raise ToolError(
                    f"recur_until must be ISO 8601 (e.g. "
                    f"'2026-12-31T00:00:00Z'); got {recur_until_raw!r}"
                ) from exc
            if recur_until <= first_fire:
                raise ToolError(
                    f"recur_until ({recur_until.isoformat()}) must be "
                    f"strictly after first delivery ({first_fire.isoformat()})"
                )

    # Metadata: same shape as immediate path (forward marker if any).
    metadata: dict[str, Any] | None = None
    if forward_msg_id is not None or user_meta:
        metadata = {**(user_meta or {})}
        if forward_msg_id is not None:
            metadata["forwarded_from_msg_id"] = forward_msg_id

    is_broadcast = len(recipients) > 1
    broadcast_id = (
        f"sched-bc-{ctx.wakeup_id}-{args.get('_tool_use_id')}"
        if is_broadcast else None
    )
    if broadcast_id is not None:
        metadata = {**(metadata or {}), "broadcast_id": broadcast_id}

    created_ids: list[int] = []
    for r in recipients:
        spec = ScheduledMail(
            recipient=r,
            sender=ctx.self_mailbox,
            urgency=urgency,  # type: ignore[arg-type]
            title=title,
            body=body,
            task_id=ctx.task_id,
            parent_msg_id=reply_to,
            metadata=metadata,
            scheduled_for=first_fire,
            recur_kind=recur_kind,  # type: ignore[arg-type]
            recur_value=recur_value,
            recur_until=recur_until,
            created_by_agent=ctx.self_mailbox,
            created_by_task=ctx.task_id,
        )
        sid = await ctx.repos.scheduled_mail.create(spec)
        created_ids.append(sid)

    return {
        "status": "scheduled",
        "scheduled_ids": created_ids,
        "recipients": recipients,
        "scheduled_for": iso(first_fire),
        "recur_kind": recur_kind,
        "recur_value": recur_value,
        "recur_until": iso(recur_until) if recur_until else None,
        "broadcast_id": broadcast_id,
    }


async def _mailbox_get_message(
    ctx: ToolContext, args: dict[str, Any]
) -> dict[str, Any]:
    """Fetch ANY message by id — your own mailbox, someone else's, the
    original of a forwarded message, the parent of a reply. Lets agents
    walk thread context without owner mediation.
    """
    msg_id = args.get("msg_id")
    if not isinstance(msg_id, int):
        raise ToolError("msg_id must be an integer")
    msg = await ctx.repos.mailbox.get_message(msg_id)
    if msg is None:
        raise ToolError(f"message id={msg_id} not found")
    out: dict[str, Any] = {
        "id": msg.id,
        "recipient": msg.recipient,
        "sender": msg.sender,
        "urgency": msg.urgency,
        "body": msg.body,
        "task_id": msg.task_id,
        "parent_msg_id": msg.parent_msg_id,
        "broadcast_id": msg.broadcast_id,
        "recipients_all": msg.recipients_all,
        "metadata": msg.metadata,
    }
    # Attachments: bulk-resolve blob metadata so the caller (agent_loop's
    # tool-result hydrator) has filename + media_type alongside the
    # blob_ids. The bytes are NOT inlined here — the loop translates
    # these into LyreContentBlock(type="image", blob_id=..., ...) which
    # the adapter then resolves through BlobStore at send-time.
    if msg.attachments:
        blob_rows = await ctx.repos.blobs.list_ids(msg.attachments)
        out["attachments"] = [
            {
                "blob_id": b.id,
                "media_type": b.media_type,
                "filename": b.filename,
                "size_bytes": b.size_bytes,
            }
            for b in blob_rows
        ]
        # Magic key: extra content blocks the agent_loop will append
        # alongside the tool_result block on the user message it builds
        # for this turn. This is what makes the model actually SEE the
        # image rather than just read its metadata. Images and PDFs
        # become their own typed blocks; anything else (future binary
        # types) just stays in `attachments` as text/JSON.
        view_blocks: list[dict[str, Any]] = []
        for b in blob_rows:
            if b.media_type.startswith("image/"):
                view_blocks.append({
                    "type": "image",
                    "blob_id": b.id,
                    "media_type": b.media_type,
                    "filename": b.filename,
                })
            elif b.media_type == "application/pdf":
                view_blocks.append({
                    "type": "document",
                    "blob_id": b.id,
                    "media_type": b.media_type,
                    "filename": b.filename,
                })
        if view_blocks:
            out["_lyre_view_blocks"] = view_blocks
    return out


async def _mailbox_read(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Inbox / sent-folder check.

    Returns LISTING ONLY (title + size + meta), never full body — pulling
    body bytes through the prompt every wakeup would burn tokens for no
    reason. To read a specific mail's body call `mailbox_get_message`.

    Two boxes:
      - `box="inbox"` (default): mail addressed to you.
          - default: only UNREAD, urgency-desc then id-asc.
          - `include_read=True`: include already-read (archive view).
          - reading auto-marks the returned rows as read.
      - `box="sent"`: mail YOU sent, newest-first. `recipient` becomes a
          filter ("mail I sent to X"). No auto-mark — sent mail has no
          read state on your side. Use this to recall what you promised
          in a prior wakeup.

    Default recipient = your own agent id (for inbox view). Explicit
    recipient must be a known agent_id (or 'owner').
    """
    self_mailbox = ctx.self_mailbox
    box = args.get("box", "inbox")
    if box not in ("inbox", "sent"):
        raise ToolError(f"box must be 'inbox' or 'sent', got {box!r}")
    limit = int(args.get("limit", 50))
    if limit > 200:
        limit = 200

    if box == "sent":
        recipient_filter = args.get("recipient")
        if recipient_filter is not None:
            if not isinstance(recipient_filter, str):
                raise ToolError("recipient must be a string if provided")
            if recipient_filter != self_mailbox and recipient_filter != "owner":
                if not await ctx.repos.agents.exists(recipient_filter):
                    known = [a.id for a in await ctx.repos.agents.list_all()]
                    raise ToolError(
                        f"unknown recipient {recipient_filter!r}. Pass a "
                        f"known AGENT ID from: {known + ['owner']}, or "
                        f"omit `recipient` to see all your sent mail."
                    )
        msgs = await ctx.repos.mailbox.list_sent_by(
            self_mailbox, recipient=recipient_filter, limit=limit,
        )
        return {
            "box": "sent",
            "sender": self_mailbox,
            "recipient_filter": recipient_filter,
            "auto_marked_read": False,
            "messages": [
                {
                    "id": m.id,
                    "recipient": m.recipient,
                    "urgency": m.urgency,
                    "title": m.title,
                    "body_chars": len(m.body or ""),
                    "parent_msg_id": m.parent_msg_id,
                    "broadcast_id": m.broadcast_id,
                    "recipients_all": m.recipients_all,
                    "task_id": m.task_id,
                    "delivered_at": (
                        m.delivered_at.isoformat()
                        if isinstance(m.delivered_at, datetime)
                        else m.delivered_at
                    ),
                }
                for m in msgs
            ],
        }

    # box == "inbox"
    recipient = args.get("recipient") or self_mailbox
    include_read = bool(args.get("include_read", False))
    only_blockers = bool(args.get("only_blockers", False))

    if recipient != self_mailbox and recipient != "owner":
        if not await ctx.repos.agents.exists(recipient):
            known = [a.id for a in await ctx.repos.agents.list_all()]
            raise ToolError(
                f"unknown recipient {recipient!r}. Either omit `recipient` "
                f"(defaults to your own mailbox: {self_mailbox!r}), or pass "
                f"a known AGENT ID from: {known + ['owner']}. "
                f"Do NOT invent recipient names."
            )

    await ctx.repos.mailbox.ensure_mailbox(recipient)

    if include_read:
        msgs = await ctx.repos.mailbox.read_all_by_recipient(
            recipient, limit=limit,
        )
        # Archive view doesn't auto-mark. Leave read state untouched.
        auto_marked = False
    else:
        msgs = await ctx.repos.mailbox.read_unread(
            recipient,
            min_urgency="blocker" if only_blockers else None,
            limit=limit,
        )
        if msgs:
            await ctx.repos.mailbox.mark_messages_read(
                recipient, [m.id for m in msgs if m.id is not None]
            )
        auto_marked = bool(msgs)

    unread_remaining = await ctx.repos.mailbox.count_unread(recipient)

    return {
        "box": "inbox",
        "recipient": recipient,
        "auto_marked_read": auto_marked,
        "unread_remaining": unread_remaining,
        "messages": [
            {
                "id": m.id,
                "sender": m.sender,
                "urgency": m.urgency,
                "title": m.title,
                "body_chars": len(m.body or ""),
                "parent_msg_id": m.parent_msg_id,
                "broadcast_id": m.broadcast_id,
                "recipients_all": m.recipients_all,
                "task_id": m.task_id,
                "delivered_at": (
                    m.delivered_at.isoformat()
                    if isinstance(m.delivered_at, datetime)
                    else m.delivered_at
                ),
                "read_at": (
                    m.read_at.isoformat()
                    if isinstance(m.read_at, datetime)
                    else m.read_at
                ),
            }
            for m in msgs
        ],
    }


async def _mark_read(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Explicitly mark one or more messages as read without calling
    mailbox_read. Use this to dismiss FYI mail you don't want to reply to.

    Only operates on your own mailbox — you can't mark someone else's mail.
    """
    msg_id = args.get("msg_id")
    if isinstance(msg_id, int):
        ids = [msg_id]
    elif isinstance(msg_id, list) and all(isinstance(x, int) for x in msg_id):
        ids = list(msg_id)
    else:
        raise ToolError("msg_id must be an integer or list of integers")
    await ctx.repos.mailbox.mark_messages_read(ctx.self_mailbox, ids)
    return {"status": "ok", "recipient": ctx.self_mailbox, "msg_ids": ids}


_ALLOWED_REACTION_KINDS = ("ack",)


async def _mailbox_react(
    ctx: ToolContext, args: dict[str, Any]
) -> dict[str, Any]:
    """Leave a reaction on someone else's message.

    Reactions are deliberately NOT mail: no new mailbox row, no unread
    count change, no Phase 0 auto-wake. The original sender sees it
    next time they pull `mailbox_get_message(msg_id)` or open the
    dashboard mail-detail view.

    Use this — instead of `mailbox_send` — when the polite response is
    "saw it, no further action". Avoids the handshake-storm pattern
    (A → B 'closing' → A 'ok, closing too' → B 'understood, closing' → …).
    """
    msg_id = args.get("msg_id")
    if not isinstance(msg_id, int):
        raise ToolError("msg_id must be an integer")
    kind = args.get("kind", "ack")
    if kind not in _ALLOWED_REACTION_KINDS:
        raise ToolError(
            f"kind must be one of {list(_ALLOWED_REACTION_KINDS)}; got {kind!r}"
        )

    # Validate the target exists — surface typos / hallucinated ids the
    # same way mailbox_send does, instead of silently inserting an FK
    # error or a dangling reaction.
    target = await ctx.repos.mailbox.get_message(msg_id)
    if target is None:
        raise ToolError(f"no mail with id={msg_id}")

    inserted = await ctx.repos.mailbox.add_reaction(
        msg_id=msg_id, reactor=ctx.self_mailbox, kind=kind,
    )

    # Forward the reaction to any external channel the original
    # message was already published to (e.g. owner → Lark → ✓ emoji
    # on the owner's message). Looks at ``metadata.channels.<name>``
    # rather than the live ChannelRegistry: a publish that landed on
    # a channel deserves a reaction echo on that channel, even if the
    # channel later got disabled (the dispatch will retry once it's
    # back). Only the FIRST react triggers the enqueue — repeats
    # would just collide on the outbox UNIQUE.
    if inserted:
        channels_meta = (target.metadata or {}).get("channels") or {}
        if isinstance(channels_meta, dict):
            rows: list[OutboxRow] = []
            for channel_name, ch_entry in channels_meta.items():
                if not isinstance(ch_entry, dict):
                    continue
                ext_msg_id = ch_entry.get("message_id")
                if not isinstance(ext_msg_id, str):
                    continue
                rows.append(OutboxRow(
                    task_id=ctx.task_id,
                    wakeup_id=ctx.wakeup_id,
                    kind="channel_reaction_publish",
                    payload={
                        "channel": channel_name,
                        "external_message_id": ext_msg_id,
                        "kind": kind,
                    },
                    external_id=(
                        f"channel:{channel_name}:reaction:"
                        f"{msg_id}:{ctx.self_mailbox}:{kind}"
                    ),
                ))
            if rows:
                await ctx.repos.outbox.enqueue(rows)

    return {
        "status": "ok" if inserted else "already_reacted",
        "msg_id": msg_id,
        "kind": kind,
        "reactor": ctx.self_mailbox,
    }


MAILBOX_SEND = Tool(
    name="mailbox_send",
    description=(
        "Email another agent (or several) via the persistent mailbox.\n"
        "Modes:\n"
        "  immediate — default. Delivered as soon as the outbox dispatcher "
        "picks it up (sub-second typical).\n"
        "  scheduled — pass any of deliver_at / deliver_in / recur_every / "
        "recur_cron. The mail goes to scheduled_mail and the scheduler "
        "delivers it when due. Powers reminders, supervision, timeouts, "
        "and recurring jobs (cron-like).\n"
        "Supports broadcast (`to` as a list), reply (`reply_to`), and "
        "forward (`forward_msg_id`)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "to": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}, "minItems": 1},
                ],
                "description": (
                    "Recipient AGENT ID(s) (use list_agents() to enumerate). "
                    "Pass a list to broadcast."
                ),
            },
            "body": {"type": "string", "description": "Message body, plain text."},
            "title": {
                "type": "string",
                "description": (
                    "Subject line shown to readers in their inbox listing "
                    "(≤140 chars). Readers see ONLY the title in their "
                    "mailbox_read output — they must call "
                    "mailbox_get_message to read your body. Always provide "
                    "a clear title; if omitted, derived from body's first "
                    "line (often vague). Good: 'PR #123 ready: typo fix'. "
                    "Bad: 'update', 'FYI', empty."
                ),
            },
            "urgency": {
                "type": "string",
                "enum": ["blocker", "high", "normal", "low"],
                "default": "normal",
            },
            "reply_to": {
                "type": "integer",
                "description": "Msg id this is a reply to (sets parent_msg_id).",
            },
            "forward_msg_id": {
                "type": "integer",
                "description": (
                    "Msg id you are forwarding. Your body is your own "
                    "commentary; recipients use mailbox_get_message to read "
                    "the original."
                ),
            },
            "attachments": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of blob_id strings (sha256 hex) to "
                    "attach. You can ONLY reference blobs you've "
                    "already seen — i.e. the blob_id values listed in "
                    "attachments[] of a mail you read via "
                    "mailbox_get_message. The tool will reject unknown "
                    "ids; you cannot manufacture binary content from "
                    "this end. To share a new image with another "
                    "agent, ask the owner to upload it via the "
                    "dashboard /send page."
                ),
            },
            "metadata": {
                "type": "object",
                "description": "Optional structured metadata to attach.",
            },
            "deliver_at": {
                "type": "string",
                "description": (
                    "Absolute ISO 8601 UTC timestamp for first delivery, "
                    "e.g. '2026-06-01T09:00:00Z'. Must be strictly in the "
                    "future and within 1 year. Past timestamps will ERROR "
                    "(use deliver_in for relative scheduling)."
                ),
            },
            "deliver_in": {
                "type": "string",
                "description": (
                    "Relative duration shortcut for first delivery: "
                    "'<N>m' / '<N>h' / '<N>d' / '<N>w'. Minimum '1m'. "
                    "E.g. '2h' = two hours from now."
                ),
            },
            "recur_every": {
                "type": "string",
                "description": (
                    "Recurrence interval: '<N>m' / '<N>h' / '<N>d' / '<N>w'. "
                    "Next delivery = previous + interval. Minimum '1m'. "
                    "Mutually exclusive with recur_cron."
                ),
            },
            "recur_cron": {
                "type": "string",
                "description": (
                    "Recurrence cron (5-field POSIX), e.g. '0 9 * * 1-5' "
                    "(workday 9am). Mutually exclusive with recur_every. "
                    "If deliver_at/deliver_in omitted, first fire = next "
                    "cron match."
                ),
            },
            "recur_until": {
                "type": "string",
                "description": (
                    "Absolute ISO 8601 UTC. Recurrence stops after this. "
                    "Default: first_fire + 1 year."
                ),
            },
        },
        "required": ["to", "body"],
    },
    handler=_mailbox_send,
)

MAILBOX_GET_MESSAGE = Tool(
    name="mailbox_get_message",
    description=(
        "Fetch any single mailbox message by primary id (regardless of "
        "recipient). Use it to: (a) read the original of a message you "
        "received as a forward, (b) walk a thread up via parent_msg_id, "
        "(c) inspect what was actually sent to other recipients of a "
        "broadcast."
    ),
    input_schema={
        "type": "object",
        "properties": {"msg_id": {"type": "integer"}},
        "required": ["msg_id"],
    },
    handler=_mailbox_get_message,
)

MAILBOX_READ = Tool(
    name="mailbox_read",
    description=(
        "Check your inbox OR your sent folder. Returns LISTING ONLY "
        "(id + sender/recipient + urgency + title + body_chars) — NOT "
        "the full body. To read a specific message's body call "
        "`mailbox_get_message(msg_id=N)`.\n\n"
        "`box=\"inbox\"` (default): mail addressed to you, unread by "
        "default, urgency-sorted. **Calling this MARKS returned mail "
        "as read** — you won't see them again unless you pass "
        "`include_read=True`.\n\n"
        "`box=\"sent\"`: mail YOU sent, newest-first. No auto-mark. "
        "Use this to recall what you promised in a prior wakeup — "
        "Lyre wakeups are stateless, so calling `mailbox_read("
        "box=\"sent\")` is how you remember your own commitments. "
        "When `box=\"sent\"`, `recipient` becomes a filter (\"mail "
        "I sent to X\"); omit it to see everything you sent.\n\n"
        "For inbox: omit `recipient` for your own mailbox (the common "
        "case). To inspect another agent's inbox (rare), pass their id."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "box": {
                "type": "string",
                "enum": ["inbox", "sent"],
                "default": "inbox",
                "description": (
                    "'inbox' = mail addressed to you (default). "
                    "'sent' = mail you sent (use to recall commitments)."
                ),
            },
            "recipient": {
                "type": "string",
                "description": (
                    "Inbox mode: whose mailbox to read (default = yours). "
                    "Sent mode: filter to mail sent TO this agent id "
                    "(default = all your sent mail)."
                ),
            },
            "include_read": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Inbox mode only. True → also return already-read "
                    "mail (archive view, no auto-mark). Default False → "
                    "only unread."
                ),
            },
            "limit": {"type": "integer", "default": 50, "maximum": 200},
            "only_blockers": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Inbox mode only. Only return urgency=blocker. "
                    "Default False (all urgencies, sorted)."
                ),
            },
        },
    },
    handler=_mailbox_read,
)

MARK_READ = Tool(
    name="mark_read",
    description=(
        "Mark mail in YOUR mailbox as read without calling mailbox_read. "
        "Use to dismiss FYI mail you don't want to reply to. Pass one "
        "msg_id (int) or a list of msg_ids. Already-read rows are a "
        "no-op (idempotent)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "msg_id": {
                "oneOf": [
                    {"type": "integer"},
                    {"type": "array", "items": {"type": "integer"}, "minItems": 1},
                ],
            },
        },
        "required": ["msg_id"],
    },
    handler=_mark_read,
)


MAILBOX_REACT = Tool(
    name="mailbox_react",
    description=(
        "React to a message you received — a lightweight ack that the "
        "sender can see but does NOT wake them or take a mailbox slot. "
        "Use this for 'saw it, no further action' instead of replying "
        "with another mailbox_send, which would only invite another "
        "polite ack and start a handshake loop. Currently the only "
        "kind is 'ack'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "msg_id": {
                "type": "integer",
                "description": "The mailbox_messages.id you're reacting to.",
            },
            "kind": {
                "type": "string",
                "enum": list(_ALLOWED_REACTION_KINDS),
                "default": "ack",
                "description": (
                    "Reaction kind. Today only 'ack' (= 'I saw your "
                    "message, no reply needed'). The vocabulary stays "
                    "narrow on purpose — pick a richer signal only when "
                    "ack is genuinely the wrong word."
                ),
            },
        },
        "required": ["msg_id"],
    },
    handler=_mailbox_react,
)


# ---------------------------------------------------------------------------
# Scheduled mail management
# ---------------------------------------------------------------------------


async def _list_scheduled_mail(
    ctx: ToolContext, args: dict[str, Any]
) -> dict[str, Any]:
    recipient = args.get("recipient")
    sender = args.get("sender")
    status = args.get("status", "pending")
    limit = int(args.get("limit", 50))
    if limit > 200:
        limit = 200
    if status not in ("pending", "completed", "cancelled", "bounced", "all"):
        raise ToolError(
            "status must be one of pending/completed/cancelled/bounced/all"
        )
    rows = await ctx.repos.scheduled_mail.list_filtered(
        recipient=recipient if isinstance(recipient, str) else None,
        sender=sender if isinstance(sender, str) else None,
        status=status,
        limit=limit,
    )
    return {
        "scheduled_mails": [
            {
                "id": r.id,
                "recipient": r.recipient,
                "sender": r.sender,
                "urgency": r.urgency,
                "body_preview": (r.body or "")[:200],
                "scheduled_for": (
                    r.scheduled_for.isoformat()
                    if hasattr(r.scheduled_for, "isoformat")
                    else r.scheduled_for
                ),
                "recur_kind": r.recur_kind,
                "recur_value": r.recur_value,
                "occurrence_count": r.occurrence_count,
                "status": r.status,
            }
            for r in rows
        ],
        "count": len(rows),
        "filters": {
            "recipient": recipient,
            "sender": sender,
            "status": status,
        },
    }


LIST_SCHEDULED_MAIL = Tool(
    name="list_scheduled_mail",
    description=(
        "List scheduled (future) mail entries. Default: pending mail only. "
        "Filter by `recipient` (agent id), `sender` (agent id), `status` "
        "(pending/completed/cancelled/bounced/all). Use this to see what's "
        "scheduled to fire later before creating duplicates."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "recipient": {"type": "string"},
            "sender": {"type": "string"},
            "status": {
                "type": "string",
                "enum": [
                    "pending", "completed", "cancelled", "bounced", "all"
                ],
                "default": "pending",
            },
            "limit": {"type": "integer", "default": 50, "maximum": 200},
        },
    },
    handler=_list_scheduled_mail,
)


async def _cancel_scheduled_mail(
    ctx: ToolContext, args: dict[str, Any]
) -> dict[str, Any]:
    raw_id = args.get("id")
    if not isinstance(raw_id, int):
        raise ToolError("id (integer) required")
    reason = args.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ToolError("reason must be a string if provided")

    existing = await ctx.repos.scheduled_mail.get(raw_id)
    if existing is None:
        raise ToolError(f"scheduled_mail id={raw_id} not found")
    if existing.status != "pending":
        raise ToolError(
            f"scheduled_mail id={raw_id} is already {existing.status}; "
            f"cannot cancel"
        )

    ok = await ctx.repos.scheduled_mail.mark_cancelled(
        mail_id=raw_id,
        cancelled_by=ctx.self_mailbox,
        reason=reason,
    )
    return {
        "id": raw_id,
        "cancelled": bool(ok),
        "note": (
            "Future occurrences are stopped. Already-delivered messages "
            "from this schedule stay in mailbox history (we don't recall)."
        ),
    }


CANCEL_SCHEDULED_MAIL = Tool(
    name="cancel_scheduled_mail",
    description=(
        "Cancel a pending scheduled mail. For recurring schedules this "
        "stops ALL future occurrences (past deliveries remain in mailbox "
        "history)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "id": {
                "type": "integer",
                "description": "scheduled_mail id (from list_scheduled_mail).",
            },
            "reason": {
                "type": "string",
                "description": "Optional reason; stored in metadata for audit.",
            },
        },
        "required": ["id"],
    },
    handler=_cancel_scheduled_mail,
)
