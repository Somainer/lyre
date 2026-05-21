"""Serve content-addressed blobs (mail image attachments).

Two responsibilities:

  * GET /blobs/<id>  → raw bytes for the mail-detail preview and any
    direct download. Streamed from disk; cache-headers tagged for
    long-lived cache (content-addressed → immutable). 404 when the
    metadata row or the on-disk file is missing.

Trust: the route does NOT gate on who has seen the blob. Anyone with
the dashboard URL implicitly speaks for the owner; the dashboard is
already an owner-only surface. Agent-level access control happens on
the mailbox layer (agents can only attach blob_ids they've received
in mail, enforced by mailbox_send validation).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

router = APIRouter()


@router.get("/blobs/{blob_id}")
async def get_blob(blob_id: str, request: Request) -> FileResponse:
    repos = request.app.state.repos
    blob_store = getattr(request.app.state, "blob_store", None)
    if blob_store is None:
        raise HTTPException(
            status_code=503,
            detail="blob_store unavailable (multimodal not configured)",
        )
    meta = await repos.blobs.get(blob_id)
    if meta is None:
        raise HTTPException(
            status_code=404, detail=f"blob {blob_id!r} not found"
        )
    path = blob_store.path_for(meta.id, meta.media_type)
    if not path.exists():
        # Metadata row exists but bytes are gone — the on-disk store
        # was wiped or hand-edited. Loud 404 beats silent empty body.
        raise HTTPException(
            status_code=404,
            detail=f"blob {blob_id!r} metadata exists but file missing",
        )
    # Content-addressed → bytes for this id never change. One year of
    # immutable cache is safe and avoids re-downloading on every mail
    # detail open. `inline` lets the browser render images directly.
    headers = {
        "Cache-Control": "public, max-age=31536000, immutable",
    }
    return FileResponse(
        path=path,
        media_type=meta.media_type,
        filename=meta.filename or f"{blob_id}",
        headers=headers,
        content_disposition_type="inline",
    )
