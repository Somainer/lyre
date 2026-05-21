"""Dashboard multimodal flow: /send file upload → blob → mail preview.

Exercises the owner-side path end to end via TestClient: POST /send
with a real PNG file, verify the blob lands in the store + DB, the
mail row carries the blob_id in attachments, GET /blobs/<id> serves
the bytes back with the right media type, and the mail detail page
embeds the image.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from fastapi.testclient import TestClient

from lyre.dashboard import MailboxBroadcaster, create_app
from lyre.persistence.db import init_db
from lyre.persistence.models import Persona
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.blob_store import BlobStore

# 1x1 transparent PNG — small, exact, byte-stable across runs.
_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest_asyncio.fixture
async def mm_client(
    tmp_path: Path,
) -> AsyncIterator[tuple[TestClient, SqliteRepositories, BlobStore]]:
    """Dashboard app wired with a real BlobStore in tmp_path. Seeds the
    `dispatcher` persona/agent so /send POST has somewhere to deliver."""
    db = tmp_path / "lyre.db"
    obj = tmp_path / "objstore"
    obj.mkdir(parents=True)
    conn = await init_db(db)
    repos = SqliteRepositories(conn)
    await repos.personas.upsert(
        Persona(name="dispatcher", role_description="d", system_prompt="d")
    )
    await repos.agents.create(agent_id="dispatcher", persona_name="dispatcher")
    await repos.mailbox.ensure_mailbox("owner")
    await repos.mailbox.ensure_mailbox("dispatcher")

    blob_store = BlobStore(obj)
    broadcaster = MailboxBroadcaster(
        repos=repos, recipient="owner", poll_interval_s=0.05,
    )
    await broadcaster.prime()
    app = create_app(repos, broadcaster, blob_store=blob_store)
    client = TestClient(app)
    try:
        yield client, repos, blob_store
    finally:
        await conn.close()


def test_send_post_with_image_attachment_writes_blob_and_attaches(
    mm_client: tuple[TestClient, SqliteRepositories, BlobStore],
) -> None:
    """End-to-end: POST /send with a PNG file should
      1. write the bytes to BlobStore (on-disk content-addressed),
      2. upsert a `blobs` row with media_type and size,
      3. land the mail in recipient inbox with `attachments=[blob_id]`."""
    client, repos, store = mm_client
    files = {"attachments": ("shot.png", _PNG_BYTES, "image/png")}
    data = {
        "persona": "dispatcher",
        "name": "",  # bootstrap persona doesn't need a name
        "body": "look at this",
        "urgency": "normal",
        "sender": "owner",
    }
    r = client.post("/send", data=data, files=files)
    assert r.status_code == 200, r.text
    # Success message in HTML body — proves we got past validation.
    assert "1 attachment" in r.text

    import asyncio

    async def verify() -> None:
        msgs = await repos.mailbox.read_messages("dispatcher")
        assert msgs, "mail not delivered to dispatcher"
        msg = msgs[-1]
        assert msg.attachments and len(msg.attachments) == 1
        blob_id = msg.attachments[0]
        # DB metadata is correct.
        blob = await repos.blobs.get(blob_id)
        assert blob is not None
        assert blob.media_type == "image/png"
        assert blob.size_bytes == len(_PNG_BYTES)
        assert blob.filename == "shot.png"
        assert blob.source == "owner"
        # Bytes on disk match exactly.
        assert store.read(blob_id, "image/png") == _PNG_BYTES

    asyncio.run(verify())


def test_send_post_rejects_unsupported_media_type(
    mm_client: tuple[TestClient, SqliteRepositories, BlobStore],
) -> None:
    """Form-side allowlist: anything not image-or-PDF must bounce with
    a clear error rather than land in the blob store."""
    client, *_ = mm_client
    files = {
        "attachments": ("malware.exe", b"MZ\x90\x00", "application/x-msdownload"),
    }
    data = {
        "persona": "dispatcher", "name": "",
        "body": "x", "urgency": "normal", "sender": "owner",
    }
    r = client.post("/send", data=data, files=files)
    assert r.status_code == 400
    assert "unsupported attachment" in r.text


def test_send_post_rejects_oversize_attachment(
    mm_client: tuple[TestClient, SqliteRepositories, BlobStore],
) -> None:
    """The 10 MiB cap protects mailbox / SSE / vision-token budget.
    Test with a single byte over the limit so we don't waste runtime
    on actual 10 MiB allocations."""
    from lyre.dashboard.routes.send import _MAX_BLOB_BYTES

    client, *_ = mm_client
    payload = b"x" * (_MAX_BLOB_BYTES + 1)
    files = {"attachments": ("big.png", payload, "image/png")}
    data = {
        "persona": "dispatcher", "name": "",
        "body": "x", "urgency": "normal", "sender": "owner",
    }
    r = client.post("/send", data=data, files=files)
    assert r.status_code == 400
    assert "cap is" in r.text


def test_blob_route_serves_bytes_with_correct_media_type(
    mm_client: tuple[TestClient, SqliteRepositories, BlobStore],
) -> None:
    """GET /blobs/<id> after an upload returns the exact bytes and
    the content-type that round-tripped through the metadata row."""
    client, repos, store = mm_client
    # Write a blob directly (simulating a prior upload).
    blob_id = store.write(_PNG_BYTES, "image/png")
    import asyncio

    from lyre.persistence.models import Blob
    asyncio.run(repos.blobs.upsert(Blob(
        id=blob_id, media_type="image/png",
        size_bytes=len(_PNG_BYTES), filename="shot.png", source="owner",
    )))

    r = client.get(f"/blobs/{blob_id}")
    assert r.status_code == 200
    assert r.content == _PNG_BYTES
    assert r.headers["content-type"].startswith("image/png")
    # Immutable cache header — content-addressed blobs never change.
    assert "max-age=31536000" in r.headers.get("cache-control", "")


def test_blob_route_404_for_unknown_id(
    mm_client: tuple[TestClient, SqliteRepositories, BlobStore],
) -> None:
    client, *_ = mm_client
    r = client.get("/blobs/" + ("d" * 64))
    assert r.status_code == 404


def test_mail_detail_renders_attachment_preview(
    mm_client: tuple[TestClient, SqliteRepositories, BlobStore],
) -> None:
    """After uploading + sending, GET /mail/<id> must include an
    `<img src=/blobs/<id>>` for image attachments — that's how the
    owner verifies what the model saw."""
    client, repos, _store = mm_client
    files = {"attachments": ("shot.png", _PNG_BYTES, "image/png")}
    data = {
        "persona": "dispatcher", "name": "",
        "body": "look", "urgency": "normal", "sender": "owner",
    }
    r = client.post("/send", data=data, files=files)
    assert r.status_code == 200

    import asyncio

    async def get_msg_id() -> int:
        msgs = await repos.mailbox.read_messages("dispatcher")
        return msgs[-1].id or -1

    msg_id = asyncio.run(get_msg_id())
    r = client.get(f"/mail/{msg_id}")
    assert r.status_code == 200
    # Image preview tags
    assert "<img" in r.text
    assert "/blobs/" in r.text
    assert "image/png" in r.text
