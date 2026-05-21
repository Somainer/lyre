"""OpenAIAdapter — LLMAdapter for OpenAI's `/v1/chat/completions` shape.

Targets:
  - OpenAI proper (gpt-4o / o-series / gpt-5)
  - DeepSeek's OpenAI-compat endpoint (`api.deepseek.com/v1`)
  - OpenRouter, Together, vLLM-served endpoints, any other OAI-compat host

Differences from Anthropic that this adapter normalizes:
  - System prompt is a `role="system"` message at the head of `messages`,
    not a separate API parameter.
  - Tool calls are `assistant.tool_calls[]` (id + function.{name, arguments}
    where arguments is a JSON STRING the model emits piecewise during
    streaming).
  - Tool results are separate messages with `role="tool"` + `tool_call_id`,
    NOT content blocks inside a user message.
  - Streaming uses `delta.content` for text and `delta.tool_calls[i].
    function.arguments` for incremental tool-input JSON.
  - Reasoning content from reasoning-model endpoints (DeepSeek's
    `deepseek-reasoner`, some OpenRouter routes) is exposed as
    `delta.reasoning_content` (non-standard but widespread). We surface
    it as a Lyre `ThinkingDelta`/`ThinkingBlockComplete` pair so the
    transcript + dashboard's 🧠 bubble work uniformly across providers.

Things NOT supported (and not needed for OAI compat):
  - Signed thinking blocks (Anthropic-only — OAI providers don't sign).
  - `cache_control` breakpoints (OAI prompt caching is automatic).
"""

from __future__ import annotations

import base64
import json as _json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from openai import AsyncOpenAI

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

# OpenAI finish_reason → Lyre stop_reason. OAI doesn't have a "cancelled"
# variant; "content_filter" is the closest to error-y.
_FINISH_REASON_MAP: dict[str, str] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "error",
}


class OpenAIAdapter:
    """Wraps AsyncOpenAI with Lyre's standardized streaming interface."""

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
        # Custom headers ride alongside the SDK's Bearer header. Used
        # when a proxy/gateway in front of the model expects a custom
        # auth scheme (signed JWT, internal SSO token, mTLS-passthrough
        # token, etc.).
        if extra_headers:
            kwargs["default_headers"] = extra_headers
        self.client = AsyncOpenAI(**kwargs)
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
        oai_messages = self._lyre_to_openai_messages(
            messages, system, blob_store=self._blob_store,
        )
        oai_tools = [self._tool_to_openai(t) for t in tools] if tools else None

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": oai_messages,
            "max_tokens": max_tokens,
            "stream": True,
            # Ask for usage stats on the final chunk (OAI's standard
            # behavior is to omit usage in streaming unless this is set).
            "stream_options": {"include_usage": True},
        }
        if oai_tools:
            kwargs["tools"] = oai_tools
        if temperature is not None:
            kwargs["temperature"] = temperature

        # Per-tool-call buffers, keyed by chunk-delta `index` (OAI
        # streams tool args piecewise; the index identifies which call
        # the chunk belongs to when multiple are in flight).
        tool_buffers: dict[int, dict[str, Any]] = {}
        # Aggregated reasoning text across the stream, paired with a
        # ThinkingBlockComplete emit at end so AgentLoop / transcript /
        # dashboard see thinking the same way they do for Anthropic.
        reasoning_chunks: list[str] = []
        # Track stop_reason + usage to emit at end (OpenAI puts these
        # on the FINAL chunk separately).
        last_finish: str | None = None
        usage_payload: tuple[int, int] | None = None

        stream = await self.client.chat.completions.create(**kwargs)
        async for chunk in stream:
            # Usage sometimes arrives in its own chunk with no choices
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                usage_payload = (
                    getattr(usage, "prompt_tokens", 0) or 0,
                    getattr(usage, "completion_tokens", 0) or 0,
                )

            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            choice = choices[0]
            delta = getattr(choice, "delta", None)
            finish_reason = getattr(choice, "finish_reason", None)

            if delta is not None:
                # Plain text content
                text = getattr(delta, "content", None)
                if text:
                    yield ContentDelta(text=text)

                # Reasoning content (DeepSeek-Reasoner + some OAI-compat
                # routes). The attribute is non-standard so we fetch
                # defensively. We accumulate + emit deltas inline so
                # the dashboard streams thinking in real time.
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    reasoning_chunks.append(reasoning)
                    yield ThinkingDelta(text=reasoning)

                # Tool calls — partial. Each delta.tool_calls[i] carries
                # an `index`, sometimes an `id`, sometimes a `function.name`
                # (usually only in the first delta of that call), and
                # `function.arguments` as a string fragment that builds
                # up across deltas. When we see the FIRST piece of args
                # for a buffer we emit ToolUseStart; on every later
                # piece we emit ToolUseDelta.
                tool_calls = getattr(delta, "tool_calls", None) or []
                for tc in tool_calls:
                    idx = getattr(tc, "index", 0) or 0
                    buf = tool_buffers.setdefault(idx, {
                        "id": None, "name": None, "args": "", "started": False,
                    })
                    if getattr(tc, "id", None):
                        buf["id"] = tc.id
                    fn = getattr(tc, "function", None)
                    if fn is not None:
                        if getattr(fn, "name", None):
                            buf["name"] = fn.name
                        args = getattr(fn, "arguments", None)
                        if args is not None:
                            # Emit Start once we have both id+name (or
                            # at least name); some providers send id
                            # later than name.
                            if not buf["started"] and buf["name"]:
                                yield ToolUseStart(
                                    id=buf["id"] or "", name=buf["name"]
                                )
                                buf["started"] = True
                            buf["args"] += args
                            yield ToolUseDelta(
                                id=buf["id"] or "", input_partial=args
                            )

            if finish_reason:
                last_finish = finish_reason

        # Stream done. Flush tool buffers → ToolUseComplete. Then emit
        # the aggregated thinking block, usage, and TurnComplete in the
        # canonical Anthropic-compatible order.
        for _idx, buf in tool_buffers.items():
            if not buf["name"]:
                continue
            try:
                parsed = (
                    _json.loads(buf["args"]) if buf["args"] else {}
                )
            except _json.JSONDecodeError:
                parsed = {"_raw": buf["args"]}
            yield ToolUseComplete(
                id=buf["id"] or "",
                name=buf["name"] or "",
                input=parsed if isinstance(parsed, dict) else {"_raw": buf["args"]},
            )

        if reasoning_chunks:
            yield ThinkingBlockComplete(
                text="".join(reasoning_chunks),
                signature=None,   # OAI-compat doesn't sign thinking
            )

        if usage_payload:
            yield Usage(
                input_tokens=usage_payload[0],
                output_tokens=usage_payload[1],
            )

        mapped = _FINISH_REASON_MAP.get(last_finish or "stop", "end_turn")
        yield TurnComplete(stop_reason=mapped)

    # ----------------------------------------------------------------
    # Conversion: Lyre -> OpenAI
    # ----------------------------------------------------------------

    @staticmethod
    def _tool_to_openai(t: LyreToolSpec) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
            },
        }

    @staticmethod
    def _lyre_to_openai_messages(
        msgs: list[LyreMessage],
        system: str | None,
        blob_store: BlobStore | None = None,
    ) -> list[dict[str, Any]]:
        """Flatten Lyre's `(role, list[block])` messages into the
        sequence OpenAI expects. Key transformations:

          - assistant message with tool_use blocks → assistant message
            with `tool_calls` field (and `content=None` if no text)
          - assistant message with thinking blocks → DROP the thinking
            (OAI-compat providers don't echo it; if we send back a
            `reasoning_content` field, most upstreams ignore or error)
          - user message with tool_result blocks → ONE `role="tool"`
            message per result (NOT a content block on the user msg)
          - user message with mixed text + tool_result → emit the tool
            messages FIRST, then a user text message, since OpenAI
            requires tool messages to immediately follow the assistant
            that called them.
          - user message with image/document blocks → user msg whose
            `content` is a LIST `[{type:text,text:...}, {type:image_url,
            image_url:{url:"data:<media>;base64,..."}}, ...]`. When
            there are NO image blocks we keep the legacy string form
            (some compat providers reject the list form for plain text).
        """
        out: list[dict[str, Any]] = []
        if system:
            out.append({"role": "system", "content": system})

        for m in msgs:
            if m.role == "system":
                # Already handled via the separate `system` param.
                continue

            if m.role == "user":
                # Split: tool_results become their own role="tool" msgs,
                # text + image become a user msg.
                tool_msgs: list[dict[str, Any]] = []
                text_parts: list[str] = []
                image_parts: list[dict[str, Any]] = []
                for blk in m.content:
                    if blk.type == "text":
                        if blk.text:
                            text_parts.append(blk.text)
                    elif blk.type == "tool_result":
                        content = blk.tool_result
                        if not isinstance(content, str):
                            try:
                                content = _json.dumps(content, ensure_ascii=False, default=str)
                            except (TypeError, ValueError):
                                content = str(content)
                        tool_msgs.append({
                            "role": "tool",
                            "tool_call_id": blk.tool_use_id or "",
                            "content": content,
                        })
                    elif blk.type == "image":
                        if blob_store is None or not blk.blob_id or not blk.media_type:
                            raise ValueError(
                                "Cannot translate 'image' block: "
                                "blob_store + blob_id + media_type required"
                            )
                        data = blob_store.read(blk.blob_id, blk.media_type)
                        b64 = base64.b64encode(data).decode("ascii")
                        image_parts.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{blk.media_type};base64,{b64}",
                            },
                        })
                    # Chat Completions has no first-class document/PDF
                    # input type — provider feature gap. The router
                    # gates on `vision` only, not on `documents`; if a
                    # doc block reaches here we drop it loudly so it
                    # doesn't silently swallow user intent.
                    elif blk.type == "document":
                        raise ValueError(
                            "OpenAI Chat Completions does not support "
                            "'document' blocks; route to a provider "
                            "with PDF input or pre-extract text."
                        )
                # tool results must immediately follow the preceding
                # assistant; emit them first.
                out.extend(tool_msgs)
                if image_parts:
                    # Multimodal user message: content MUST be a list.
                    content_list: list[dict[str, Any]] = []
                    if text_parts:
                        content_list.append({
                            "type": "text", "text": "".join(text_parts),
                        })
                    content_list.extend(image_parts)
                    out.append({"role": "user", "content": content_list})
                elif text_parts:
                    out.append({
                        "role": "user", "content": "".join(text_parts),
                    })
                continue

            if m.role == "assistant":
                text_parts = []
                tool_calls: list[dict[str, Any]] = []
                for blk in m.content:
                    if blk.type == "text":
                        if blk.text:
                            text_parts.append(blk.text)
                    elif blk.type == "thinking":
                        # OAI-compat providers don't accept a thinking
                        # field back. The transcript still has the
                        # reasoning; we just don't replay it.
                        continue
                    elif blk.type == "tool_use":
                        tool_calls.append({
                            "id": blk.tool_use_id or "",
                            "type": "function",
                            "function": {
                                "name": blk.tool_name or "",
                                "arguments": _json.dumps(
                                    blk.tool_input or {},
                                    ensure_ascii=False, default=str,
                                ),
                            },
                        })
                msg: dict[str, Any] = {"role": "assistant"}
                # OpenAI requires `content` to be string or null. Set
                # to null when there are tool_calls and no text — some
                # endpoints (esp. DeepSeek's compat) reject empty
                # string when tool_calls are present.
                if text_parts:
                    msg["content"] = "".join(text_parts)
                else:
                    msg["content"] = None
                if tool_calls:
                    msg["tool_calls"] = tool_calls
                # Only emit if there's SOMETHING. An empty assistant
                # msg confuses some endpoints.
                if text_parts or tool_calls:
                    out.append(msg)
                continue

            # role="tool" or other — skip; Lyre uses role="user" with
            # tool_result blocks instead, handled above.

        return out
