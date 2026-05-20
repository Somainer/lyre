"""SSE endpoint — push new owner-mailbox messages to subscribers.

Each connection subscribes a queue to the broadcaster, then streams rendered
HTML partials as Server-Sent Events. Browser side uses native EventSource.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

router = APIRouter()


@router.get("/sse/mailbox")
async def sse_mailbox(request: Request, recipient: str = "owner"):
    broadcaster = request.app.state.broadcaster

    async def event_stream():
        queue = broadcaster.subscribe()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if msg.recipient != recipient:
                    continue
                # Compact JSON payload — clients render via small JS helper.
                payload = {
                    "id": msg.id,
                    "sender": msg.sender,
                    "recipient": msg.recipient,
                    "urgency": msg.urgency,
                    "title": msg.title,
                    "body": msg.body,
                    "task_id": msg.task_id,
                }
                yield (
                    "event: mailbox\n"
                    f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                )
        finally:
            broadcaster.unsubscribe(queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering if proxied
        },
    )
