"""LarkChannel — Lark-side parsing helpers + outbound publish path.

These tests don't talk to Lark — they mock the lark_oapi SDK client
so the channel logic (body parsing, image upload sequencing, error
handling, metadata-write contracts) can be exercised offline.
"""

from __future__ import annotations

import io
import json as _json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from lyre.config import LarkConfig
from lyre.integrations.lark.channel import (
    LarkChannel,
    _build_owner_mail_card,
    _extract_body_and_images,
    _parse_urgency_prefix,
    _sniff_image_mime,
)
from lyre.persistence.db import init_db
from lyre.persistence.models import Blob, MailboxMessage, Persona
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.blob_store import BlobStore

# Small PNG fixture — byte-stable for image sniffing assertions.
_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


# ---------------------------------------------------------------------------
# _extract_body_and_images — Lark content JSON → (body, image_keys)
# ---------------------------------------------------------------------------


def test_extract_text_message() -> None:
    body, images = _extract_body_and_images(
        "text", _json.dumps({"text": "hello team"}),
    )
    assert body == "hello team"
    assert images == []


def test_extract_image_message_yields_key() -> None:
    body, images = _extract_body_and_images(
        "image", _json.dumps({"image_key": "img_abc"}),
    )
    assert body == ""
    assert images == ["img_abc"]


def test_extract_post_concatenates_text_and_collects_images() -> None:
    """Rich post: nested list of paragraphs of blocks. Text blocks
    join with newlines; img blocks contribute keys."""
    content = _json.dumps({
        "content": [
            [
                {"tag": "text", "text": "look at this:"},
            ],
            [
                {"tag": "img", "image_key": "img_1"},
                {"tag": "text", "text": " (kept inline)"},
            ],
        ],
    })
    body, images = _extract_body_and_images("post", content)
    assert "look at this:" in body
    assert "(kept inline)" in body
    assert images == ["img_1"]


def test_extract_handles_garbage_content_json() -> None:
    """A malformed content JSON shouldn't crash the WS thread —
    return empty body + no images so the inbound path just drops
    the message with a debug log upstream."""
    assert _extract_body_and_images("text", "not-json") == ("", [])
    assert _extract_body_and_images("text", None) == ("", [])
    assert _extract_body_and_images("unknown_type", "{}") == ("", [])


# ---------------------------------------------------------------------------
# _sniff_image_mime — magic-byte detection for inbound images
# ---------------------------------------------------------------------------


def test_sniff_recognizes_png() -> None:
    assert _sniff_image_mime(_PNG_BYTES) == "image/png"


def test_sniff_recognizes_jpeg() -> None:
    assert _sniff_image_mime(b"\xff\xd8\xff\xe0" + b"\x00" * 20) == "image/jpeg"


def test_sniff_recognizes_gif() -> None:
    assert _sniff_image_mime(b"GIF89a" + b"\x00" * 20) == "image/gif"


def test_sniff_recognizes_webp() -> None:
    assert _sniff_image_mime(b"RIFF\x00\x00\x00\x00WEBP" + b"x" * 20) == "image/webp"


def test_sniff_returns_none_for_unknown() -> None:
    assert _sniff_image_mime(b"random non-image bytes") is None
    assert _sniff_image_mime(b"") is None


# ---------------------------------------------------------------------------
# LarkChannel construction — guards on missing config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_channel_refuses_without_app_credentials(
    tmp_path: Path,
) -> None:
    """Missing app_id/app_secret → loud failure at construction.
    Better to fail at `lyre serve` startup than silently never
    deliver any owner mail."""
    conn = await init_db(tmp_path / "lyre.db")
    try:
        repos = SqliteRepositories(conn)
        cfg = LarkConfig(
            enabled=True, authorized_user_id="ou_x",
            app_id=None, app_secret=None,
        )
        with pytest.raises(ValueError, match="LARK_APP_ID"):
            LarkChannel(cfg, repos, None, dispatcher_id="dispatcher")
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_channel_refuses_without_authorized_user_id(
    tmp_path: Path,
) -> None:
    """Open-to-anyone mode is forbidden — without
    authorized_user_id the bot would accept tasks from anyone in
    the same tenant. Fail loudly."""
    conn = await init_db(tmp_path / "lyre.db")
    try:
        repos = SqliteRepositories(conn)
        cfg = LarkConfig(
            enabled=True, authorized_user_id=None,
            app_id="cli_x", app_secret="s",
        )
        with pytest.raises(ValueError, match="authorized_user_id"):
            LarkChannel(cfg, repos, None, dispatcher_id="dispatcher")
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# _parse_urgency_prefix — owner-typed `!blocker` / `!urgent` / `!high` / `!low`
# ---------------------------------------------------------------------------


def test_urgency_prefix_each_token_strips_and_maps() -> None:
    """Each recognized token at the very start maps to the right
    mailbox urgency and is stripped (along with the following space)
    from the body so agents don't see the meta-marker."""
    assert _parse_urgency_prefix("!blocker drop everything now") == (
        "blocker", "drop everything now",
    )
    # !urgent aliases to high (matches how people actually type).
    assert _parse_urgency_prefix("!urgent fix this") == ("high", "fix this")
    assert _parse_urgency_prefix("!high heads up") == ("high", "heads up")
    assert _parse_urgency_prefix("!low FYI when you can") == (
        "low", "FYI when you can",
    )


def test_urgency_prefix_case_insensitive() -> None:
    assert _parse_urgency_prefix("!BLOCKER stop")[0] == "blocker"
    assert _parse_urgency_prefix("!Urgent x")[0] == "high"
    assert _parse_urgency_prefix("!High y")[0] == "high"


def test_urgency_prefix_default_normal_when_absent() -> None:
    """No recognized prefix → urgency stays normal, body unchanged."""
    assert _parse_urgency_prefix("just a regular message") == (
        "normal", "just a regular message",
    )
    assert _parse_urgency_prefix("") == ("normal", "")


def test_urgency_prefix_unknown_token_passes_through() -> None:
    """`!important` looks like a urgency token but isn't recognized —
    the channel shouldn't silently strip it (leaves owner's text
    intact, urgency falls back to normal)."""
    assert _parse_urgency_prefix("!important note") == (
        "normal", "!important note",
    )
    assert _parse_urgency_prefix("!nope") == ("normal", "!nope")


def test_urgency_prefix_word_boundary_prevents_partial_match() -> None:
    """`!blockerfoo` must NOT match `!blocker` — without the word
    boundary check the parser would eat the prefix and mangle the
    body to "foo"."""
    assert _parse_urgency_prefix("!blockerfoo bar") == (
        "normal", "!blockerfoo bar",
    )
    assert _parse_urgency_prefix("!lowness check") == (
        "normal", "!lowness check",
    )


def test_urgency_prefix_only_at_start() -> None:
    """A prefix that appears mid-body is not honoured (otherwise any
    mention of `!blocker` would change urgency, which is surprising)."""
    assert _parse_urgency_prefix("see also !blocker note") == (
        "normal", "see also !blocker note",
    )


def test_urgency_prefix_alone_yields_empty_body() -> None:
    """`!blocker` with nothing after still parses — empty body, but
    the urgency is set. (Whether the rest of the pipeline accepts an
    empty body is a separate concern.)"""
    assert _parse_urgency_prefix("!blocker") == ("blocker", "")
    assert _parse_urgency_prefix("!blocker   ") == ("blocker", "")


# ---------------------------------------------------------------------------
# _build_owner_mail_card — pure function, no SDK
# ---------------------------------------------------------------------------


def test_card_no_title_has_no_header_and_sender_rides_body() -> None:
    """When no title is provided (or it auto-derived to the body's
    first line), the card has no ``header`` block at all — owner sees
    a pure body card. The body always starts with ``**from <sender>**``
    so attribution still lands. Body uses lark_md so markdown renders."""
    card = _build_owner_mail_card(
        sender="analyst/auth",
        body="**done**: see `~/.lyre/memory/facts/specs-auth.md`",
        urgency="normal",
    )
    assert "header" not in card
    body_el = card["elements"][0]
    assert body_el["tag"] == "div"
    assert body_el["text"]["tag"] == "lark_md"
    content = body_el["text"]["content"]
    assert content.startswith("**from analyst/auth**\n\n")
    assert content.endswith("**done**: see `~/.lyre/memory/facts/specs-auth.md`")


def test_card_meaningful_title_promotes_to_header_with_urgency_color() -> None:
    """A sender-supplied title that differs from the body's first line
    is "meaningful" — promote it to the header (with urgency-coloured
    template) and keep the uniform attribution body prefix. blocker
    urgency also stamps the ``🔴`` dot on the body so the urgency signal
    survives even when Lark clients downplay header colour."""
    card = _build_owner_mail_card(
        sender="worker-maintainer-1",
        body="Hit 429s on the prod sync.\nTwo options...",
        urgency="blocker",
        title="Need decision on rate-limit policy",
    )
    assert card["header"]["title"]["content"] == "Need decision on rate-limit policy"
    assert card["header"]["template"] == "red"  # blocker → red
    body = card["elements"][0]["text"]["content"]
    assert body.startswith("🔴 **from worker-maintainer-1**\n\n")
    assert body.endswith("Hit 429s on the prod sync.\nTwo options...")


def test_card_auto_derived_title_drops_header() -> None:
    """If the title was auto-derived from the body's first line (the
    persistence layer does this when sender didn't supply one), it
    would just duplicate body[0] in the header — drop the header
    block entirely and let the ``**from <sender>**`` body prefix carry
    attribution. Keeps the card from showing the same sentence twice."""
    body = "Quick status update.\n\nAll three workers green."
    derived = "Quick status update."  # what _derive_title_from_body returns
    card = _build_owner_mail_card(
        sender="dispatcher",
        body=body,
        urgency="normal",
        title=derived,
    )
    assert "header" not in card
    content = card["elements"][0]["text"]["content"]
    assert content.startswith("**from dispatcher**\n\n")
    assert content.endswith(body)


def test_card_urgency_maps_to_header_template_when_titled() -> None:
    """Urgency colours the header bar when there *is* a header to
    colour. Unknown urgency falls back to blue. (No-title cards have
    no header and thus no colour — see no-title tests above.)"""
    def tpl(urgency: str) -> str:
        card = _build_owner_mail_card(
            "sender", "body", urgency, title="Subject",
        )
        return card["header"]["template"]

    assert tpl("blocker") == "red"
    assert tpl("high") == "orange"
    assert tpl("normal") == "blue"
    assert tpl("low") == "grey"
    assert tpl("weird") == "blue"


def test_card_empty_body_no_title_shows_sender_only() -> None:
    """Empty body + no title → the card is just ``**from <sender>**``.
    Still well-formed content for Lark (non-empty lark_md text), and
    visually says "agent with no message body", which is rare but
    happens with synthetic notifications."""
    card = _build_owner_mail_card("watchdog", "", "normal")
    assert "header" not in card
    assert card["elements"][0]["text"]["content"] == "**from watchdog**"


def test_card_meaningful_title_with_empty_body() -> None:
    """Title-only mail (empty body, sender-supplied subject) — header
    shows the title, body falls back to the attribution line so the
    card still has visible content. ``high`` urgency stamps the 🟠 dot
    on the attribution line."""
    card = _build_owner_mail_card(
        sender="watchdog", body="", urgency="high", title="Heartbeat missed",
    )
    assert card["header"]["title"]["content"] == "Heartbeat missed"
    assert card["elements"][0]["text"]["content"] == "🟠 **from watchdog**"


def test_card_urgency_body_marker_only_for_elevated_urgency() -> None:
    """Body-level traffic-light marker: only blocker/high get a dot,
    normal/low stay clean. Owner can spot elevated mail at a glance
    even when the card has no coloured header (auto-derived title)."""
    def first_line(urgency: str) -> str:
        card = _build_owner_mail_card("a", "x", urgency)
        return card["elements"][0]["text"]["content"].split("\n", 1)[0]

    assert first_line("blocker") == "🔴 **from a**"
    assert first_line("high") == "🟠 **from a**"
    assert first_line("normal") == "**from a**"
    assert first_line("low") == "**from a**"
    assert first_line("weird") == "**from a**"  # unknown → no marker


# ---------------------------------------------------------------------------
# publish_owner_mail — outbound text + image with fake SDK client
# ---------------------------------------------------------------------------


def _make_channel_with_mocked_client(
    repos: SqliteRepositories, blob_store: BlobStore | None,
) -> LarkChannel:
    """Build a LarkChannel, then swap its SDK client with mocks for
    each API call we care about."""
    cfg = LarkConfig(
        enabled=True,
        authorized_user_id="ou_owner",
        app_id="cli_test",
        app_secret="secret",
    )
    ch = LarkChannel(cfg, repos, blob_store, dispatcher_id="dispatcher")
    # Mock the SDK client's nested attribute chain: client.im.v1.message
    # .acreate(...) → mocked response.
    api = MagicMock()
    api.im.v1.message.acreate = AsyncMock()
    api.im.v1.message.areply = AsyncMock()
    api.im.v1.image.acreate = AsyncMock()
    api.im.v1.message_resource.aget = AsyncMock()
    api.im.v1.message_reaction.acreate = AsyncMock()
    ch._api_client = api
    return ch


def _ok_message_response(message_id: str) -> Any:
    """Build a fake response object matching the SDK's CreateMessageResponse shape."""
    resp = MagicMock()
    resp.success.return_value = True
    resp.code = 0
    resp.msg = "ok"
    resp.data.message_id = message_id
    return resp


def _ok_image_response(image_key: str) -> Any:
    resp = MagicMock()
    resp.success.return_value = True
    resp.code = 0
    resp.msg = "ok"
    resp.data.image_key = image_key
    return resp


@pytest.mark.asyncio
async def test_publish_text_only_returns_lark_message_id(
    tmp_path: Path,
) -> None:
    """Bare text owner-mail → one acreate call, returns Lark
    message id. The outbox dispatcher persists this id back to
    metadata.channels.lark.message_id (verified in outbox tests)."""
    conn = await init_db(tmp_path / "lyre.db")
    try:
        repos = SqliteRepositories(conn)
        await repos.mailbox.ensure_mailbox("owner")
        ch = _make_channel_with_mocked_client(repos, None)
        ch._api_client.im.v1.message.acreate.return_value = (
            _ok_message_response("lark_msg_001")
        )

        msg = MailboxMessage(
            id=99, recipient="owner", external_id="m1",
            sender="worker-maintainer/refactor-auth",
            urgency="normal", body="status: done",
        )
        ext_id = await ch.publish_owner_mail(msg, reply_to_external_id=None)

        assert ext_id == "lark_msg_001"
        # One card post, no image post.
        assert ch._api_client.im.v1.message.acreate.await_count == 1
        assert ch._api_client.im.v1.image.acreate.await_count == 0
        # The outbound request is an interactive card with the sender
        # attribution on the body and no header block (no sender-supplied
        # title, body's first line auto-derived as the title — header
        # would just duplicate it).
        call = ch._api_client.im.v1.message.acreate.await_args
        sent_req = call.args[0]
        assert sent_req.body.msg_type == "interactive"
        card = _json.loads(sent_req.body.content)
        assert "header" not in card
        assert card["elements"][0]["text"]["tag"] == "lark_md"
        assert card["elements"][0]["text"]["content"] == (
            "**from worker-maintainer/refactor-auth**\n\nstatus: done"
        )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_publish_with_image_uploads_then_posts(
    tmp_path: Path,
) -> None:
    """Image attachment → image.acreate (upload to get key) →
    message.acreate(text) → message.acreate(image, key). The text
    post still wins as the return value (its id is what we thread
    against)."""
    conn = await init_db(tmp_path / "lyre.db")
    try:
        repos = SqliteRepositories(conn)
        await repos.mailbox.ensure_mailbox("owner")
        # Seed a blob both on disk + in the metadata table.
        blob_store = BlobStore(tmp_path / "objstore")
        blob_id = blob_store.write(_PNG_BYTES, "image/png")
        await repos.blobs.upsert(Blob(
            id=blob_id, media_type="image/png",
            size_bytes=len(_PNG_BYTES), filename="shot.png",
            source="owner",
        ))

        ch = _make_channel_with_mocked_client(repos, blob_store)
        ch._api_client.im.v1.image.acreate.return_value = (
            _ok_image_response("img_key_xyz")
        )
        # The text post AND the image post both call message.acreate.
        # Distinguish responses via side_effect.
        ch._api_client.im.v1.message.acreate.side_effect = [
            _ok_message_response("lark_text"),
            _ok_message_response("lark_image_followup"),
        ]

        msg = MailboxMessage(
            id=42, recipient="owner", external_id="m2",
            sender="worker-maintainer/coco", urgency="normal",
            body="see attached", attachments=[blob_id],
        )
        ext_id = await ch.publish_owner_mail(msg, reply_to_external_id=None)

        assert ext_id == "lark_text"  # text-post id wins
        assert ch._api_client.im.v1.image.acreate.await_count == 1
        assert ch._api_client.im.v1.message.acreate.await_count == 2
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_publish_with_reply_to_uses_reply_endpoint(
    tmp_path: Path,
) -> None:
    """When ``reply_to_external_id`` is set, the reply must go through
    ``messages/:id/reply`` (not ``messages``) so it nests under the
    parent in the owner's Lark UI. Without this, agent replies showed
    up as flat top-level messages even though Lyre's parent_msg_id was
    threaded correctly internally."""
    conn = await init_db(tmp_path / "lyre.db")
    try:
        repos = SqliteRepositories(conn)
        await repos.mailbox.ensure_mailbox("owner")
        ch = _make_channel_with_mocked_client(repos, None)
        ch._api_client.im.v1.message.areply.return_value = (
            _ok_message_response("lark_reply_id")
        )

        msg = MailboxMessage(
            id=200, recipient="owner", external_id="reply-1",
            sender="analyst/auth", urgency="normal",
            body="spec written",
        )
        ext_id = await ch.publish_owner_mail(
            msg, reply_to_external_id="om_parent_xyz",
        )

        assert ext_id == "lark_reply_id"
        # Reply endpoint hit; create endpoint untouched.
        assert ch._api_client.im.v1.message.areply.await_count == 1
        assert ch._api_client.im.v1.message.acreate.await_count == 0
        # The parent id flowed into the request as the path param.
        reply_call = ch._api_client.im.v1.message.areply.await_args
        sent_req = reply_call.args[0]
        assert sent_req.message_id == "om_parent_xyz"
        assert sent_req.paths["message_id"] == "om_parent_xyz"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_publish_reply_with_image_threads_image_too(
    tmp_path: Path,
) -> None:
    """Image attachments on a threaded reply must also reply to the
    parent — otherwise the image floats out of the thread, leaving
    the text reply nested but the image at top-level."""
    conn = await init_db(tmp_path / "lyre.db")
    try:
        repos = SqliteRepositories(conn)
        await repos.mailbox.ensure_mailbox("owner")
        blob_store = BlobStore(tmp_path / "objstore")
        blob_id = blob_store.write(_PNG_BYTES, "image/png")
        await repos.blobs.upsert(Blob(
            id=blob_id, media_type="image/png",
            size_bytes=len(_PNG_BYTES), filename="shot.png",
            source="owner",
        ))
        ch = _make_channel_with_mocked_client(repos, blob_store)
        ch._api_client.im.v1.image.acreate.return_value = (
            _ok_image_response("img_key_abc")
        )
        # Two reply calls: text reply + image reply.
        ch._api_client.im.v1.message.areply.side_effect = [
            _ok_message_response("lark_text_reply"),
            _ok_message_response("lark_img_reply"),
        ]

        msg = MailboxMessage(
            id=201, recipient="owner", external_id="reply-img",
            sender="analyst/webhook", urgency="normal",
            body="diagram attached", attachments=[blob_id],
        )
        ext_id = await ch.publish_owner_mail(
            msg, reply_to_external_id="om_parent_with_img",
        )

        assert ext_id == "lark_text_reply"
        assert ch._api_client.im.v1.message.areply.await_count == 2
        assert ch._api_client.im.v1.message.acreate.await_count == 0
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_publish_fails_loudly_on_text_post_error(
    tmp_path: Path,
) -> None:
    """If the SDK reports the text post failed, raise so the outbox
    marks the row failed (and retries on next tick)."""
    conn = await init_db(tmp_path / "lyre.db")
    try:
        repos = SqliteRepositories(conn)
        ch = _make_channel_with_mocked_client(repos, None)
        err_resp = MagicMock()
        err_resp.success.return_value = False
        err_resp.code = 99991663
        err_resp.msg = "rate-limited"
        ch._api_client.im.v1.message.acreate.return_value = err_resp

        msg = MailboxMessage(
            id=7, recipient="owner", external_id="m3",
            sender="x", urgency="normal", body="hi",
        )
        with pytest.raises(RuntimeError, match="rate-limited"):
            await ch.publish_owner_mail(msg, reply_to_external_id=None)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_image_download_writes_blob_and_upserts_metadata(
    tmp_path: Path,
) -> None:
    """Inbound image: SDK aget returns BytesIO; channel writes
    bytes via BlobStore, upserts the blobs row, returns the blob_id."""
    conn = await init_db(tmp_path / "lyre.db")
    try:
        repos = SqliteRepositories(conn)
        await repos.personas.upsert(
            Persona(name="dispatcher", role_description="d", system_prompt="d")
        )
        await repos.agents.create(agent_id="dispatcher", persona_name="dispatcher")
        blob_store = BlobStore(tmp_path / "objstore")
        ch = _make_channel_with_mocked_client(repos, blob_store)

        # SDK aget returns a response with `.success() == True` and
        # `.file` as BytesIO holding the PNG bytes.
        img_resp = MagicMock()
        img_resp.success.return_value = True
        img_resp.file = io.BytesIO(_PNG_BYTES)
        ch._api_client.im.v1.message_resource.aget.return_value = img_resp

        ids = await ch._download_images(
            "lark_msg_001", ["img_key_alpha"],
        )
        assert len(ids) == 1
        blob = await repos.blobs.get(ids[0])
        assert blob is not None
        assert blob.media_type == "image/png"
        assert blob.size_bytes == len(_PNG_BYTES)
        assert blob.source == "owner"
        # Bytes round-trip via BlobStore.
        assert blob_store.read(blob.id, "image/png") == _PNG_BYTES
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# publish_reaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_reaction_maps_ack_to_ok_emoji(tmp_path: Path) -> None:
    """Lyre ``kind="ack"`` shows up on Lark as the ``OK`` emoji on the
    previously-published message id. The request body should carry the
    message_id in the path and ``reaction_type.emoji_type="OK"`` in the
    body."""
    conn = await init_db(tmp_path / "lyre.db")
    try:
        repos = SqliteRepositories(conn)
        ch = _make_channel_with_mocked_client(repos, None)
        ok = MagicMock()
        ok.success.return_value = True
        ch._api_client.im.v1.message_reaction.acreate.return_value = ok

        await ch.publish_reaction(
            external_message_id="om_target", kind="ack",
        )

        assert ch._api_client.im.v1.message_reaction.acreate.await_count == 1
        call = ch._api_client.im.v1.message_reaction.acreate.await_args
        req = call.args[0]
        assert req.message_id == "om_target"
        assert req.request_body.reaction_type.emoji_type == "OK"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_publish_reaction_swallows_lark_error(tmp_path: Path) -> None:
    """Lark returns an error when the same actor adds the same emoji
    twice. We log + swallow rather than raise — retrying wouldn't help
    and the outbox row should mark dispatched."""
    conn = await init_db(tmp_path / "lyre.db")
    try:
        repos = SqliteRepositories(conn)
        ch = _make_channel_with_mocked_client(repos, None)
        err = MagicMock()
        err.success.return_value = False
        err.code = 230002
        err.msg = "reaction already exists"
        ch._api_client.im.v1.message_reaction.acreate.return_value = err

        # Should NOT raise.
        await ch.publish_reaction(
            external_message_id="om_target", kind="ack",
        )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_publish_reaction_unknown_kind_is_no_op(tmp_path: Path) -> None:
    """If a future ReactionKind isn't in the Lark emoji map yet, do
    nothing (logged) instead of calling Lark with ``None``."""
    conn = await init_db(tmp_path / "lyre.db")
    try:
        repos = SqliteRepositories(conn)
        ch = _make_channel_with_mocked_client(repos, None)

        await ch.publish_reaction(
            external_message_id="om_target", kind="future_unmapped_kind",
        )

        assert ch._api_client.im.v1.message_reaction.acreate.await_count == 0
    finally:
        await conn.close()
