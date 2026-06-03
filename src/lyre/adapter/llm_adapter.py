"""LLMAdapter Protocol + standardized message / event types.

See AGENT_RUNTIME.md §1 for the design rationale (provider-neutral streaming interface).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal, Protocol

# ---------------------------------------------------------------------------
# Message / tool types — Lyre's internal canonical form (MCP-shape)
# ---------------------------------------------------------------------------

@dataclass
class LyreContentBlock:
    # "thinking" blocks carry the model's reasoning. Anthropic /
    # DeepSeek-V4-pro REQUIRE these to be echoed back verbatim in the
    # next API call (along with their signature, if any) — otherwise
    # the API rejects with "content[].thinking must be passed back".
    # The signature is the provider's cryptographic seal on the
    # reasoning; pass-through only, don't synthesize.
    #
    # "image" / "document" blocks reference a blob in the persistence
    # `blobs` table by `blob_id` (sha256 hex). Adapters resolve the
    # bytes via the runtime BlobStore at send-time and translate to
    # the provider's native shape (Anthropic source/base64, OpenAI
    # image_url, Responses input_image). When the routed model has
    # no `vision` capability, the agent_loop replaces these blocks
    # with text placeholders before dispatch — see model_router.
    type: Literal[
        "text", "tool_use", "tool_result", "thinking",
        "image", "document",
    ]
    text: str | None = None  # reused for both text and thinking content
    tool_use_id: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_result: Any = None
    is_error: bool = False
    signature: str | None = None  # only meaningful for type="thinking"
    # Multimodal: identifies a row in `blobs` (sha256 hex) plus the
    # media type (`image/png`, `application/pdf`, …). Bytes live on
    # disk under `${object_store}/blobs/<id>.<ext>`. `filename` is the
    # original upload name when known — preserved for human display
    # in the dashboard mail-detail view; not sent to the model.
    blob_id: str | None = None
    media_type: str | None = None
    filename: str | None = None


@dataclass
class LyreMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: list[LyreContentBlock]
    # Set on messages PRODUCED BY compaction (the synthetic mail-in/out
    # messages + the work-summary seam). Lets `runtime/compact.py` recognize
    # its own prior output on a SECOND compaction and carry it forward
    # verbatim instead of re-eliding it — without this flag a recompaction
    # drops the previously-preserved mail (those synthetic messages have no
    # tool_use blocks to re-synthesize from). Adapters ignore this field
    # entirely; it never reaches the provider.
    compaction_artifact: bool = False


@dataclass
class LyreToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


# ---------------------------------------------------------------------------
# Stream event types — provider-neutral
# ---------------------------------------------------------------------------

class StreamEvent:
    """Base class for stream events. Adapter normalizes provider-specific
    stream events to these subclasses."""


@dataclass
class ContentDelta(StreamEvent):
    text: str


@dataclass
class ThinkingDelta(StreamEvent):
    """Streaming chunk of the model's thinking / reasoning. Surfaced to
    the transcript + dashboard for operator debug. The accumulated text
    is also gathered into the corresponding `ThinkingBlockComplete`
    event for replay to the API on the next turn. Emitted by Anthropic
    extended-thinking models and DeepSeek's Anthropic-compat reasoning
    models."""
    text: str


@dataclass
class ThinkingBlockComplete(StreamEvent):
    """End of a thinking content block. Carries the FULL accumulated
    thinking text and (provider-issued) signature. The agent loop must
    append a `LyreContentBlock(type="thinking", ...)` to the assistant
    message it replays back to the API on the next turn — providers
    reject the request if the thinking block from the prior assistant
    turn isn't echoed back. For DeepSeek the signature may be empty
    (their compat layer doesn't always sign); for Anthropic it's
    required."""
    text: str
    signature: str | None


@dataclass
class ToolUseStart(StreamEvent):
    id: str
    name: str


@dataclass
class ToolUseDelta(StreamEvent):
    id: str
    input_partial: str


@dataclass
class ToolUseComplete(StreamEvent):
    id: str
    name: str
    input: dict[str, Any]


StopReason = Literal[
    "end_turn", "tool_use", "max_tokens", "cancelled", "error"
]


@dataclass
class TurnComplete(StreamEvent):
    stop_reason: StopReason


@dataclass
class Usage(StreamEvent):
    input_tokens: int
    output_tokens: int


@dataclass
class StreamError(StreamEvent):
    error_kind: Literal["api_error", "timeout", "rate_limit", "cancelled"]
    detail: str


# ---------------------------------------------------------------------------
# Adapter Protocol
# ---------------------------------------------------------------------------

class LLMAdapter(Protocol):
    """Provider-neutral streaming LLM interface."""

    async def stream_turn(
        self,
        messages: list[LyreMessage],
        tools: list[LyreToolSpec],
        model: str,
        max_tokens: int = 4096,
        temperature: float | None = None,
        system: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a single conversational turn.

        Yields StreamEvent subclasses in order. Caller can cancel mid-stream
        by calling .aclose() on the returned iterator.
        """
        # Protocol method body — never executed. The bare ``yield`` flags
        # the function as an async generator so the return-type satisfies
        # ``AsyncIterator``. Using a literal ``False`` makes mypy treat
        # the yield as unreachable, which is fine — Protocol methods are
        # never called directly.
        if False:
            yield
        ...
