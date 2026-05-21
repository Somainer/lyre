"""Adapter-level image/document block translation.

Tests the pure conversion functions on each adapter — no live API
calls. The adapter must accept a ``BlobStore`` and emit the
provider's native multimodal shape.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from lyre.adapter.anthropic import AnthropicAdapter
from lyre.adapter.llm_adapter import LyreContentBlock, LyreMessage
from lyre.adapter.openai import OpenAIAdapter
from lyre.adapter.openai_responses import OpenAIResponsesAdapter
from lyre.runtime.blob_store import BlobStore

# Single 1x1 transparent PNG used across tests — small enough to base64-
# encode inline and check for exact equality without polluting the test
# output with kilobyte strings.
_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture
def blob_store(tmp_path: Path) -> tuple[BlobStore, str]:
    """Pre-populated blob store with one PNG. Returns the store and
    the blob_id so each test can reference the same image."""
    store = BlobStore(tmp_path)
    blob_id = store.write(_PNG_BYTES, "image/png")
    return store, blob_id


def _image_msg(blob_id: str) -> LyreMessage:
    return LyreMessage(
        role="user",
        content=[
            LyreContentBlock(type="text", text="what's in this image?"),
            LyreContentBlock(
                type="image", blob_id=blob_id, media_type="image/png",
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


def test_anthropic_image_block_to_base64_source(
    blob_store: tuple[BlobStore, str],
) -> None:
    """Anthropic's shape: `{type:image, source:{type:base64,
    media_type, data}}`. We compute the base64 inline at the adapter
    boundary so transcripts/logs never carry raw bytes."""
    store, blob_id = blob_store
    out = AnthropicAdapter._lyre_to_anthropic_messages(
        [_image_msg(blob_id)], blob_store=store,
    )
    # One user message with two content blocks: text + image.
    assert len(out) == 1
    assert out[0]["role"] == "user"
    blocks = out[0]["content"]
    assert len(blocks) == 2
    assert blocks[0] == {"type": "text", "text": "what's in this image?"}
    assert blocks[1] == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(_PNG_BYTES).decode("ascii"),
        },
    }


def test_anthropic_document_block_uses_document_type(
    blob_store: tuple[BlobStore, str],
) -> None:
    """PDFs land as `type:"document"`, otherwise identical source shape."""
    store, _ = blob_store
    pdf_id = store.write(b"%PDF-1.4\n...", "application/pdf")
    msg = LyreMessage(
        role="user",
        content=[
            LyreContentBlock(
                type="document",
                blob_id=pdf_id,
                media_type="application/pdf",
            ),
        ],
    )
    out = AnthropicAdapter._lyre_to_anthropic_messages([msg], blob_store=store)
    assert out[0]["content"][0]["type"] == "document"
    assert out[0]["content"][0]["source"]["media_type"] == "application/pdf"


def test_anthropic_image_block_without_blob_store_raises(
    blob_store: tuple[BlobStore, str],
) -> None:
    """Programmer error: passing image block without a BlobStore must
    fail loudly, not silently drop the image (which would let the
    model answer as if it could see when it can't)."""
    _, blob_id = blob_store
    with pytest.raises(ValueError, match="blob_store"):
        AnthropicAdapter._lyre_to_anthropic_messages(
            [_image_msg(blob_id)], blob_store=None,
        )


# ---------------------------------------------------------------------------
# OpenAI Chat Completions
# ---------------------------------------------------------------------------


def test_openai_chat_image_block_to_image_url_data_uri(
    blob_store: tuple[BlobStore, str],
) -> None:
    """Chat Completions: user.content becomes a LIST of parts when
    images are present: `[{type:text}, {type:image_url, image_url:
    {url: "data:...;base64,..."}}]`."""
    store, blob_id = blob_store
    out = OpenAIAdapter._lyre_to_openai_messages(
        [_image_msg(blob_id)], system=None, blob_store=store,
    )
    assert len(out) == 1
    msg = out[0]
    assert msg["role"] == "user"
    content = msg["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "what's in this image?"}
    expected_url = (
        "data:image/png;base64,"
        + base64.b64encode(_PNG_BYTES).decode("ascii")
    )
    assert content[1] == {
        "type": "image_url", "image_url": {"url": expected_url},
    }


def test_openai_chat_text_only_msg_stays_string_form(
    blob_store: tuple[BlobStore, str],
) -> None:
    """Compat providers (DeepSeek-OAI, OpenRouter) reject the
    multimodal list form when there's no image. Only switch to list
    when at least one image is present — keeps the legacy path
    untouched."""
    store, _ = blob_store
    msg = LyreMessage(
        role="user",
        content=[LyreContentBlock(type="text", text="hello")],
    )
    out = OpenAIAdapter._lyre_to_openai_messages(
        [msg], system=None, blob_store=store,
    )
    assert out[0]["content"] == "hello"  # plain string, NOT a list


def test_openai_chat_document_block_raises(
    blob_store: tuple[BlobStore, str],
) -> None:
    """Chat Completions has no first-class PDF input — surfacing a
    clear error beats silently dropping the document."""
    store, _ = blob_store
    pdf_id = store.write(b"%PDF-1.4", "application/pdf")
    msg = LyreMessage(
        role="user",
        content=[LyreContentBlock(
            type="document", blob_id=pdf_id, media_type="application/pdf",
        )],
    )
    with pytest.raises(ValueError, match="document"):
        OpenAIAdapter._lyre_to_openai_messages(
            [msg], system=None, blob_store=store,
        )


# ---------------------------------------------------------------------------
# OpenAI Responses
# ---------------------------------------------------------------------------


def test_responses_image_block_to_input_image(
    blob_store: tuple[BlobStore, str],
) -> None:
    """Responses API: user message content carries `input_text` +
    `input_image` parts. `input_image.image_url` is a flat data URI
    string (NOT a nested dict like Chat Completions)."""
    store, blob_id = blob_store
    out = OpenAIResponsesAdapter._lyre_to_responses_input(
        [_image_msg(blob_id)], blob_store=store,
    )
    # One message item carrying both text and image parts.
    msg_items = [x for x in out if x.get("type") == "message"]
    assert len(msg_items) == 1
    parts = msg_items[0]["content"]
    assert parts[0] == {
        "type": "input_text", "text": "what's in this image?",
    }
    expected_url = (
        "data:image/png;base64,"
        + base64.b64encode(_PNG_BYTES).decode("ascii")
    )
    assert parts[1] == {"type": "input_image", "image_url": expected_url}


def test_responses_image_only_message_emits_message_item(
    blob_store: tuple[BlobStore, str],
) -> None:
    """A message with ONLY an image (no text) still produces a
    message item — earlier we required text_parts to be non-empty,
    which would silently drop image-only sends."""
    store, blob_id = blob_store
    msg = LyreMessage(
        role="user",
        content=[LyreContentBlock(
            type="image", blob_id=blob_id, media_type="image/png",
        )],
    )
    out = OpenAIResponsesAdapter._lyre_to_responses_input(
        [msg], blob_store=store,
    )
    msg_items = [x for x in out if x.get("type") == "message"]
    assert len(msg_items) == 1
    assert any(p.get("type") == "input_image" for p in msg_items[0]["content"])


# ---------------------------------------------------------------------------
# Vision degrade-gracefully — agent_loop strips image blocks before
# dispatching to a non-vision-capable model so the request still flies.
# ---------------------------------------------------------------------------


def test_strip_vision_blocks_replaces_image_with_text_placeholder(
    blob_store: tuple[BlobStore, str],
) -> None:
    from lyre.runtime.agent_loop import _strip_vision_blocks

    _, blob_id = blob_store
    msgs = [
        LyreMessage(
            role="user",
            content=[
                LyreContentBlock(type="text", text="look:"),
                LyreContentBlock(
                    type="image", blob_id=blob_id,
                    media_type="image/png", filename="shot.png",
                ),
            ],
        ),
    ]
    out = _strip_vision_blocks(msgs)
    assert len(out) == 1
    blocks = out[0].content
    assert blocks[0].type == "text" and blocks[0].text == "look:"
    assert blocks[1].type == "text"
    assert "shot.png" in (blocks[1].text or "")
    assert "vision" in (blocks[1].text or "")


def test_strip_vision_blocks_leaves_text_only_messages_untouched(
    blob_store: tuple[BlobStore, str],
) -> None:
    """No image present → return the SAME list/messages, not copies.
    Cheap fast path: 99% of turns have no image blocks."""
    from lyre.runtime.agent_loop import _strip_vision_blocks

    msgs = [
        LyreMessage(
            role="user",
            content=[LyreContentBlock(type="text", text="plain text")],
        ),
    ]
    out = _strip_vision_blocks(msgs)
    # Same object identity — proves no copy was made when not needed.
    assert out[0] is msgs[0]


def test_strip_vision_blocks_uses_blob_id_prefix_when_no_filename(
    blob_store: tuple[BlobStore, str],
) -> None:
    """Filename is optional. When absent, the placeholder shows a
    short blob-id prefix so the operator can still match it to a
    specific upload in the dashboard."""
    from lyre.runtime.agent_loop import _strip_vision_blocks

    _, blob_id = blob_store
    msgs = [
        LyreMessage(
            role="user",
            content=[LyreContentBlock(
                type="image", blob_id=blob_id, media_type="image/png",
            )],
        ),
    ]
    out = _strip_vision_blocks(msgs)
    text = out[0].content[0].text or ""
    assert blob_id[:12] in text
