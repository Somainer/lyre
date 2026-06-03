"""OpenAIResponsesAdapter — LLMAdapter for OpenAI's `/v1/responses` shape.

Targets:
  - OpenAI's newer Responses API (POST `/v1/responses`)
  - Internal / corporate proxies that implement the Responses API
    surface (e.g. bytedance's ai-coder gateway)

What's different from the Chat Completions adapter:

  Request shape
    Chat Completions:  {model, messages: [...], max_tokens, stream, tools}
    Responses:         {model, input: [...], instructions, max_output_tokens,
                        stream, tools, tool_choice}

  System prompt
    Chat Completions:  message with role="system" at the head of messages
    Responses:         separate `instructions` string param

  Tool calls in assistant output
    Chat Completions:  message has `tool_calls: [{id, function:{name, arguments}}]`
    Responses:         output item with `type="function_call"`, fields
                       `{call_id, name, arguments}` (JSON string)

  Tool results from caller
    Chat Completions:  message with role="tool" + tool_call_id
    Responses:         input item with `type="function_call_output"`,
                       fields `{call_id, output}`

  Streaming events
    Chat Completions:  one event type (`chat.completion.chunk`) with
                       delta.content / delta.tool_calls / finish_reason
    Responses:         many event types — `response.created`,
                       `response.output_item.added`,
                       `response.output_text.delta`,
                       `response.function_call_arguments.delta`,
                       `response.reasoning_text.delta`,
                       `response.completed`, etc.

What is NOT implemented (yet):
  - Audio / image / code-interpreter / file-search / MCP-call streaming
    events. The Responses API supports many tool types Lyre doesn't
    expose; events for those are silently skipped.
  - Background mode / conversation persistence (`previous_response_id`).
  - Computed reasoning summaries (the more verbose
    `response.reasoning_summary_text.*` track).
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
    StopReason,
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


class OpenAIResponsesAdapter:
    """Wraps AsyncOpenAI's `client.responses.create(...)` with Lyre's
    standardized streaming interface."""

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
        # Headers ride alongside whatever the SDK builds from api_key.
        # For Responses-API proxies that use custom auth schemes, this
        # is the actual auth path; `api_key` may be a placeholder.
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
        input_items = self._lyre_to_responses_input(
            messages, blob_store=self._blob_store,
        )
        responses_tools = (
            [self._tool_to_responses(t) for t in tools] if tools else None
        )

        kwargs: dict[str, Any] = {
            "model": model,
            "input": input_items,
            "max_output_tokens": max_tokens,
            "stream": True,
        }
        if system:
            kwargs["instructions"] = system
        if responses_tools:
            kwargs["tools"] = responses_tools
        if temperature is not None:
            kwargs["temperature"] = temperature

        # Per-tool-call buffer, keyed by item_id (the Responses API
        # gives a stable id per output item we can use to thread the
        # function_call_arguments.delta stream back to its name).
        tool_buffers: dict[str, dict[str, Any]] = {}
        # Currently-open ToolUseStart ids — we close them in
        # function_call_arguments.done with the parsed args.
        emitted_tool_starts: set[str] = set()
        reasoning_chunks: list[str] = []
        last_finish: StopReason | None = None
        usage_payload: tuple[int, int] | None = None

        # Enter the SDK stream's own async context manager so a
        # break/GeneratorExit out of the iteration (e.g. the agent loop
        # bailing on a blocker interrupt) deterministically closes the
        # underlying HTTP response rather than leaking it until GC.
        # Matches anthropic.py's `async with self.client.messages.stream`.
        async with await self.client.responses.create(**kwargs) as stream:
            async for evt in stream:
                etype = getattr(evt, "type", None)
                if etype is None:
                    continue

                # ----- text streaming ---------------------------------------
                if etype == "response.output_text.delta":
                    delta = getattr(evt, "delta", None) or ""
                    if delta:
                        yield ContentDelta(text=delta)
                    continue

                # Some SDK versions name this slightly differently; tolerate.
                if etype == "response.text.delta":
                    delta = getattr(evt, "delta", None) or ""
                    if delta:
                        yield ContentDelta(text=delta)
                    continue

                # ----- reasoning / thinking content -------------------------
                if etype in (
                    "response.reasoning_text.delta",
                    "response.reasoning.delta",
                ):
                    delta = getattr(evt, "delta", None) or ""
                    if delta:
                        reasoning_chunks.append(delta)
                        yield ThinkingDelta(text=delta)
                    continue

                # ----- tool-call streaming ----------------------------------
                if etype == "response.output_item.added":
                    item = getattr(evt, "item", None)
                    if item is None:
                        continue
                    item_type = getattr(item, "type", None)
                    if item_type == "function_call":
                        item_id = getattr(item, "id", None) or ""
                        call_id = getattr(item, "call_id", None) or item_id
                        name = getattr(item, "name", None) or ""
                        tool_buffers[item_id] = {
                            "call_id": call_id,
                            "name": name,
                            "args_chunks": [],
                        }
                        yield ToolUseStart(id=call_id, name=name)
                        emitted_tool_starts.add(call_id)
                    continue

                if etype == "response.function_call_arguments.delta":
                    item_id = getattr(evt, "item_id", None) or ""
                    delta = getattr(evt, "delta", None) or ""
                    buf = tool_buffers.get(item_id)
                    if buf is not None and delta:
                        buf["args_chunks"].append(delta)
                        yield ToolUseDelta(
                            id=buf["call_id"], input_partial=delta,
                        )
                    continue

                if etype == "response.function_call_arguments.done":
                    item_id = getattr(evt, "item_id", None) or ""
                    buf = tool_buffers.pop(item_id, None)
                    if buf is None:
                        continue
                    raw = (
                        getattr(evt, "arguments", None)
                        or "".join(buf["args_chunks"])
                    )
                    try:
                        parsed = _json.loads(raw) if raw else {}
                    except _json.JSONDecodeError:
                        parsed = {"_raw": raw}
                    if not isinstance(parsed, dict):
                        parsed = {"_raw": raw}
                    yield ToolUseComplete(
                        id=buf["call_id"],
                        name=buf["name"],
                        input=parsed,
                    )
                    last_finish = "tool_use"
                    continue

                # ----- terminal events --------------------------------------
                if etype == "response.completed":
                    response = getattr(evt, "response", None)
                    usage = getattr(response, "usage", None) if response else None
                    if usage is not None:
                        # Responses API uses `input_tokens` / `output_tokens`.
                        in_t = getattr(usage, "input_tokens", 0) or 0
                        out_t = getattr(usage, "output_tokens", 0) or 0
                        usage_payload = (int(in_t), int(out_t))
                    # If we didn't see a tool_use along the way, this was a
                    # plain end-of-turn text response.
                    if last_finish is None:
                        last_finish = "end_turn"
                    continue

                if etype == "response.incomplete":
                    response = getattr(evt, "response", None)
                    # Reason can be `max_output_tokens` or `content_filter`.
                    reason = None
                    if response is not None:
                        incomplete = getattr(response, "incomplete_details", None)
                        if incomplete is not None:
                            reason = getattr(incomplete, "reason", None)
                    last_finish = (
                        "max_tokens" if reason == "max_output_tokens" else "error"
                    )
                    continue

                if etype == "response.failed":
                    last_finish = "error"
                    continue

                # Everything else (e.g. response.created, response.in_progress,
                # response.output_item.done for messages, audio events, etc.)
                # — no Lyre event to emit. Drop silently.

        # Flush reasoning as a single ThinkingBlockComplete (the
        # transcript / dashboard care about the full block, not the
        # individual chunks). Skip if nothing was reasoned.
        if reasoning_chunks:
            yield ThinkingBlockComplete(
                text="".join(reasoning_chunks),
                signature=None,
            )

        if usage_payload:
            yield Usage(
                input_tokens=usage_payload[0],
                output_tokens=usage_payload[1],
            )

        yield TurnComplete(stop_reason=last_finish or "end_turn")

    # ------------------------------------------------------------------
    # Conversion: Lyre → Responses API
    # ------------------------------------------------------------------

    @staticmethod
    def _tool_to_responses(t: LyreToolSpec) -> dict[str, Any]:
        """Responses API tools are flat — `{type:"function", name, ...}`
        — no nested `function` object like Chat Completions used to."""
        return {
            "type": "function",
            "name": t.name,
            "description": t.description,
            "parameters": t.input_schema,
        }

    @staticmethod
    def _lyre_to_responses_input(
        msgs: list[LyreMessage],
        blob_store: BlobStore | None = None,
    ) -> list[dict[str, Any]]:
        """Convert Lyre's `(role, list[block])` messages into the
        Responses-API `input` array.

        Mapping rules:

          * `system` messages → dropped here; pass via `instructions`
            on the request (the caller sets that separately).
          * assistant `text` blocks  → input items
                {type:"message", role:"assistant", content:[{type:"output_text", text}]}
          * assistant `tool_use` blocks  → input items
                {type:"function_call", call_id, name,
                 arguments: json-stringified input}
          * assistant `thinking` blocks → dropped (the upstream model
            re-derives reasoning each turn; we don't echo it back).
          * user `text` blocks → input items
                {type:"message", role:"user", content:[{type:"input_text", text}]}
          * user `image` blocks → content parts on the user message
                {type:"input_image", image_url:"data:<media>;base64,..."}
          * user `tool_result` blocks → input items
                {type:"function_call_output", call_id, output}
        """
        out: list[dict[str, Any]] = []

        for m in msgs:
            if m.role == "system":
                # System prompt rides on `instructions`, not `input`.
                continue

            if m.role == "user":
                # Split: tool_results become function_call_output items;
                # text + image become a single user message.
                text_parts: list[str] = []
                image_parts: list[dict[str, Any]] = []
                for blk in m.content:
                    if blk.type == "text" and blk.text:
                        text_parts.append(blk.text)
                    elif blk.type == "tool_result":
                        content = blk.tool_result
                        if not isinstance(content, str):
                            try:
                                content = _json.dumps(
                                    content, ensure_ascii=False, default=str,
                                )
                            except (TypeError, ValueError):
                                content = str(content)
                        out.append({
                            "type": "function_call_output",
                            "call_id": blk.tool_use_id or "",
                            "output": content,
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
                            "type": "input_image",
                            "image_url": (
                                f"data:{blk.media_type};base64,{b64}"
                            ),
                        })
                    elif blk.type == "document":
                        raise ValueError(
                            "OpenAI Responses adapter does not yet "
                            "translate 'document' blocks; route PDFs "
                            "to Anthropic or pre-extract text."
                        )
                if text_parts or image_parts:
                    content_parts: list[dict[str, Any]] = []
                    if text_parts:
                        content_parts.append({
                            "type": "input_text",
                            "text": "\n".join(text_parts),
                        })
                    content_parts.extend(image_parts)
                    out.append({
                        "type": "message",
                        "role": "user",
                        "content": content_parts,
                    })
                continue

            if m.role == "assistant":
                text_parts = []
                tool_uses: list[dict[str, Any]] = []
                for blk in m.content:
                    if blk.type == "text" and blk.text:
                        text_parts.append(blk.text)
                    elif blk.type == "tool_use":
                        args = blk.tool_input or {}
                        try:
                            arg_str = _json.dumps(args, ensure_ascii=False)
                        except (TypeError, ValueError):
                            arg_str = "{}"
                        tool_uses.append({
                            "type": "function_call",
                            "call_id": blk.tool_use_id or "",
                            "name": blk.tool_name or "",
                            "arguments": arg_str,
                        })
                    # thinking blocks: drop — not echoed back upstream.
                if text_parts:
                    out.append({
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": "\n".join(text_parts)},
                        ],
                    })
                out.extend(tool_uses)
                continue

            # Unknown role — best-effort: emit as a user message.
            text_parts = [
                blk.text for blk in m.content
                if blk.type == "text" and blk.text
            ]
            if text_parts:
                out.append({
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "\n".join(text_parts)},
                    ],
                })

        return out
