"""LarkChannel — :class:`ExternalChannel` impl for Lark/Feishu bot.

Two halves running in one ``run(stop_event)``:

  * **Inbound** — a Lark WebSocket Long Connection (provided by
    ``lark_oapi.ws.Client``) pushes events synchronously from a
    worker thread. We bridge each event into the main asyncio loop
    via :func:`asyncio.run_coroutine_threadsafe`, where it lands
    in mail.insert_message with sender="owner" and a recipient
    chosen by :mod:`addressing`.

  * **Outbound** — :meth:`publish_owner_mail` runs in the main
    asyncio loop, calls ``message.acreate`` (async SDK variant) to
    post to Lark. Image attachments are uploaded first via
    ``image.acreate`` to get a stable ``image_key``, then sent as a
    second image-message in the same thread.

The WS client's ``start()`` is sync-blocking and the SDK exposes no
graceful stop API. We run it in a **daemon thread** so it dies with
the process on shutdown — lyre serve already SIGTERMs cleanly, the
thread doesn't block exit, and lost in-flight WS state is fine
(events are pushed, not pulled, so missed events come back on
reconnect after restart).
"""

from __future__ import annotations

import asyncio
import io
import json as _json
import re
import threading
import uuid
from typing import TYPE_CHECKING, Any, ClassVar

import structlog

# Recomputing the same auto-derive used at insert time lets us tell
# "owner specified a real subject" apart from "title fell back to the
# body's first line" when rendering Lark cards — keep them in sync.
from ...persistence.sqlite_impl import _derive_title_from_body

if TYPE_CHECKING:
    from ...config import LarkConfig
    from ...persistence.models import MailboxMessage
    from ...persistence.repositories import Repositories
    from ...runtime.blob_store import BlobStore

log = structlog.get_logger()

# Image MIME types we accept on the inbound side. Anything else
# coming through Lark gets logged + dropped — we don't want to
# pollute the blob store with arbitrary file types until the
# multimodal contract is extended to documents/other binaries.
_INBOUND_IMAGE_MIME_BY_EXT: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "heic": "image/heic",
    "heif": "image/heif",
}


class LarkChannel:
    """Lark/Feishu owner-mailbox surface."""

    name: ClassVar[str] = "lark"

    def __init__(
        self,
        cfg: LarkConfig,
        repos: Repositories,
        blob_store: BlobStore | None,
        *,
        dispatcher_id: str,
    ) -> None:
        """``dispatcher_id`` is the default recipient for unaddressed
        Lark messages — resolved from the dispatcher persona's
        display_name at lyre-serve startup and passed in so the
        WS-callback path doesn't need to re-query the DB on each
        event."""
        if not cfg.app_id or not cfg.app_secret:
            raise ValueError(
                "LarkChannel requires LARK_APP_ID + LARK_APP_SECRET "
                "in env. config.toml only carries the non-sensitive "
                "fields (enabled, authorized_user_id)."
            )
        if not cfg.authorized_user_id:
            raise ValueError(
                "LarkChannel requires authorized_user_id in "
                "[integrations.lark] — the Lark user_id whose "
                "messages are treated as the owner's. Without this "
                "guard, anyone in the same tenant could inject tasks."
            )
        self.cfg = cfg
        self.repos = repos
        self.blob_store = blob_store
        self.dispatcher_id = dispatcher_id
        # Late-imported so the import-time cost of lark-oapi only
        # hits processes that actually enable the integration.
        import lark_oapi  # type: ignore[import-untyped]

        self._lark = lark_oapi
        # Async-capable client for outbound API calls. Built lazily
        # in __init__ rather than per-call so token refresh state
        # accumulates correctly inside the SDK.
        self._api_client = (
            lark_oapi.Client.builder()
            .app_id(cfg.app_id)
            .app_secret(cfg.app_secret)
            .build()
        )
        # Captured at run() time so the sync inbound callback (called
        # from the WS worker thread) can schedule coroutines back
        # onto the right asyncio loop.
        self._main_loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------
    # ExternalChannel Protocol — run loop
    # ------------------------------------------------------------------

    async def run(self, stop_event: asyncio.Event) -> None:
        """Open the WebSocket Long Connection, wait for stop signal.

        The WS client runs in a daemon thread (lark-oapi 1.6.5 has
        no stop API on ``ws.Client``); when ``lyre serve`` exits,
        the daemon thread dies with it. Events still arriving
        mid-shutdown are lost; on next boot Lark redelivers any
        missed messages via its own event-subscription buffer.
        """
        self._main_loop = asyncio.get_running_loop()

        # Empty encrypt_key + verification_token — the Long Connection
        # transport handles auth at the WS handshake level, so the
        # event handler's HTTP-style signature verification isn't used.
        handler = (
            self._lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_lark_message)
            # Read receipts: Lark pushes ``im.message.message_read_v1``
            # whenever the user reads any message in the bot's thread.
            # We don't surface that signal yet, but if no handler is
            # registered the SDK floods stderr with "processor not
            # found" errors on every read event. A no-op handler
            # absorbs the events cleanly.
            .register_p2_im_message_message_read_v1(self._on_lark_read)
            .build()
        )
        def _run_ws() -> None:
            # ──── SDK BUG WORKAROUND ────────────────────────────────
            # lark_oapi has no async-native start API and binds its WS
            # event loop at module import time, making the SDK
            # incompatible with apps that already run an asyncio loop
            # (uvicorn / FastAPI / our scheduler).
            #
            #   - ``lark_oapi.ws.client.loop`` is a MODULE-LEVEL global
            #     captured at first import — which happens on the main
            #     thread when ``LarkChannel.__init__`` imports the SDK.
            #     ``Client.start()``, ``_connect()``, ``_disconnect()``,
            #     ``_ping_loop`` all use that one shared global.
            #   - ``ExpiringCache.__init__`` (created inside
            #     ``Client.__init__``) also calls
            #     ``asyncio.get_event_loop()`` and schedules a sweeper
            #     task on whatever loop that returns.
            #
            # Upstream tracking:
            #   larksuite/oapi-sdk-python#119 — module-level loop bug
            #     (reporter uses the same rebind workaround we do here)
            #   larksuite/oapi-sdk-python#96  — request for async start
            #   larksuite/oapi-sdk-python#128 — graceful stop PR
            #
            # When any of those land we can drop this block and call
            # the proper API instead. Until then: this worker thread
            # owns a fresh loop, the SDK's module global is rebound to
            # it BEFORE Client construction so ExpiringCache picks it
            # up too, and only then does the Client get built. This
            # also eliminates the "Task was destroyed but it is
            # pending" warning chain from the sweeper task being
            # scheduled on the wrong loop.
            try:
                new_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(new_loop)
                from lark_oapi.ws import client as _ws_mod  # type: ignore[import-untyped]
                _ws_mod.loop = new_loop

                ws = self._lark.ws.Client(
                    self.cfg.app_id,
                    self.cfg.app_secret,
                    event_handler=handler,
                )
                ws.start()
            except Exception as exc:  # noqa: BLE001
                # ws.start() normally blocks for the channel's lifetime.
                # If it raises (construction error, or the SDK exhausts
                # its internal reconnects on auth/network failure), this
                # daemon thread dies and the channel goes silently deaf —
                # run() keeps awaiting stop_event forever. Surface the
                # death as a structured log instead of a bare stderr
                # traceback so it's observable. No reconnect/restart here
                # (that would change behavior); recovery is a serve
                # restart, per the channel's documented daemon-thread model.
                log.exception("lark_ws_thread_died", error=str(exc))

        thread = threading.Thread(
            target=_run_ws, name="lark-ws", daemon=True,
        )
        thread.start()
        log.info(
            "lark_channel_started",
            authorized_user_id=self.cfg.authorized_user_id,
            default_recipient=self.dispatcher_id,
        )
        try:
            await stop_event.wait()
        finally:
            log.info("lark_channel_stopping")
            # daemon thread dies with process; nothing to await.

    # ------------------------------------------------------------------
    # Inbound — Lark → mail
    # ------------------------------------------------------------------

    def _on_lark_read(self, data: Any) -> None:
        """No-op handler for ``im.message.message_read_v1`` events.

        Registered purely to keep the SDK from spamming stderr with
        "processor not found" errors on every owner-side read receipt.
        We don't surface read state yet; if we ever do, swap this for
        a real handler.
        """
        return

    def _on_lark_message(self, data: Any) -> None:
        """Synchronous callback invoked by lark-oapi from the WS
        worker thread. Schedule the actual handling on the main
        asyncio loop so all DB / blob_store I/O happens in one
        coherent context."""
        if self._main_loop is None:
            log.warning("lark_event_before_run", reason="no main loop")
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._handle_inbound(data), self._main_loop,
            )
        except Exception as exc:  # noqa: BLE001
            # Don't let an exception in the sync callback kill the
            # WS thread — it'd take all future events down with it.
            log.exception("lark_event_dispatch_failed", error=str(exc))

    async def _handle_inbound(self, event: Any) -> None:
        """Resolve a Lark event to a mail insertion. Idempotent on
        ``mailbox_messages.external_id = "lark:<message_id>"``: the
        WS connection occasionally redelivers events on reconnect,
        and we don't want double-mail."""
        try:
            payload = event.event
            sender = payload.sender
            # ``sender_id`` carries open_id / user_id / union_id; we
            # match on ``open_id`` because that's the app-scoped form
            # the bot can use for outbound sends WITHOUT requesting the
            # contact:user.employee_id:readonly scope (which user_id
            # would need — Lark equates user_id with employee_id).
            sender_open_id = (
                sender.sender_id.open_id if sender else None
            )
            if sender_open_id != self.cfg.authorized_user_id:
                # INFO (not debug) — during first-time config the owner
                # needs to see their app-scoped open_id to put it in
                # config.toml. Lark's open_id is per-app: an id obtained
                # from one bot won't match here. Send a message to this
                # bot, copy the ``got=ou_…`` value from this line.
                log.info(
                    "lark_event_unauthorized_sender",
                    got=sender_open_id,
                    expected=self.cfg.authorized_user_id,
                    hint=(
                        "if this is YOUR open_id for this app, set "
                        "[integrations.lark].authorized_user_id to it"
                    ),
                )
                return

            message = payload.message
            msg_id = message.message_id
            msg_type = message.message_type
            chat_type = message.chat_type  # "p2p" / "group"

            # Bot ignores group chats by design — owner is by
            # definition a single user; group traffic would be
            # ambiguous and noisy.
            if chat_type != "p2p":
                log.debug(
                    "lark_event_non_p2p_skipped",
                    chat_type=chat_type,
                )
                return

            # Resolve thread context: if this is a reply to a Lark
            # message we previously published, look up the original
            # mail and inherit its recipient. The parent's
            # metadata.channels.lark.message_id is the join key.
            thread_recipient: str | None = None
            parent_mail_id: int | None = None
            root_id = message.root_id or None
            if root_id:
                parent = await self._lookup_mail_by_lark_id(root_id)
                if parent is not None:
                    parent_mail_id = parent.id
                    # If parent was an outbound mail (Lyre → Lark),
                    # the original recipient is the agent that sent
                    # to the owner — we want the reply to go back to
                    # them. If parent was inbound (owner → agent),
                    # continue to the same recipient.
                    if parent.recipient == "owner":
                        thread_recipient = parent.sender
                    else:
                        thread_recipient = parent.recipient

            # Body extraction varies by msg_type. Lark always
            # encodes content as a JSON string.
            body_text, image_keys = _extract_body_and_images(
                msg_type, message.content,
            )

            from .addressing import resolve

            addr = resolve(
                body_text,
                default_recipient=self.dispatcher_id,
                thread_recipient=thread_recipient,
            )

            # Validate recipient exists. Unknown agent → drop the
            # mail with a log (silent UX is bad but auto-creating
            # arbitrary ids from chat is worse).
            if not await self.repos.agents.exists(addr.recipient):
                log.warning(
                    "lark_inbound_unknown_recipient",
                    recipient=addr.recipient,
                    source=addr.source,
                )
                return

            # Idempotency short-circuit: the WS connection redelivers
            # events on reconnect (see docstring). insert_message's
            # ON CONFLICT is still the authority, but on a redelivery
            # we can skip the wasted image fetch + blob upsert by
            # checking the same (recipient, external_id) key first.
            external_id = f"lark:{msg_id}"
            if (
                await self.repos.mailbox.find_id_by_external_id(
                    addr.recipient, external_id,
                )
                is not None
            ):
                log.debug(
                    "lark_inbound_duplicate_skipped",
                    msg_id=msg_id, recipient=addr.recipient,
                )
                return

            # Download any image attachments.
            attachments = await self._download_images(
                msg_id, image_keys,
            )

            # Owner can override urgency with a leading token
            # (!blocker / !urgent / !high / !low). Token is stripped
            # from the stored body so agents don't see the meta-marker
            # in the message they read.
            urgency, stored_body = _parse_urgency_prefix(addr.body)

            await self.repos.mailbox.ensure_mailbox(addr.recipient)
            from ...persistence.models import MailboxMessage
            inserted_id = await self.repos.mailbox.insert_message(
                MailboxMessage(
                    recipient=addr.recipient,
                    external_id=external_id,
                    sender="owner",
                    urgency=urgency,  # type: ignore[arg-type]
                    body=stored_body,
                    parent_msg_id=parent_mail_id,
                    attachments=attachments or None,
                    metadata={
                        "channels": {
                            "lark": {
                                "message_id": msg_id,
                                "open_id": sender_open_id,
                                # `root_id` for thread, falls back
                                # to the message id itself so a
                                # first-message-in-thread still has
                                # a stable thread key.
                                "thread_id": root_id or msg_id,
                            },
                        },
                    },
                )
            )
            log.info(
                "lark_inbound_delivered",
                msg_id=msg_id, mail_id=inserted_id,
                recipient=addr.recipient,
                source=addr.source,
                attachments=len(attachments),
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("lark_inbound_failed", error=str(exc))

    async def _lookup_mail_by_lark_id(
        self, lark_message_id: str,
    ) -> MailboxMessage | None:
        """Find a mail whose metadata records this Lark message id.
        Used to resolve thread-reply recipients."""
        return await self.repos.mailbox.find_by_channel_external_id(
            "lark", lark_message_id,
        )

    async def _download_images(
        self, lark_message_id: str, image_keys: list[str],
    ) -> list[str]:
        """Download images from Lark, write to BlobStore, upsert
        metadata, return blob_ids in the order they were attached."""
        if not image_keys:
            return []
        if self.blob_store is None:
            log.warning(
                "lark_inbound_images_skipped_no_blob_store",
                count=len(image_keys),
            )
            return []
        blob_ids: list[str] = []
        from lark_oapi.api.im.v1 import (  # type: ignore[import-untyped]
            GetMessageResourceRequest,
        )

        for key in image_keys:
            req = (
                GetMessageResourceRequest.builder()
                .message_id(lark_message_id)
                .file_key(key)
                .type("image")
                .build()
            )
            resp = await self._api_client.im.v1.message_resource.aget(req)
            if not resp.success():
                log.warning(
                    "lark_image_download_failed",
                    file_key=key, code=resp.code, msg=resp.msg,
                )
                continue
            # ``resp.file`` is typed loosely by the SDK; at runtime it's
            # either a BytesIO-like buffer (download stream) or the raw
            # bytes. ``getvalue`` exists on BytesIO but not the abstract
            # ``IOBase`` mypy sees, so the cast is needed only for the
            # type-checker.
            file_obj = resp.file
            if isinstance(file_obj, io.IOBase) and hasattr(file_obj, "getvalue"):
                data = file_obj.getvalue()
            else:
                data = file_obj
            # Lark doesn't surface a MIME type on download. Sniff
            # from the magic bytes — png/jpg/gif/webp cover the
            # vast majority of screenshots and phone-camera images.
            media_type = _sniff_image_mime(data) or "image/png"
            blob_id = await asyncio.to_thread(self.blob_store.write, data, media_type)
            from ...persistence.models import Blob
            await self.repos.blobs.upsert(Blob(
                id=blob_id,
                media_type=media_type,
                size_bytes=len(data),
                filename=None,  # Lark doesn't supply one
                source="owner",  # came in from the owner via Lark
            ))
            blob_ids.append(blob_id)
        return blob_ids

    # ------------------------------------------------------------------
    # Outbound — mail → Lark (called by outbox dispatcher)
    # ------------------------------------------------------------------

    async def publish_owner_mail(
        self,
        msg: MailboxMessage,
        reply_to_external_id: str | None,
    ) -> str | None:
        """Post an owner-bound mail (and any image attachments) to
        Lark. Returns the new Lark message id (text post) so the
        outbox dispatcher records it on metadata; the image posts
        (one per attachment) are not threaded back to Lyre — they
        ride alongside the text post in the same Lark thread.

        ``reply_to_external_id``: when set (it's the Lark
        ``message_id`` of the parent mail's published post), we use
        the ``/messages/:id/reply`` endpoint so the reply nests
        under the parent in the owner's Lark client. Without this,
        every agent reply showed up as a fresh top-level message
        even though Lyre's own DB threaded them correctly via
        ``parent_msg_id``."""
        from lark_oapi.api.im.v1 import (
            CreateImageRequest,
            CreateImageRequestBody,
            CreateMessageRequest,
            CreateMessageRequestBody,
            ReplyMessageRequest,
            ReplyMessageRequestBody,
        )

        # Build an interactive card so Lark renders the body's
        # markdown (lists, code blocks, links, bold). Plain text posts
        # showed everything as raw asterisks and backticks. Header
        # carries the sender id + urgency-based colour.
        card_content = _json.dumps(_build_owner_mail_card(
            sender=msg.sender,
            body=msg.body or "",
            urgency=msg.urgency,
            title=msg.title,
        ))
        text_uuid = f"lyre-mail-{msg.id}"  # SDK-side dedup token

        if reply_to_external_id is not None:
            reply_req = (
                ReplyMessageRequest.builder()
                .message_id(reply_to_external_id)
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .msg_type("interactive")
                    .content(card_content)
                    .uuid(text_uuid)
                    .build()
                )
                .build()
            )
            text_resp = await self._api_client.im.v1.message.areply(reply_req)
        else:
            text_req = (
                CreateMessageRequest.builder()
                .receive_id_type("open_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(self.cfg.authorized_user_id)
                    .msg_type("interactive")
                    .content(card_content)
                    .uuid(text_uuid)
                    .build()
                )
                .build()
            )
            text_resp = await self._api_client.im.v1.message.acreate(text_req)
        if not text_resp.success():
            raise RuntimeError(
                f"Lark card post failed: code={text_resp.code} "
                f"msg={text_resp.msg}"
            )
        lark_msg_id = (
            text_resp.data.message_id if text_resp.data else None
        )

        # Image attachments → upload each then send as image messages.
        # We don't fail the whole publish if one image fails — the
        # text already landed; log + continue is the right tradeoff.
        if msg.attachments and self.blob_store is not None:
            for blob_id in msg.attachments:
                blob = await self.repos.blobs.get(blob_id)
                if blob is None:
                    log.warning(
                        "lark_outbound_missing_blob", blob_id=blob_id,
                    )
                    continue
                try:
                    raw = await asyncio.to_thread(self.blob_store.read, blob.id, blob.media_type)
                except FileNotFoundError:
                    log.warning(
                        "lark_outbound_blob_file_missing",
                        blob_id=blob_id,
                    )
                    continue
                up_req = (
                    CreateImageRequest.builder()
                    .request_body(
                        CreateImageRequestBody.builder()
                        .image_type("message")
                        .image(io.BytesIO(raw))
                        .build()
                    )
                    .build()
                )
                up_resp = await self._api_client.im.v1.image.acreate(up_req)
                if not up_resp.success() or not up_resp.data:
                    log.warning(
                        "lark_image_upload_failed",
                        blob_id=blob_id,
                        code=up_resp.code, msg=up_resp.msg,
                    )
                    continue
                img_content = _json.dumps(
                    {"image_key": up_resp.data.image_key},
                )
                img_uuid = f"lyre-mail-{msg.id}-img-{blob_id[:8]}"
                if reply_to_external_id is not None:
                    img_req = (
                        ReplyMessageRequest.builder()
                        .message_id(reply_to_external_id)
                        .request_body(
                            ReplyMessageRequestBody.builder()
                            .msg_type("image")
                            .content(img_content)
                            .uuid(img_uuid)
                            .build()
                        )
                        .build()
                    )
                    img_resp = await self._api_client.im.v1.message.areply(img_req)
                else:
                    img_create = (
                        CreateMessageRequest.builder()
                        .receive_id_type("open_id")
                        .request_body(
                            CreateMessageRequestBody.builder()
                            .receive_id(self.cfg.authorized_user_id)
                            .msg_type("image")
                            .content(img_content)
                            .uuid(img_uuid)
                            .build()
                        )
                        .build()
                    )
                    img_resp = await self._api_client.im.v1.message.acreate(img_create)
                if not img_resp.success():
                    log.warning(
                        "lark_image_post_failed",
                        blob_id=blob_id,
                        code=img_resp.code, msg=img_resp.msg,
                    )

        return lark_msg_id

    async def publish_reaction(
        self,
        external_message_id: str,
        kind: str,
    ) -> None:
        """Add a reaction emoji to a previously-published Lark message.

        Used by ``mailbox_react(kind="ack")`` so an offline owner sees
        a ✓ on their original message in Lark without us pushing a new
        push notification. The map below decides which Lyre reaction
        kind shows as which Lark emoji.
        """
        emoji_type = _REACTION_TO_LARK_EMOJI.get(kind)
        if emoji_type is None:
            log.warning(
                "lark_reaction_unmapped_kind",
                kind=kind,
                external_message_id=external_message_id,
            )
            return

        from lark_oapi.api.im.v1 import (
            CreateMessageReactionRequest,
            CreateMessageReactionRequestBody,
            Emoji,
        )

        req = (
            CreateMessageReactionRequest.builder()
            .message_id(external_message_id)
            .request_body(
                CreateMessageReactionRequestBody.builder()
                .reaction_type(
                    Emoji.builder().emoji_type(emoji_type).build()
                )
                .build()
            )
            .build()
        )
        resp = await self._api_client.im.v1.message_reaction.acreate(req)
        if not resp.success():
            # Idempotency: Lark returns an error if the same emoji
            # already exists on the message from the same actor. We
            # don't have a stable error code documented for that case
            # so we just log and swallow — the outbox row was already
            # dispatched, repeating won't help.
            log.warning(
                "lark_reaction_post_failed",
                external_message_id=external_message_id,
                kind=kind,
                emoji_type=emoji_type,
                code=resp.code, msg=resp.msg,
            )


# Mapping: Lyre reaction kinds → Lark emoji_type. Keep small; this is
# the per-channel render surface, the cross-channel kinds set lives
# on ``MailReaction.kind``.
_REACTION_TO_LARK_EMOJI: dict[str, str] = {
    "ack": "OK",
}


# ---------------------------------------------------------------------------
# Helpers — kept module-level so they're easy to test without spinning up
# a full LarkChannel.
# ---------------------------------------------------------------------------


# Inbound urgency-prefix parsing for owner messages from Lark.
# Owner can lead a chat message with `!blocker` / `!urgent` / `!high` /
# `!low` to override the default normal. `!urgent` aliases to `high`
# (matches how people naturally type "urgent" instead of "high"). The
# token must be at the very start and bounded by whitespace or end of
# string (so `!blockerfoo` doesn't match `!blocker`). Case-insensitive.
# If no recognized token, urgency stays normal and body passes through
# unchanged.
_URGENCY_TOKEN_TO_VALUE: dict[str, str] = {
    "blocker": "blocker",
    "urgent":  "high",
    "high":    "high",
    "low":     "low",
}
_URGENCY_PREFIX_RE = re.compile(
    r"^!(?P<token>blocker|urgent|high|low)\b\s*",
    re.IGNORECASE,
)


def _parse_urgency_prefix(body: str) -> tuple[str, str]:
    """Strip a leading ``!blocker`` / ``!urgent`` / ``!high`` / ``!low``
    prefix from ``body`` and map it to a mailbox urgency level.

    Returns ``(urgency, stripped_body)``. If no recognized prefix is
    present, returns ``("normal", body)`` — the channel's default.

    The prefix is only honoured at the very start of the message, and
    must be followed by whitespace or end-of-string (the ``\\b`` in
    the regex). That keeps phrases like ``!important`` or ``!low-key``
    or ``!blockedness`` from getting mis-parsed.
    """
    if not body:
        return "normal", body
    m = _URGENCY_PREFIX_RE.match(body)
    if m is None:
        return "normal", body
    urgency = _URGENCY_TOKEN_TO_VALUE[m.group("token").lower()]
    return urgency, body[m.end():]


# Urgency → Lark card header color template. Lark's built-in palette
# (see open.feishu.cn card docs) — these are the values that
# actually render colored bars in the owner's Lark client.
_URGENCY_TEMPLATE: dict[str, str] = {
    "blocker": "red",
    "high":    "orange",
    "normal":  "blue",
    "low":     "grey",
}

# Traffic-light marker prepended to the body's ``**from <sender>**``
# attribution line. Only the elevated urgencies get a marker — flagging
# every normal mail with a coloured dot would be visual noise. blocker
# / high mail tends to be terse, often arrives without a meaningful
# title (so no coloured header bar), and the body-level dot is the
# only urgency signal owner sees in those cases.
_URGENCY_BODY_MARKER: dict[str, str] = {
    "blocker": "🔴",
    "high":    "🟠",
}


def _build_owner_mail_card(
    sender: str, body: str, urgency: str, title: str | None = None,
) -> dict[str, Any]:
    """Lark interactive card with markdown body.

    Plain ``msg_type=text`` posts don't render markdown — the owner saw
    everything (lists, code blocks, links) as raw asterisks and
    backticks. Cards via ``lark_md`` text components render properly
    and let us colour-code by urgency too.

    Layout: an attribution line — ``[<dot>] **from <sender>**`` where
    the dot is 🔴/🟠 for blocker/high urgency and omitted otherwise —
    is always the first line of the body, then a blank line, then the
    message body verbatim. Uniform sender attribution, owner doesn't
    have to scan two places to know who's talking.

    The ``header`` block is only attached when the sender supplied a
    *meaningful* title — i.e. one distinct from what we'd auto-derive
    from the body's first line. An auto-derived title would just
    duplicate body[0] in the header, so we drop the header entirely
    for those — cleaner than the previous ``[<sender>]`` placeholder.
    When the header is present, ``template`` colours it by urgency
    (blocker→red etc); when absent, the body-level dot is the only
    urgency signal the owner gets.

    Image attachments still ride as separate ``msg_type=image`` posts
    threaded to the same reply parent — embedding them inside the card
    would require extra ``img_key`` round-trips for no UX gain over the
    existing thread layout.
    """
    template = _URGENCY_TEMPLATE.get(urgency, "blue")
    marker = _URGENCY_BODY_MARKER.get(urgency, "")

    # Auto-derive check: the persistence layer fills missing titles with
    # the body's first non-empty line (sqlite_impl._derive_title_from_body).
    # Recomputing here lets the card tell "owner cares about this subject"
    # apart from "no subject given" without threading an extra flag through.
    has_meaningful_title = (
        title is not None and title != _derive_title_from_body(body)
    )

    attribution = f"{marker} **from {sender}**" if marker else f"**from {sender}**"
    body_content = f"{attribution}\n\n{body}" if body else attribution

    card: dict[str, Any] = {
        "config": {"wide_screen_mode": True, "update_multi": False},
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": body_content,
                },
            },
        ],
    }
    if has_meaningful_title:
        card["header"] = {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
        }
    return card


def _extract_body_and_images(
    msg_type: str, content_json: str | None,
) -> tuple[str, list[str]]:
    """Parse Lark's ``message.content`` JSON string by msg_type.

    Returns ``(body_text, image_keys)``:
      * msg_type="text"  → (text content, [])
      * msg_type="image" → ("", [image_key])
      * msg_type="post"  → (concatenated text from rich blocks, image_keys)
      * anything else    → ("", []) plus a debug log upstream

    Stays pure so we can test the content shapes without an SDK
    instance.
    """
    if not content_json:
        return "", []
    try:
        content = _json.loads(content_json)
    except (ValueError, TypeError):
        return "", []

    if msg_type == "text":
        return str(content.get("text", "")), []
    if msg_type == "image":
        key = content.get("image_key")
        return "", [str(key)] if isinstance(key, str) else []
    if msg_type == "post":
        # Rich post: content.content is a list[list[block]]. Each
        # block has type+text or type+image_key. Concatenate text
        # parts and collect image keys.
        text_parts: list[str] = []
        image_keys: list[str] = []
        for paragraph in content.get("content", []) or []:
            for block in paragraph or []:
                t = block.get("tag")
                if t == "text":
                    text_parts.append(str(block.get("text", "")))
                elif t == "img":
                    k = block.get("image_key")
                    if isinstance(k, str):
                        image_keys.append(k)
        return "\n".join(text_parts), image_keys
    return "", []


def _sniff_image_mime(data: bytes) -> str | None:
    """Identify image format from the first few bytes. Avoids
    trusting any header Lark may not send."""
    if len(data) < 12:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


# Suppress mypy "uuid imported but unused" — kept on hand for future
# request-deduplication tokens beyond the existing per-mail uuid.
_ = uuid
