"""In-wakeup context compaction.

Called by AgentLoop when a turn's `input_tokens` crosses `compact_threshold ×
context_window`. Compacts the message history in place so the next API call
fits comfortably in the model's window.

Strategy (Lyre-specific — see context-compress design discussion):
  - System prompt is passed separately to the adapter; never appears in
    `messages`, never touched here.
  - `messages[0]` is the initial user message (task goal). Always kept.
  - Last K assistant messages + their following tool_result user messages
    are kept INTACT (so thinking blocks pair with tool_use blocks correctly
    for the next API call).
  - Everything in between is "elided" and replaced with:
      1. Chronological synthetic user/assistant messages that capture
         mail in/out — `mailbox_get_message` results become user
         messages, `mailbox_send` calls become assistant messages. This
         preserves owner / peer communication verbatim (in Lyre,
         mailbox tools carry what other systems put in user role).
      2. One synthesized work-summary user message produced by a single
         LLM call to the same model the wakeup is running on. The
         summary describes shell_exec / python_exec / dispatch_task
         outcomes that can't be reconstructed from mail history.

Tools that are dropped from elided range entirely (idempotent /
re-fetchable / pure side-effect ack):
  - mailbox_read (listings — bodies are preserved via mailbox_get_message)
  - list_agents / list_personas / list_models / list_tasks / query_task_status
  - report_progress / report_side_effect / mark_read
  - create_agent / archive_agent (one-liner in work summary if any)
"""

from __future__ import annotations

import json as _json
import re
from collections.abc import AsyncGenerator
from typing import Any, cast

import structlog

from ..adapter.llm_adapter import (
    ContentDelta,
    LLMAdapter,
    LyreContentBlock,
    LyreMessage,
    StreamEvent,
    TurnComplete,
)

log = structlog.get_logger()

# Tool name → policy.
# "preserve_in"   = synthesize a user message from the tool_result body
# "preserve_out"  = synthesize an assistant message from the tool_use body
# "trace"         = include in work-summary trace (with truncated output)
# "drop"          = drop entirely (idempotent / re-fetchable / no info value)
_TOOL_POLICY: dict[str, str] = {
    "mailbox_get_message": "preserve_in",
    "mailbox_send": "preserve_out",
    "mailbox_read": "drop",
    "mark_read": "drop",
    "list_agents": "drop",
    "list_personas": "drop",
    "list_models": "drop",
    "list_tasks": "drop",
    "list_scheduled_mail": "drop",
    "query_task_status": "drop",
    "report_progress": "drop",
    "report_side_effect": "trace",   # operator may care about effects
    "create_agent": "trace",
    "archive_agent": "trace",
    "dispatch_task": "trace",        # task_id MUST be quoted in trace
}
# All other tools (shell_exec, python_exec, read_memory, cancel_scheduled_mail,
# mailbox_get_message error cases, …) fall through to "trace".
_DEFAULT_POLICY = "trace"


def find_pivot(messages: list[LyreMessage], keep_last_k: int) -> int:
    """Index where the last `keep_last_k` assistant messages start.

    Returns 1 (right after the initial user message) if the conversation
    hasn't had enough assistant turns yet to elide anything meaningful.
    """
    if keep_last_k <= 0:
        return len(messages)
    count = 0
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].role == "assistant":
            count += 1
            if count == keep_last_k:
                return i
    return 1


async def compact_messages(
    messages: list[LyreMessage],
    *,
    adapter: LLMAdapter,
    model: str,
    keep_last_k: int = 3,
    wakeup_id: str | None = None,
    max_summary_tokens: int = 600,
) -> list[LyreMessage]:
    """Return a new (shorter) messages list after compaction.

    `messages` is NOT mutated. Caller should swap in the return value.

    The caller is responsible for the compact-threshold decision, peak
    tracking, thrashing detection (e.g. count of consecutive compacts),
    and any silent-close-style fallback. This function just does the
    transformation.
    """
    if len(messages) < 3:
        # Not enough history to meaningfully compact.
        return list(messages)
    pivot = find_pivot(messages, keep_last_k)
    if pivot <= 1:
        # No room to elide anything; bail.
        return list(messages)

    kept_head = list(messages[:1])  # initial user msg (task goal)
    elided = list(messages[1:pivot])
    kept_tail = list(messages[pivot:])

    # Carry forward this function's OWN prior output (synthetic mail + summary
    # seams from an earlier compaction of the same wakeup) VERBATIM. Without
    # this, a second compaction re-elides those messages — and because they
    # carry no tool_use blocks, `_extract_synthetic_history` produces nothing
    # for them, silently destroying the owner/peer mail that the five laws
    # require kept verbatim. Only genuinely-new turns since the last compaction
    # get summarized.
    carried = [m for m in elided if m.compaction_artifact]
    fresh = [m for m in elided if not m.compaction_artifact]

    synthetic, work_trace = _extract_synthetic_history(fresh)
    new_msgs: list[LyreMessage] = carried + synthetic
    # Emit a summary seam when there's fresh tool work to fold, OR on the
    # FIRST compaction (carried empty) where the seam marks the elision even
    # if only mail was elided. A recompaction with no fresh tool work skips
    # the seam — the carried artifacts already represent the elided history,
    # so a fresh empty marker would just accrete on every compaction.
    if work_trace or not carried:
        summary_msg = await _make_work_summary_msg(
            adapter=adapter, model=model, work_trace=work_trace,
            wakeup_id=wakeup_id, max_tokens=max_summary_tokens,
        )
        new_msgs.append(summary_msg)

    return kept_head + new_msgs + kept_tail


def _extract_synthetic_history(
    elided: list[LyreMessage],
) -> tuple[list[LyreMessage], list[str]]:
    """Walk the elided range chronologically. For each assistant tool_use:
      - If policy is "preserve_in", look up its tool_result and emit a
        synthetic user message carrying the mail body.
      - If "preserve_out", emit a synthetic assistant message from the
        tool_use body.
      - If "trace", append a one-line trace entry for the work summary.
      - If "drop", do nothing.

    Returns `(synthetic_messages, work_trace_lines)`.
    """
    # First pass: index tool_results by tool_use_id for lookup.
    results_by_id: dict[str, tuple[Any, bool]] = {}
    for msg in elided:
        for blk in msg.content:
            if blk.type == "tool_result":
                if blk.tool_use_id:
                    results_by_id[blk.tool_use_id] = (
                        blk.tool_result, bool(blk.is_error)
                    )

    synthetic: list[LyreMessage] = []
    work_trace: list[str] = []

    for msg in elided:
        if msg.role != "assistant":
            continue
        for blk in msg.content:
            if blk.type != "tool_use":
                continue
            name = blk.tool_name or ""
            policy = _TOOL_POLICY.get(name, _DEFAULT_POLICY)
            tool_input = blk.tool_input or {}
            result_pair = results_by_id.get(blk.tool_use_id or "", (None, False))

            if policy == "preserve_in":
                msg_obj = _synth_mail_in(tool_input, result_pair)
                if msg_obj is not None:
                    synthetic.append(msg_obj)
                else:
                    # Bad/missing result — fall back to trace.
                    work_trace.append(
                        f"- {name}({_format_args(tool_input)}) → "
                        f"(result missing or unparseable)"
                    )
            elif policy == "preserve_out":
                msg_obj = _synth_mail_out(tool_input)
                if msg_obj is not None:
                    synthetic.append(msg_obj)
            elif policy == "drop":
                continue
            else:  # "trace"
                work_trace.append(
                    _format_trace_line(name, tool_input, result_pair)
                )

    return synthetic, work_trace


def _synth_mail_in(
    tool_input: dict[str, Any], result_pair: tuple[Any, bool],
) -> LyreMessage | None:
    """`mailbox_get_message` → synthetic user message containing the body."""
    result, is_error = result_pair
    if is_error or result is None:
        return None
    parsed = _try_parse_json(result)
    if not parsed or not isinstance(parsed, dict):
        return None
    msg_id = parsed.get("id", tool_input.get("msg_id", "?"))
    sender = parsed.get("sender", "?")
    urgency = parsed.get("urgency", "")
    title = parsed.get("title") or ""
    body = parsed.get("body") or ""
    parent_msg_id = parsed.get("parent_msg_id")
    meta_bits = [f"from {sender}", f"msg #{msg_id}"]
    if urgency:
        meta_bits.append(f"urgency={urgency}")
    if parent_msg_id:
        meta_bits.append(f"reply_to=#{parent_msg_id}")
    if title and title.strip() and title.strip() != body.strip().split("\n", 1)[0].strip():
        meta_bits.append(f"title={title!r}")
    header = f"[Mail {' '.join(meta_bits)}]"
    return LyreMessage(
        role="user",
        content=[LyreContentBlock(type="text", text=f"{header}\n{body}")],
        compaction_artifact=True,
    )


def _synth_mail_out(tool_input: dict[str, Any]) -> LyreMessage | None:
    """`mailbox_send` → synthetic assistant message recording what was sent."""
    body = tool_input.get("body")
    if not isinstance(body, str):
        return None
    to = tool_input.get("to")
    if isinstance(to, list):
        to_str = ", ".join(str(x) for x in to)
    elif isinstance(to, str):
        to_str = to
    else:
        to_str = "?"
    parts = [f"to {to_str}"]
    if tool_input.get("reply_to"):
        parts.append(f"reply_to=#{tool_input['reply_to']}")
    if tool_input.get("urgency") and tool_input["urgency"] != "normal":
        parts.append(f"urgency={tool_input['urgency']}")
    if tool_input.get("deliver_in"):
        parts.append(f"deliver_in={tool_input['deliver_in']}")
    elif tool_input.get("deliver_at"):
        parts.append(f"deliver_at={tool_input['deliver_at']}")
    if tool_input.get("recur_every"):
        parts.append(f"recur_every={tool_input['recur_every']}")
    header = f"[Sent {' '.join(parts)}]"
    return LyreMessage(
        role="assistant",
        content=[LyreContentBlock(type="text", text=f"{header}\n{body}")],
        compaction_artifact=True,
    )


_TASK_ID_RE = re.compile(r'"task_id"\s*:\s*"([^"]+)"')
_TRUNC = 200


def _format_trace_line(
    name: str, tool_input: dict[str, Any], result_pair: tuple[Any, bool],
) -> str:
    """One-line trace entry for the work-summary prompt. Truncates output."""
    args = _format_args(tool_input)
    result, is_error = result_pair
    result_str = "" if result is None else str(result)
    # dispatch_task: extract task_id explicitly so the summary can quote it
    if name == "dispatch_task":
        m = _TASK_ID_RE.search(result_str)
        tid = m.group(1) if m else "(unknown)"
        target = tool_input.get("agent") or tool_input.get("persona") or "?"
        return f"- dispatch_task → task_id={tid} (to {target}, goal={tool_input.get('goal', '')[:80]!r})"
    truncated = result_str[:_TRUNC] + ("…" if len(result_str) > _TRUNC else "")
    err_tag = " [ERROR]" if is_error else ""
    return f"- {name}({args}){err_tag} → {truncated}"


def _format_args(tool_input: dict[str, Any]) -> str:
    """Compact arg preview: first useful key only, truncated."""
    for k in ("argv", "code", "rel_path", "msg_id", "to", "agent", "goal"):
        if k in tool_input:
            v = tool_input[k]
            preview = (
                " ".join(str(x) for x in v[:3])
                if isinstance(v, list)
                else str(v)
            )
            preview = preview[:80] + ("…" if len(preview) > 80 else "")
            return f"{k}={preview!r}"
    return ""


def _try_parse_json(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = _json.loads(value)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


async def _make_work_summary_msg(
    *,
    adapter: LLMAdapter,
    model: str,
    work_trace: list[str],
    wakeup_id: str | None,
    max_tokens: int,
) -> LyreMessage:
    """Call the SAME model the wakeup is on to produce a brief summary
    of the elided work. Mail in/out is NOT in this prompt — it's
    already preserved verbatim as synthetic messages — so the model
    only has to digest tool traces (shell_exec, python_exec, dispatch,
    etc.).
    """
    transcript_ref = (
        f" Full transcript: wakeup={wakeup_id}." if wakeup_id else ""
    )
    if not work_trace:
        # No tool work to summarize; emit a minimal marker so the model
        # still sees the seam.
        body = (
            "[Compact summary of prior turns — no substantive tool work "
            f"to summarize between the mail messages above.{transcript_ref}]"
        )
        return LyreMessage(
            role="user",
            content=[LyreContentBlock(type="text", text=body)],
            compaction_artifact=True,
        )

    prompt = (
        "You are producing a compaction summary for an ongoing agent task.\n\n"
        "Below is the tool trace from elided turns (mail in/out is NOT in "
        "this list — that's already preserved verbatim in the conversation). "
        "Write a concise paragraph (<200 words) capturing:\n"
        "  - key findings or facts learned\n"
        "  - files written / artifacts produced\n"
        "  - task_ids dispatched (quote them VERBATIM)\n"
        "  - open / pending items the agent committed to\n\n"
        "Skip restating tool calls verbatim. Skip narrating chronology. "
        "Output is plain prose — no markdown headers.\n\n"
        "Tool trace:\n" + "\n".join(work_trace)
    )
    summary_text = await _call_for_summary(
        adapter=adapter, model=model, prompt=prompt, max_tokens=max_tokens,
    )
    if not summary_text:
        # Fallback: emit the raw trace inline. Information dense but cheap.
        summary_text = (
            "Tool actions during elided turns:\n" + "\n".join(work_trace[:40])
        )

    body = f"[Compact summary of prior turns.{transcript_ref}]\n\n{summary_text}"
    return LyreMessage(
        role="user",
        content=[LyreContentBlock(type="text", text=body)],
        compaction_artifact=True,
    )


async def _call_for_summary(
    *,
    adapter: LLMAdapter,
    model: str,
    prompt: str,
    max_tokens: int,
) -> str:
    """Single-turn adapter call. No tools, no system prompt (the
    summary prompt is self-contained)."""
    user_msg = LyreMessage(
        role="user",
        content=[LyreContentBlock(type="text", text=prompt)],
    )
    pieces: list[str] = []
    stream: AsyncGenerator[StreamEvent, None] | None = None
    try:
        # The protocol types stream_turn as AsyncIterator, but every concrete
        # adapter implements it as an async generator, so .aclose() is part of
        # the documented contract; cast so the finally below type-checks.
        stream = cast(
            "AsyncGenerator[StreamEvent, None]",
            adapter.stream_turn(
                messages=[user_msg],
                tools=[],
                model=model,
                max_tokens=max_tokens,
                system=None,
            ),
        )
        async for evt in stream:
            if isinstance(evt, ContentDelta):
                pieces.append(evt.text)
            elif isinstance(evt, TurnComplete):
                break
    except Exception as exc:  # noqa: BLE001 — caller decides what to do
        # Returning "" triggers the raw-trace fallback in
        # _make_work_summary_msg, which is correct but invisible: the
        # agent_loop's compaction_failed handler never fires because
        # compact_messages still returns "successfully". Log so a
        # consistently-erroring summary endpoint is diagnosable.
        log.warning(
            "compact_summary_call_failed",
            model=model,
            error=str(exc),
            type=type(exc).__name__,
        )
        return ""
    finally:
        if stream is not None:
            # Eagerly finalize the adapter's async generator. Breaking on
            # TurnComplete leaves it suspended inside the adapter's
            # `async with ...stream(...)` block, holding the HTTP
            # connection open until GC. aclose() runs GeneratorExit
            # through it, exiting the context; no-op if already exhausted.
            # Guard it (like wakeup_summary._call_for_summary) so a close-time
            # error can't mask the return value / "" — which would escape to
            # the agent_loop's compaction_failed path instead of the intended
            # raw-trace fallback.
            try:
                await stream.aclose()
            except Exception:  # noqa: BLE001
                pass
    return "".join(pieces).strip()
