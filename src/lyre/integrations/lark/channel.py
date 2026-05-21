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
import threading
import uuid
from typing import TYPE_CHECKING, Any, ClassVar

import structlog

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
        Lark messages — passed in (vs. read off the config) so the
        bootstrap rename feature works without LarkChannel re-reading
        config on every event."""
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
        import lark_oapi

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
            .build()
        )
        ws = self._lark.ws.Client(
            self.cfg.app_id,
            self.cfg.app_secret,
            event_handler=handler,
        )
        thread = threading.Thread(
            target=ws.start, name="lark-ws", daemon=True,
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
            sender_user_id = sender.sender_id.user_id if sender else None
            if sender_user_id != self.cfg.authorized_user_id:
                log.debug(
                    "lark_event_unauthorized_sender",
                    got=sender_user_id,
                    expected=self.cfg.authorized_user_id,
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

            # Download any image attachments.
            attachments = await self._download_images(
                msg_id, image_keys,
            )

            await self.repos.mailbox.ensure_mailbox(addr.recipient)
            from ...persistence.models import MailboxMessage
            inserted_id = await self.repos.mailbox.insert_message(
                MailboxMessage(
                    recipient=addr.recipient,
                    external_id=f"lark:{msg_id}",
                    sender="owner",
                    urgency="normal",
                    body=addr.body,
                    parent_msg_id=parent_mail_id,
                    attachments=attachments or None,
                    metadata={
                        "channels": {
                            "lark": {
                                "message_id": msg_id,
                                "user_id": sender_user_id,
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
        # SQLite JSON1 path query — small mail volumes per user,
        # acceptable to scan. If this becomes hot we can add an
        # index on metadata->>'$.channels.lark.message_id'.
        async with self.repos.conn.execute(
            "SELECT * FROM mailbox_messages "
            "WHERE json_extract(metadata, "
            "  '$.channels.lark.message_id') = ? "
            "LIMIT 1",
            (lark_message_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return self.repos.mailbox._row_to_msg(row)

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
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

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
            data = resp.file.getvalue() if isinstance(resp.file, io.IOBase) else resp.file
            # Lark doesn't surface a MIME type on download. Sniff
            # from the magic bytes — png/jpg/gif/webp cover the
            # vast majority of screenshots and phone-camera images.
            media_type = _sniff_image_mime(data) or "image/png"
            blob_id = self.blob_store.write(data, media_type)
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
        ride alongside the text post in the same Lark thread."""
        from lark_oapi.api.im.v1 import (
            CreateImageRequest,
            CreateImageRequestBody,
            CreateMessageRequest,
            CreateMessageRequestBody,
        )

        # Compose the text body — prefix with the sender so owner
        # knows which agent is talking. Markdown survives in Lark
        # as plaintext (the bot account doesn't render rich cards
        # in MVP; that's a follow-up).
        text_body = f"[{msg.sender}] {msg.body or ''}"

        text_req = (
            CreateMessageRequest.builder()
            .receive_id_type("user_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(self.cfg.authorized_user_id)
                .msg_type("text")
                .content(_json.dumps({"text": text_body}))
                .uuid(f"lyre-mail-{msg.id}")  # SDK-side dedup token
                .build()
            )
            .build()
        )
        text_resp = await self._api_client.im.v1.message.acreate(text_req)
        if not text_resp.success():
            raise RuntimeError(
                f"Lark text post failed: code={text_resp.code} "
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
                    raw = self.blob_store.read(blob.id, blob.media_type)
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
                img_req = (
                    CreateMessageRequest.builder()
                    .receive_id_type("user_id")
                    .request_body(
                        CreateMessageRequestBody.builder()
                        .receive_id(self.cfg.authorized_user_id)
                        .msg_type("image")
                        .content(_json.dumps(
                            {"image_key": up_resp.data.image_key},
                        ))
                        .uuid(f"lyre-mail-{msg.id}-img-{blob_id[:8]}")
                        .build()
                    )
                    .build()
                )
                img_resp = await self._api_client.im.v1.message.acreate(img_req)
                if not img_resp.success():
                    log.warning(
                        "lark_image_post_failed",
                        blob_id=blob_id,
                        code=img_resp.code, msg=img_resp.msg,
                    )

        return lark_msg_id


# ---------------------------------------------------------------------------
# Helpers — kept module-level so they're easy to test without spinning up
# a full LarkChannel.
# ---------------------------------------------------------------------------


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
