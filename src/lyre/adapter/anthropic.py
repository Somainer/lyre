"""AnthropicAdapter — the only LLMAdapter implementation in MVP.

Supports custom base_url so it can be repointed at LiteLLM proxy / Bedrock /
local Anthropic-compatible servers etc. (AGENT_RUNTIME.md §2.3).
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from anthropic import AsyncAnthropic
from anthropic.types import (
    ContentBlockDeltaEvent,
    ContentBlockStartEvent,
    ContentBlockStopEvent,
    MessageDeltaEvent,
    MessageStopEvent,
)

from .llm_adapter import (
    ContentDelta,
    LyreMessage,
    LyreToolSpec,
    StreamEvent,
    ThinkingBlockComplete,
    ThinkingDelta,
    ToolUseComplete,
    ToolUseDelta,
    ToolUseStart,
    TurnComplete,
    Usage,
)

if TYPE_CHECKING:
    from ..runtime.blob_store import BlobStore


class AnthropicAdapter:
    """Wraps AsyncAnthropic with Lyre's standardized streaming interface."""

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        timeout: float = 600.0,
        extra_headers: dict[str, str] | None = None,
        blob_store: BlobStore | None = None,
    ):
        kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout}
        if base_url:
            kwargs["base_url"] = base_url
        # Custom headers (e.g. for a proxy with its own auth scheme)
        # ride alongside the standard x-api-key header the SDK sets
        # from `api_key`. The SDK applies these to every request.
        if extra_headers:
            kwargs["default_headers"] = extra_headers
        self.client = AsyncAnthropic(**kwargs)
        # Multimodal: resolves blob_id → bytes at send-time. None means
        # the adapter raises if it encounters an image/document block —
        # tests that don't touch multimodal can leave it unset.
        self._blob_store = blob_store

    async def stream_turn(
        self,
        messages: list[LyreMessage],
        tools: list[LyreToolSpec],
        model: str,
        max_tokens: int = 4096,
        temperature: float | None = None,
        system: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        anth_messages = self._lyre_to_anthropic_messages(
            messages, blob_store=self._blob_store,
        )
        anth_tools = [self._tool_to_anthropic(t) for t in tools]

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": anth_messages,
        }
        # Cache breakpoint after tools: everything Lyre treats as "stable
        # per (agent, persona, allowed_tools) tuple" sits before this
        # point (system prompt + tool defs). With cache_control set,
        # Anthropic's prompt cache reads back the prefix on every wakeup
        # for 0.1× the input cost. DeepSeek's Anthropic-compat endpoint
        # auto-caches prefixes regardless; this just turns it on for
        # Anthropic proper too.
        if anth_tools:
            # Mark the LAST tool with cache_control. Anthropic caches
            # everything up to and including that block.
            anth_tools[-1] = {
                **anth_tools[-1],
                "cache_control": {"type": "ephemeral"},
            }
            kwargs["tools"] = anth_tools
        if system:
            # Pass system as a single text block with cache_control. If
            # there are no tools, this is what bounds the cached prefix.
            kwargs["system"] = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        if temperature is not None:
            kwargs["temperature"] = temperature

        # Track in-flight tool_use + thinking blocks. tool_use_buffers
        # accumulate input_json fragments per block index; thinking_buffers
        # accumulate the reasoning text + signature so we can emit the
        # whole thing on ContentBlockStopEvent (the provider needs the
        # whole thing echoed back on the next turn).
        tool_use_buffers: dict[int, dict[str, Any]] = {}
        thinking_buffers: dict[int, dict[str, str]] = {}

        async with self.client.messages.stream(**kwargs) as stream:
            async for event in stream:
                lyre_event = self._anthropic_to_lyre(
                    event, tool_use_buffers, thinking_buffers
                )
                if lyre_event is not None:
                    yield lyre_event

    # ----------------------------------------------------------------
    # Conversion: Lyre -> Anthropic
    # ----------------------------------------------------------------

    @staticmethod
    def _tool_to_anthropic(t: LyreToolSpec) -> dict[str, Any]:
        return {
            "name": t.name,
            "description": t.description,
            "input_schema": t.input_schema,
        }

    @staticmethod
    def _lyre_to_anthropic_messages(
        msgs: list[LyreMessage],
        blob_store: BlobStore | None = None,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in msgs:
            if m.role == "system":
                # Anthropic system prompt is passed separately, not in messages.
                continue
            anth_content: list[dict[str, Any]] = []
            for blk in m.content:
                if blk.type == "text":
                    anth_content.append({"type": "text", "text": blk.text or ""})
                elif blk.type == "thinking":
                    # Echo the reasoning block back verbatim. Signature
                    # only included if non-empty — DeepSeek's compat
                    # layer doesn't sign, and sending empty "" can be
                    # rejected. Anthropic-proper REQUIRES non-empty
                    # signature though; if it's missing on a non-
                    # DeepSeek call, the upstream will tell us.
                    block: dict[str, Any] = {
                        "type": "thinking",
                        "thinking": blk.text or "",
                    }
                    if blk.signature:
                        block["signature"] = blk.signature
                    anth_content.append(block)
                elif blk.type == "tool_use":
                    anth_content.append({
                        "type": "tool_use",
                        "id": blk.tool_use_id or "",
                        "name": blk.tool_name or "",
                        "input": blk.tool_input or {},
                    })
                elif blk.type == "tool_result":
                    content = (
                        blk.tool_result
                        if isinstance(blk.tool_result, list)
                        else str(blk.tool_result)
                    )
                    anth_content.append({
                        "type": "tool_result",
                        "tool_use_id": blk.tool_use_id or "",
                        "content": content,
                        "is_error": blk.is_error,
                    })
                elif blk.type in ("image", "document"):
                    # Anthropic's image/document shape:
                    #   {"type": "image", "source":
                    #       {"type": "base64",
                    #        "media_type": "image/png", "data": "..."}}
                    # Document blocks (PDFs) are identical except the
                    # outer type. We load bytes via BlobStore and
                    # base64-encode here at the adapter boundary so
                    # the rest of the runtime never has to materialize
                    # binary content in messages it logs / persists.
                    if blob_store is None or not blk.blob_id or not blk.media_type:
                        raise ValueError(
                            f"Cannot translate {blk.type!r} block: "
                            f"blob_store + blob_id + media_type all required"
                        )
                    data = blob_store.read(blk.blob_id, blk.media_type)
                    anth_content.append({
                        "type": blk.type,
                        "source": {
                            "type": "base64",
                            "media_type": blk.media_type,
                            "data": base64.b64encode(data).decode("ascii"),
                        },
                    })
            # Anthropic doesn't have role="tool"; tool results live under role="user"
            role = "user" if m.role == "tool" else m.role
            out.append({"role": role, "content": anth_content or ""})
        return out

    # ----------------------------------------------------------------
    # Conversion: Anthropic -> Lyre
    # ----------------------------------------------------------------

    @staticmethod
    def _anthropic_to_lyre(
        evt: Any,
        tool_use_buffers: dict[int, dict[str, Any]],
        thinking_buffers: dict[int, dict[str, str]] | None = None,
    ) -> StreamEvent | None:
        if thinking_buffers is None:
            thinking_buffers = {}
        # ContentBlockStartEvent: text / tool_use / thinking begins
        if isinstance(evt, ContentBlockStartEvent):
            blk = evt.content_block
            if blk.type == "tool_use":
                tool_use_buffers[evt.index] = {
                    "id": blk.id,
                    "name": blk.name,
                    "input_json": "",
                }
                return ToolUseStart(id=blk.id, name=blk.name)
            if blk.type == "thinking":
                # Some providers emit initial thinking text in the start
                # event itself (rare); seed the buffer with it.
                thinking_buffers[evt.index] = {
                    "text": getattr(blk, "thinking", "") or "",
                    "signature": getattr(blk, "signature", "") or "",
                }
                return None
            return None

        # ContentBlockDeltaEvent: text_delta / input_json_delta /
        # thinking_delta / signature_delta. Signature is the provider's
        # cryptographic seal on the thinking block — Anthropic requires
        # it back on the next turn; DeepSeek may emit empty.
        if isinstance(evt, ContentBlockDeltaEvent):
            d = evt.delta
            if d.type == "text_delta":
                return ContentDelta(text=d.text)
            if d.type == "thinking_delta":
                chunk = getattr(d, "thinking", "") or ""
                buf = thinking_buffers.setdefault(
                    evt.index, {"text": "", "signature": ""}
                )
                buf["text"] = buf.get("text", "") + chunk
                return ThinkingDelta(text=chunk)
            if d.type == "signature_delta":
                chunk = getattr(d, "signature", "") or ""
                buf = thinking_buffers.setdefault(
                    evt.index, {"text": "", "signature": ""}
                )
                buf["signature"] = buf.get("signature", "") + chunk
                # Signature accumulation is internal — no stream event.
                return None
            if d.type == "input_json_delta":
                buf = tool_use_buffers.get(evt.index, {})
                buf["input_json"] = buf.get("input_json", "") + d.partial_json
                tool_use_buffers[evt.index] = buf
                return ToolUseDelta(id=buf.get("id", ""), input_partial=d.partial_json)
            return None

        # ContentBlockStopEvent: block finished (tool_use or thinking
        # buffer may be assembled now).
        if isinstance(evt, ContentBlockStopEvent):
            tu_buf: dict[str, Any] | None = tool_use_buffers.pop(
                evt.index, None,
            )
            if tu_buf is not None:
                import json as _json
                input_json = tu_buf.get("input_json", "")
                try:
                    parsed_input = _json.loads(input_json) if input_json else {}
                except _json.JSONDecodeError:
                    parsed_input = {"_raw": input_json}
                return ToolUseComplete(
                    id=tu_buf["id"], name=tu_buf["name"], input=parsed_input
                )
            tbuf = thinking_buffers.pop(evt.index, None)
            if tbuf is not None:
                return ThinkingBlockComplete(
                    text=tbuf.get("text", ""),
                    signature=tbuf.get("signature") or None,
                )
            return None

        # MessageDeltaEvent: contains stop_reason in .delta + usage tally in .usage
        if isinstance(evt, MessageDeltaEvent):
            # Anthropic emits stop_reason here, not in MessageStopEvent.
            delta = getattr(evt, "delta", None)
            stop_reason = getattr(delta, "stop_reason", None) if delta else None
            usage = getattr(evt, "usage", None)
            # Prefer emitting Usage first; TurnComplete is emitted by MessageStopEvent.
            if usage is not None:
                # Note: Anthropic streams usage progressively; this captures the latest snapshot.
                return Usage(
                    input_tokens=getattr(usage, "input_tokens", 0) or 0,
                    output_tokens=getattr(usage, "output_tokens", 0) or 0,
                )
            if stop_reason:
                return TurnComplete(stop_reason=stop_reason)

        # MessageStopEvent: stream ended; emit a fallback TurnComplete if not yet done
        if isinstance(evt, MessageStopEvent):
            return TurnComplete(stop_reason="end_turn")

        return None
