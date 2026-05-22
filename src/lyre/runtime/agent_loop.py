"""Agent loop.

Q9 (2026-05-17) refactor: instead of one adapter+model string, the loop is
handed a *list* of ModelEntry candidates from the Router plus a factory that
turns each entry into an adapter. Per-turn fallback: if a candidate's
stream_turn raises BEFORE emitting any event, the loop tries the next
candidate. Mid-stream errors surface as turn-level failures (no partial-output
retry in MVP).

Sprint 1 still in scope: tool dispatch, max_turns cap, per-turn message
accumulation. Mid-loop interrupt (blocker mailbox) is Sprint 2.
"""

from __future__ import annotations

import json as _json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

from ..adapter.llm_adapter import (
    ContentDelta,
    LLMAdapter,
    LyreContentBlock,
    LyreMessage,
    LyreToolSpec,
    ThinkingBlockComplete,
    ThinkingDelta,
    ToolUseComplete,
    TurnComplete,
    Usage,
)
from .compact import compact_messages
from .health_tracker import HealthTracker
from .kill_switch import KillSwitch
from .mail_watcher import MailWatcher, format_mail_notice
from .model_registry import ModelEntry
from .tools import ToolContext, ToolError, ToolRegistry
from .transcript import TranscriptWriter

log = structlog.get_logger()


@dataclass
class AgentLoopResult:
    status: str
    text: str
    usage: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str | None = None
    wall_clock_ms: int = 0
    turns: int = 0
    # Which model produced the LAST successful turn (after any fallback).
    model_id: str | None = None
    fallback_events: list[dict[str, Any]] = field(default_factory=list)
    interrupt_events: list[dict[str, Any]] = field(default_factory=list)
    # Largest input_tokens any turn reported during this wakeup. Proxy
    # for "how close to the model's context window did we get".
    context_peak_tokens: int = 0
    # How many times the wakeup auto-compacted its message history.
    compaction_count: int = 0


class AllCandidatesFailedError(RuntimeError):
    """Raised when every model candidate exhausted at least one fallback try."""


# Tools whose presence counts as "the agent did something user-facing this
# wakeup". If the wakeup ends with stop_reason=end_turn and NONE of these
# were called, we suspect the model gathered context and forgot to follow
# through — see the silent-turn nudge logic in AgentLoop.run.
_USER_FACING_TOOLS: frozenset[str] = frozenset(
    {
        "mailbox_send",       # reply / inform sender
        "dispatch_task",      # spawn worker
        "await_subagents",    # wait for spawned work
        "report_progress",    # publish status
        "report_side_effect", # publish Tier-1 notification
        "cancel_scheduled_mail",
        "archive_agent",
        "create_agent",       # creating workers is itself a planning step
        "mark_read",          # explicit "I'm done with this mail"
    }
)


_SILENT_TURN_NUDGE_TEMPLATE = (
    "You gathered context (mailbox_read / read_memory / list_* / "
    "mailbox_get_message / …) and produced a response with no tool "
    "calls. The wakeup will close on the next no-tool response, and "
    "the senders of mail you read haven't heard back yet.\n\n"
    "If you have the answer or a concrete next step, "
    "`mailbox_send` it now. If you need to do more work first "
    "(shell_exec / python_exec / dispatch_task), do that — "
    "`mailbox_send` doesn't yield the wakeup, so the natural flow "
    "is research → reply with result → stop.\n\n"
    "Avoid sending a vague IOU (\"I'll look into X\") with no "
    "follow-up tools after it — that's the ack-and-stop pattern. "
    "If you genuinely need to defer, `dispatch_task` (with a real "
    "task_id) or future-mail yourself (`deliver_in=…`), and tell "
    "the asker so."
)
_MAX_SILENT_TURN_NUDGES = 2  # give the model 2 chances before giving up

# Thrashing cap: if a wakeup compacts this many times, bail to silent_close.
# More than this means the work-in-progress itself produces oversized output
# every turn (e.g. shell_exec emitting megabytes), and the right answer is
# to dispatch_task instead.
_MAX_COMPACTIONS = 3


def _take_view_blocks(result: Any) -> list[LyreContentBlock]:
    """Pop and return any ``_lyre_view_blocks`` carried on a tool
    result dict, translating each entry into a ``LyreContentBlock``.

    Tools that produce multimodal output (mailbox_get_message when a
    mail has attachments) tuck this magic key onto their result. The
    loop drains it so the JSON the model sees stays clean ("here's
    the mail body and metadata") while the actual image/document
    blocks ride alongside on the same user message.

    Returns ``[]`` for anything that isn't a dict or has no view
    blocks — the common case.
    """
    if not isinstance(result, dict):
        return []
    raw = result.pop("_lyre_view_blocks", None)
    if not raw:
        return []
    out: list[LyreContentBlock] = []
    for spec in raw:
        if not isinstance(spec, dict):
            continue
        t = spec.get("type")
        if t in ("image", "document"):
            out.append(LyreContentBlock(
                type=t,
                blob_id=spec.get("blob_id"),
                media_type=spec.get("media_type"),
                filename=spec.get("filename"),
            ))
    return out


def _strip_vision_blocks(messages: list[LyreMessage]) -> list[LyreMessage]:
    """Rewrite image/document blocks into text placeholders.

    Called per-dispatch when the routed model lacks the ``vision``
    capability — degrades gracefully so the model still gets useful
    context ("there was a screenshot attached called shot.png") rather
    than the adapter raising on a block it can't translate. The
    original ``messages`` list is left untouched; we return a new
    list with new ``LyreMessage`` / ``LyreContentBlock`` instances so
    the message store stays canonical and a later turn with a
    vision-capable candidate sees the real image again.
    """
    out: list[LyreMessage] = []
    for msg in messages:
        rewritten = False
        new_blocks: list[LyreContentBlock] = []
        for blk in msg.content:
            if blk.type in ("image", "document"):
                name = blk.filename or (blk.blob_id[:12] if blk.blob_id else "?")
                placeholder = (
                    f"[{blk.type}: {name} — current model lacks vision "
                    f"capability; route to a vision-capable model to see it]"
                )
                new_blocks.append(
                    LyreContentBlock(type="text", text=placeholder)
                )
                rewritten = True
            else:
                new_blocks.append(blk)
        out.append(
            LyreMessage(role=msg.role, content=new_blocks)
            if rewritten else msg
        )
    return out


class AgentLoop:
    """Multi-turn agent loop with tool dispatch + per-turn model fallback."""

    def __init__(
        self,
        candidates: list[ModelEntry],
        adapter_for: Callable[[ModelEntry], LLMAdapter],
        model_name_for: Callable[[ModelEntry], str],
        transcript: TranscriptWriter,
        tool_registry: ToolRegistry | None = None,
        tool_context: ToolContext | None = None,
        allowed_tools: list[str] | None = None,
        max_tokens: int = 4096,
        max_turns: int = 24,
        health: HealthTracker | None = None,
        blocker_watcher: MailWatcher | None = None,
        kill_switch: KillSwitch | None = None,
        compact_threshold: float = 0.7,
        compact_keep_last_k: int = 3,
    ):
        if not candidates:
            raise ValueError("AgentLoop needs at least one model candidate")
        self.candidates = candidates
        self.adapter_for = adapter_for
        self.model_name_for = model_name_for
        self.transcript = transcript
        self.tool_registry = tool_registry
        self.tool_context = tool_context
        self.allowed_tools = allowed_tools or []
        self.max_tokens = max_tokens
        self.max_turns = max_turns
        self.health = health
        self.blocker_watcher = blocker_watcher
        self.kill_switch = kill_switch
        # Auto-compact when a turn's input_tokens >= threshold * context_window.
        # 0.7 leaves room for the next turn's output + tool_results before
        # we'd actually overflow. _MAX_COMPACTIONS caps thrashing — if the
        # wakeup hits this many compacts and still can't fit, we bail.
        self.compact_threshold = compact_threshold
        self.compact_keep_last_k = compact_keep_last_k

    # ------------------------------------------------------------------
    # Multi-turn with tool dispatch
    # ------------------------------------------------------------------

    async def run(
        self,
        system_prompt: str,
        initial_messages: list[LyreMessage],
    ) -> AgentLoopResult:
        """Run turns until end_turn / max_turns / max_tokens."""
        started = time.time()
        messages: list[LyreMessage] = list(initial_messages)
        all_tool_calls: list[dict[str, Any]] = []
        total_usage = {"input_tokens": 0, "output_tokens": 0}
        final_text = ""
        final_stop_reason: str | None = None
        last_model_id: str | None = None
        fallback_events: list[dict[str, Any]] = []
        turn_count = 0
        # Per-wakeup context metrics: max input_tokens any single turn
        # reported (proxy for "biggest context we ever sent"), and how
        # many times we auto-compacted mid-wakeup. Both end up on the
        # Wakeup row for dashboard display.
        context_peak_tokens = 0
        compaction_count = 0

        interrupt_events: list[dict[str, Any]] = []
        # Silent-turn nudge state: whether THIS wakeup has produced any
        # user-facing action (reply / dispatch / await / progress report)
        # AND whether we've already issued a one-shot nudge. Defends
        # against the observed DeepSeek pattern where the model gathers
        # context (mailbox_read / list_*) then produces a final text
        # response with no tool calls — the wakeup looks "completed" but
        # the owner never received a reply.
        made_user_facing_action = False
        silent_turn_nudges_used = 0
        # Track senders of mail this wakeup auto-marked as read. If the
        # wakeup ends without made_user_facing_action AND the nudge budget
        # was exhausted, we send each of them a fallback mail so they
        # don't experience the wakeup as pure silence (see
        # _maybe_emit_silent_close_fallback at the bottom of run()).
        silent_close_askers: set[str] = set()

        tool_specs_for_log = self._tool_specs()
        self.transcript.write_system(
            system_prompt=system_prompt,
            tool_names=[t.name for t in tool_specs_for_log],
            allowed_tools=list(self.allowed_tools),
        )

        for turn_idx in range(self.max_turns):
            turn_count = turn_idx + 1

            # Turn-boundary interrupt: if blockers arrived between turns,
            # inject a user-role notice BEFORE the next LLM call.
            if (
                self.blocker_watcher is not None
                and self.blocker_watcher.signal.is_set()
            ):
                self._inject_blocker_notice(messages, interrupt_events, where="pre_turn")

            (
                text_parts,
                tool_uses_this_turn,
                stop_reason,
                turn_usage,
                used_model_id,
                interrupted_mid_stream,
                thinking_blocks_this_turn,
            ) = await self._run_one_turn_with_fallback(
                messages=messages,
                system_prompt=system_prompt,
                fallback_events=fallback_events,
            )

            all_tool_calls.extend(tool_uses_this_turn)
            final_text = "".join(text_parts)
            final_stop_reason = stop_reason
            total_usage["input_tokens"] += turn_usage[0] or 0
            total_usage["output_tokens"] += turn_usage[1] or 0
            last_model_id = used_model_id
            # Running max of per-turn input_tokens — this IS the running
            # context size since each API call resends the full history.
            if turn_usage and turn_usage[0]:
                context_peak_tokens = max(context_peak_tokens, turn_usage[0])

            log.info(
                "agent_turn",
                turn=turn_count,
                model=used_model_id,
                text_chars=len(final_text),
                tool_calls=len(tool_uses_this_turn),
                stop_reason=stop_reason,
                interrupted=interrupted_mid_stream,
            )
            self.transcript.write_turn_end(
                turn_idx=turn_count,
                stop_reason=stop_reason,
                text_len=len("".join(text_parts)),
                tool_count=len(tool_uses_this_turn),
                model_id=used_model_id,
            )

            # Mid-stream interrupt: persist whatever the model emitted as an
            # assistant turn, then inject the blocker notice as the next user
            # message, then continue the loop. Tool uses (if any) are still
            # honored: we let the LLM decide on the next turn whether to
            # finish them or abandon them, after seeing the interrupt notice.
            if interrupted_mid_stream:
                self._append_assistant_message(
                    messages, final_text, tool_uses_this_turn,
                    thinking_blocks=thinking_blocks_this_turn,
                )
                if tool_uses_this_turn:
                    # Drain the tools we received before the interrupt so the
                    # model's history stays consistent (assistant tool_use must
                    # always be followed by user tool_result).
                    tool_result_blocks: list[LyreContentBlock] = []
                    for tu in tool_uses_this_turn:
                        result, is_error = await self._dispatch_tool(
                            tu["name"], tu["id"], tu["input"]
                        )
                        view_blocks = _take_view_blocks(result)
                        tool_result_blocks.append(
                            LyreContentBlock(
                                type="tool_result",
                                tool_use_id=tu["id"],
                                tool_result=result,
                                is_error=is_error,
                            )
                        )
                        tool_result_blocks.extend(view_blocks)
                        self.transcript.write_tool_result(tu["id"], result, is_error)
                        if tu["name"] == "mailbox_read" and not is_error:
                            silent_close_askers.update(
                                _askers_from_mailbox_read(result)
                            )
                    messages.append(
                        LyreMessage(role="user", content=tool_result_blocks)
                    )
                if self.blocker_watcher is not None and self.blocker_watcher.signal.is_set():
                    self._inject_blocker_notice(
                        messages, interrupt_events, where="mid_stream"
                    )
                continue

            if not tool_uses_this_turn or stop_reason in ("end_turn", "max_tokens"):
                if not tool_uses_this_turn:
                    # Before exiting: if mail arrived during this final turn,
                    # don't drop it on the floor — surface as the next turn's
                    # initial user message and continue. (This is the
                    # "high-urgency mail mid-turn" case: agent already
                    # decided to end_turn, but new mail just landed.)
                    if (
                        self.blocker_watcher is not None
                        and self.blocker_watcher.signal.is_set()
                    ):
                        self._append_assistant_message(
                            messages, final_text, [],
                            thinking_blocks=thinking_blocks_this_turn,
                        )
                        self._inject_blocker_notice(
                            messages, interrupt_events,
                            where="post_turn_before_break",
                        )
                        continue
                    # Silent-turn nudge: only fires when this wakeup has
                    # called tools that were all info-gathering (no
                    # mailbox_send, no dispatch, etc.). Plain text-only
                    # responses are not nudged — chat is a legit action.
                    if (
                        stop_reason == "end_turn"
                        and all_tool_calls
                        and not made_user_facing_action
                        and silent_turn_nudges_used < _MAX_SILENT_TURN_NUDGES
                    ):
                        self._append_assistant_message(
                            messages, final_text, [],
                            thinking_blocks=thinking_blocks_this_turn,
                        )
                        messages.append(
                            LyreMessage(
                                role="user",
                                content=[
                                    LyreContentBlock(
                                        type="text",
                                        text=_SILENT_TURN_NUDGE_TEMPLATE,
                                    )
                                ],
                            )
                        )
                        silent_turn_nudges_used += 1
                        self.transcript.note(
                            f"silent_turn_nudge_injected "
                            f"({silent_turn_nudges_used}/{_MAX_SILENT_TURN_NUDGES})"
                        )
                        continue
                    break

            # Build assistant message. Thinking blocks MUST come first
            # (provider invariant); otherwise the next API call gets
            # 400 'content[].thinking must be passed back'.
            assistant_blocks: list[LyreContentBlock] = list(
                thinking_blocks_this_turn
            )
            if final_text:
                assistant_blocks.append(LyreContentBlock(type="text", text=final_text))
            for tu in tool_uses_this_turn:
                assistant_blocks.append(
                    LyreContentBlock(
                        type="tool_use",
                        tool_use_id=tu["id"],
                        tool_name=tu["name"],
                        tool_input=tu["input"],
                    )
                )
            messages.append(LyreMessage(role="assistant", content=assistant_blocks))

            # Execute tools and feed results back.
            tool_result_blocks = []
            for tu in tool_uses_this_turn:
                result, is_error = await self._dispatch_tool(
                    tu["name"], tu["id"], tu["input"]
                )
                # Tools that produce multimodal output (today only
                # mailbox_get_message with attachments) tuck a
                # `_lyre_view_blocks` list onto the result dict — the
                # loop extracts those, appends them as their own
                # LyreContentBlock entries on the same user message,
                # and strips the magic key from what gets shown to
                # the model so the JSON tool_result stays clean.
                view_blocks = _take_view_blocks(result)
                tool_result_blocks.append(
                    LyreContentBlock(
                        type="tool_result",
                        tool_use_id=tu["id"],
                        tool_result=result,
                        is_error=is_error,
                    )
                )
                tool_result_blocks.extend(view_blocks)
                self.transcript.write_tool_result(tu["id"], result, is_error)
                if tu["name"] == "mailbox_read" and not is_error:
                    silent_close_askers.update(
                        _askers_from_mailbox_read(result)
                    )
                # Track whether this wakeup ever ATTEMPTED a user-facing
                # action. We use attempt (not success) because a model
                # that tried mailbox_send and got an error already saw it
                # and can retry — that's not the silent-turn failure
                # pattern we're guarding against.
                if tu["name"] in _USER_FACING_TOOLS:
                    made_user_facing_action = True
                # Kill point 2 / "mid_action_after_tool": fires right after a
                # successful (or errored) tool dispatch. Lets chaos tests
                # simulate process death partway through real work.
                if self.kill_switch is not None:
                    self.kill_switch.check("mid_action_after_tool")
            messages.append(LyreMessage(role="user", content=tool_result_blocks))

            # After executing tool calls we ALWAYS give the model another
            # turn to react to tool_results, regardless of stop_reason.
            # Rationale: with DeepSeek-V4 (and occasionally Anthropic),
            # the provider emits `stop_reason="end_turn"` ALONGSIDE
            # tool_use blocks — that's metadata, NOT a control signal.
            # Canonical Anthropic agentic loop says: continue until the
            # model emits a response with NO tool_uses. Breaking here on
            # end_turn was the structural bug behind every "ack and stop"
            # silent failure — the model called mailbox_send, got the
            # "reminder: this doesn't end the wakeup" tool_result, but
            # the loop exited before that result was ever sent back.
            #
            # The real exit point is up at line ~331 (no tool_uses this
            # turn → silent-turn nudge if applicable, else break).
            # max_turns is the safety cap on a runaway tool loop.
            if (
                stop_reason == "end_turn"
                and all_tool_calls
                and not made_user_facing_action
                and silent_turn_nudges_used < _MAX_SILENT_TURN_NUDGES
            ):
                # Model gathered context (info tools only), no reply
                # sent. Inject a nudge alongside the tool_results so the
                # next response is forced to act.
                messages.append(
                    LyreMessage(
                        role="user",
                        content=[
                            LyreContentBlock(
                                type="text",
                                text=_SILENT_TURN_NUDGE_TEMPLATE,
                            )
                        ],
                    )
                )
                silent_turn_nudges_used += 1
                self.transcript.note(
                    f"silent_turn_nudge_injected "
                    f"({silent_turn_nudges_used}/{_MAX_SILENT_TURN_NUDGES})"
                )

            # ----------------------------------------------------------
            # Auto-compact: if THIS turn's input_tokens crossed the
            # configured threshold of the active model's context window,
            # rewrite `messages` in place (preserving last K turn pairs +
            # all owner / peer mail verbatim, summarizing the work in
            # between via one same-model LLM call). See runtime/compact.py
            # for the algorithm.
            # ----------------------------------------------------------
            ctx_window = self._context_window_for(used_model_id)
            if (
                ctx_window
                and turn_usage
                and turn_usage[0]  # input_tokens reported
                and turn_usage[0] >= self.compact_threshold * ctx_window
            ):
                if compaction_count >= _MAX_COMPACTIONS:
                    # Thrashing: we've compacted N times and the model
                    # still produces oversized output every turn. Bail
                    # to silent-close with an apology mail so the
                    # asker isn't left hanging. The work-up-to-now is
                    # in the transcript for the operator.
                    self.transcript.note(
                        f"compaction_thrashed: count={compaction_count}, "
                        f"turn_input={turn_usage[0]}, ctx={ctx_window}"
                    )
                    log.warning(
                        "compaction_thrashed",
                        compaction_count=compaction_count,
                        turn_input_tokens=turn_usage[0],
                        context_window=ctx_window,
                    )
                    # Force-exit with a special stop_reason. Reuses the
                    # silent_close fallback path below for the apology
                    # email.
                    final_stop_reason = "end_turn"
                    # Mark as silent so silent_close detection fires
                    # (treats this wakeup as "context blew up before
                    # the agent could finish replying").
                    made_user_facing_action = False
                    silent_turn_nudges_used = _MAX_SILENT_TURN_NUDGES
                    break

                # Find the candidate we just successfully ran on, so
                # the summary call uses the same provider / model the
                # wakeup is using. Falls back to the first candidate.
                cand = next(
                    (c for c in self.candidates if c.id == used_model_id),
                    self.candidates[0],
                )
                adapter_for_compact = self.adapter_for(cand)
                model_for_compact = self.model_name_for(cand)
                wakeup_id_hint = (
                    self.tool_context.wakeup_id
                    if self.tool_context is not None else None
                )
                pre_compact_len = len(messages)
                try:
                    messages = await compact_messages(
                        messages,
                        adapter=adapter_for_compact,
                        model=model_for_compact,
                        keep_last_k=self.compact_keep_last_k,
                        wakeup_id=wakeup_id_hint,
                    )
                except Exception as exc:  # noqa: BLE001 — non-fatal
                    log.warning(
                        "compaction_failed",
                        error=str(exc),
                        type=type(exc).__name__,
                    )
                    self.transcript.note(
                        f"compaction_failed: {type(exc).__name__}: {exc}"
                    )
                else:
                    compaction_count += 1
                    self.transcript.note(
                        f"compacted: count={compaction_count}, "
                        f"turn_input={turn_usage[0]}, ctx={ctx_window}, "
                        f"messages: {pre_compact_len} → {len(messages)}"
                    )
                    log.info(
                        "compacted",
                        compaction_count=compaction_count,
                        turn_input_tokens=turn_usage[0],
                        context_window=ctx_window,
                        pre_messages=pre_compact_len,
                        post_messages=len(messages),
                    )

            # Always continue — let the model see tool_results and decide
            # whether to keep working or emit a final no-tool response.

        wall_ms = int((time.time() - started) * 1000)

        # Silent-close detection: wakeup ended after exhausting the nudge
        # budget without ever calling a user-facing tool. The model
        # gathered context but never replied. Auto-send a fallback mail
        # to each asker so the wakeup isn't experienced as pure silence.
        silent_close = (
            final_stop_reason == "end_turn"
            and silent_turn_nudges_used >= _MAX_SILENT_TURN_NUDGES
            and not made_user_facing_action
            and bool(all_tool_calls)
        )
        if silent_close:
            await self._emit_silent_close_fallback(
                askers=silent_close_askers,
                tool_calls=all_tool_calls,
                final_text=final_text,
            )

        result_status = (
            "silent_close"
            if silent_close
            else "completed"
            if final_stop_reason == "end_turn"
            else "needs_continuation"
        )
        log.info(
            "agent_run_complete",
            turns=turn_count,
            tool_calls_total=len(all_tool_calls),
            stop_reason=final_stop_reason,
            wall_ms=wall_ms,
            status=result_status,
            model_id=last_model_id,
            fallbacks=len(fallback_events),
            interrupts=len(interrupt_events),
        )
        return AgentLoopResult(
            status=result_status,
            text=final_text,
            usage=total_usage,
            tool_calls=all_tool_calls,
            stop_reason=final_stop_reason,
            wall_clock_ms=wall_ms,
            turns=turn_count,
            model_id=last_model_id,
            fallback_events=fallback_events,
            interrupt_events=interrupt_events,
            context_peak_tokens=context_peak_tokens,
            compaction_count=compaction_count,
        )

    # ------------------------------------------------------------------
    # Blocker interrupt helpers
    # ------------------------------------------------------------------

    def _inject_blocker_notice(
        self,
        messages: list[LyreMessage],
        interrupt_events: list[dict[str, Any]],
        where: str,
    ) -> None:
        """Pull pending high+/blocker mail, append a user-role notice to
        the conversation, and clear the signal."""
        assert self.blocker_watcher is not None  # caller guarantees
        new_mail = self.blocker_watcher.acknowledge()
        notice = format_mail_notice(new_mail)
        messages.append(
            LyreMessage(
                role="user",
                content=[LyreContentBlock(type="text", text=notice)],
            )
        )
        interrupt_events.append(
            {
                "where": where,
                "blocker_ids": [m.id for m in new_mail],
                "count": len(new_mail),
                "urgencies": [m.urgency for m in new_mail],
            }
        )
        self.transcript.note(
            f"interrupt_injected ({where}): {len(new_mail)} message(s)"
        )

    def _append_assistant_message(
        self,
        messages: list[LyreMessage],
        text: str,
        tool_uses: list[dict[str, Any]],
        thinking_blocks: list[LyreContentBlock] | None = None,
    ) -> None:
        assistant_blocks: list[LyreContentBlock] = []
        # Per Anthropic / DeepSeek-compat convention, thinking blocks
        # must come BEFORE text + tool_use blocks. Providers reject the
        # next-turn request if the prior assistant message dropped its
        # thinking — see error 'content[].thinking must be passed back'.
        if thinking_blocks:
            assistant_blocks.extend(thinking_blocks)
        if text:
            assistant_blocks.append(LyreContentBlock(type="text", text=text))
        for tu in tool_uses:
            assistant_blocks.append(
                LyreContentBlock(
                    type="tool_use",
                    tool_use_id=tu["id"],
                    tool_name=tu["name"],
                    tool_input=tu["input"],
                )
            )
        if assistant_blocks:
            messages.append(LyreMessage(role="assistant", content=assistant_blocks))

    # ------------------------------------------------------------------
    # Per-turn execution with model fallback
    # ------------------------------------------------------------------

    async def _run_one_turn_with_fallback(
        self,
        messages: list[LyreMessage],
        system_prompt: str,
        fallback_events: list[dict[str, Any]],
    ) -> tuple[
        list[str],
        list[dict[str, Any]],
        str | None,
        tuple[int, int],
        str,
        bool,
        list[LyreContentBlock],
    ]:
        """Try each candidate in order. Return collected events for the first
        one that successfully starts streaming. If a candidate raises before
        yielding anything, record a fallback event and try the next.

        Returns:
          (text_parts, tool_uses, stop_reason, usage, model_id,
           interrupted_mid_stream, thinking_blocks)
        """
        tool_specs = self._tool_specs()
        last_exc: Exception | None = None

        for candidate in self.candidates:
            if self.health and not self.health.is_available(candidate.id):
                fallback_events.append(
                    {"model_id": candidate.id, "reason": "circuit_open"}
                )
                self.transcript.note(
                    f"model_skip: {candidate.id} circuit open"
                )
                continue

            # Adapter construction can fail (env var unset, malformed
            # endpoint, etc.). Skip to the next candidate instead of
            # tearing the whole task down — the router's reachability
            # filter usually catches these earlier, but this is the
            # last line of defense if e.g. an env var was unset
            # between router select and this attempt.
            try:
                adapter = self.adapter_for(candidate)
            except Exception as exc:  # noqa: BLE001
                fallback_events.append(
                    {"model_id": candidate.id, "reason": f"adapter_factory: {exc}"}
                )
                self.transcript.note(
                    f"model_skip: {candidate.id} adapter_factory failed ({exc})"
                )
                continue
            model_name = self.model_name_for(candidate)
            text_parts: list[str] = []
            tool_uses: list[dict[str, Any]] = []
            thinking_blocks: list[LyreContentBlock] = []
            stop_reason: str | None = None
            turn_input = 0
            turn_output = 0
            yielded_any = False
            interrupted_mid_stream = False

            # Multimodal degrade-gracefully: if the chosen candidate
            # lacks the `vision` capability but the message list
            # contains image/document blocks, rewrite those blocks
            # into text placeholders so the model still gets a useful
            # signal ("[image: shot.png — current model lacks vision
            # capability]") instead of the adapter raising on dispatch.
            # The router doesn't pre-filter on `needs_vision` because
            # images typically arrive mid-wakeup (mail tool result),
            # well after candidate selection.
            dispatch_messages = (
                messages
                if "vision" in candidate.capabilities
                else _strip_vision_blocks(messages)
            )

            try:
                stream = adapter.stream_turn(
                    messages=dispatch_messages,
                    tools=tool_specs,
                    model=model_name,
                    max_tokens=self.max_tokens,
                    system=system_prompt,
                )
                async for evt in stream:
                    yielded_any = True
                    if isinstance(evt, ContentDelta):
                        text_parts.append(evt.text)
                        self.transcript.write_delta(evt.text)
                    elif isinstance(evt, ThinkingDelta):
                        # Streamed to transcript only — the assembled
                        # block is captured via ThinkingBlockComplete
                        # below for replay into the assistant message.
                        self.transcript.write_thinking_delta(evt.text)
                    elif isinstance(evt, ThinkingBlockComplete):
                        # The full reasoning block. MUST be echoed back
                        # in the next API call (Anthropic + DeepSeek
                        # both require this, with empty signature
                        # tolerated only by DeepSeek). Stash for
                        # _append_assistant_message.
                        thinking_blocks.append(
                            LyreContentBlock(
                                type="thinking",
                                text=evt.text,
                                signature=evt.signature,
                            )
                        )
                    elif isinstance(evt, ToolUseComplete):
                        tu = {"id": evt.id, "name": evt.name, "input": evt.input}
                        tool_uses.append(tu)
                        self.transcript.write_tool_use(evt.id, evt.name, evt.input)
                    elif isinstance(evt, Usage):
                        turn_input = evt.input_tokens
                        turn_output = evt.output_tokens
                    elif isinstance(evt, TurnComplete):
                        stop_reason = evt.stop_reason
                    # Mid-stream interrupt is reserved for urgency=blocker
                    # ("system is waiting"). high-urgency mail also signals
                    # the watcher, but it should NOT yank the agent off
                    # mid-thought — wait for the turn boundary instead.
                    if (
                        self.blocker_watcher is not None
                        and self.blocker_watcher.signal.is_set()
                        and self.blocker_watcher.has_blocker_pending
                    ):
                        interrupted_mid_stream = True
                        self.transcript.note(
                            f"interrupt: blocker signal raised mid-stream on {candidate.id}"
                        )
                        break
            except Exception as exc:  # noqa: BLE001
                if self.health:
                    self.health.mark_failure(candidate.id)
                if not yielded_any:
                    fallback_events.append(
                        {
                            "model_id": candidate.id,
                            "reason": "pre_stream_error",
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    self.transcript.note(
                        f"model_fallback: {candidate.id} failed pre-stream "
                        f"({type(exc).__name__}); trying next candidate"
                    )
                    last_exc = exc
                    continue
                log.error(
                    "agent_turn_midstream_error",
                    model=candidate.id,
                    error=str(exc),
                )
                raise

            if self.health:
                self.health.mark_success(candidate.id)
            return (
                text_parts,
                tool_uses,
                stop_reason,
                (turn_input, turn_output),
                candidate.id,
                interrupted_mid_stream,
                thinking_blocks,
            )

        msg = (
            f"All {len(self.candidates)} model candidates failed before "
            f"emitting any output. Last error: {last_exc!r}"
        )
        raise AllCandidatesFailedError(msg)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _tool_specs(self) -> list[LyreToolSpec]:
        if not self.tool_registry or not self.allowed_tools:
            return []
        return self.tool_registry.specs_for(self.allowed_tools)

    def _context_window_for(self, model_id: str | None) -> int | None:
        """Returns the active model's context_window in tokens, or None
        if the registry didn't declare it. Used by the compact trigger."""
        if not model_id:
            return None
        for c in self.candidates:
            if c.id == model_id:
                return c.context_window
        return None

    async def _dispatch_tool(
        self, name: str, tool_use_id: str, tool_input: dict[str, Any]
    ) -> tuple[str, bool]:
        if not self.tool_registry or not self.tool_context:
            return ("Tool dispatch not configured for this agent loop.", True)
        if name not in self.allowed_tools:
            return (
                f"Tool '{name}' is not in this persona's allowlist: {self.allowed_tools}.",
                True,
            )
        tool = self.tool_registry.get(name)
        if tool is None:
            return (f"Unknown tool '{name}'.", True)
        try:
            args = dict(tool_input)
            args.setdefault("_tool_use_id", tool_use_id)
            result = await tool.handler(self.tool_context, args)
        except ToolError as exc:
            return (str(exc), True)
        except Exception as exc:  # noqa: BLE001
            log.exception("tool_dispatch_unhandled", tool=name, error=str(exc))
            return (
                f"Internal error executing tool '{name}': {exc.__class__.__name__}: {exc}",
                True,
            )
        if isinstance(result, str):
            return (result, False)
        try:
            return (_json.dumps(result, ensure_ascii=False, default=str), False)
        except Exception:
            return (str(result), False)

    async def _emit_silent_close_fallback(
        self,
        askers: set[str],
        tool_calls: list[dict[str, Any]],
        final_text: str,
    ) -> None:
        """Auto-send a fallback mail when this wakeup runs out of nudge
        budget without composing a reply. Each asker gets one message
        explaining the wakeup couldn't form a reply, plus a summary of
        what was attempted so the operator can diagnose.

        This is harness-y by design — the alternative is the asker
        sitting in silence forever, which is the user-reported bug we
        are fixing. The fallback mail itself is honest about being
        system-generated.
        """
        from ..persistence.models import OutboxRow

        if not askers or self.tool_context is None:
            return
        ctx = self.tool_context
        self_id = ctx.self_mailbox
        clean_askers = {a for a in askers if a and a != self_id}
        if not clean_askers:
            return

        tool_names = [tc.get("name", "?") for tc in tool_calls]
        # Compact summary: count per tool name preserves info without
        # exploding the body if the model spammed mailbox_read 30 times.
        counts: dict[str, int] = {}
        for name in tool_names:
            counts[name] = counts.get(name, 0) + 1
        tool_summary = ", ".join(
            f"{n}×{c}" if c > 1 else n for n, c in sorted(counts.items())
        )

        body_lines = [
            "⚠ [Lyre silent-close fallback — system-generated]",
            "",
            f"This wakeup ({self_id}) read your mail but exhausted the "
            f"silent-turn nudge budget without composing a reply.",
            "",
            f"Tools called this wakeup ({len(tool_calls)} total): {tool_summary}",
        ]
        if final_text:
            snippet = final_text.strip()
            if len(snippet) > 800:
                snippet = snippet[:800] + "…"
            body_lines += [
                "",
                "Last assistant text (NOT delivered to anyone, shown here "
                "for debug):",
                snippet,
            ]
        body_lines += [
            "",
            "Please re-ask if you still need an answer. Operator: check "
            f"the wakeup transcript (id={ctx.wakeup_id}) for root cause.",
        ]
        body = "\n".join(body_lines)
        title = f"[silent-close] {self_id} couldn't compose a reply"

        rows: list[OutboxRow] = []
        for asker in clean_askers:
            ext = f"silent-close:{ctx.wakeup_id}:{asker}"
            payload: dict[str, Any] = {
                "recipient": asker,
                "sender": self_id,
                "urgency": "high",
                "title": title,
                "body": body,
                "task_id": ctx.task_id,
                "external_id": ext,
                "parent_msg_id": None,
                "broadcast_id": None,
                "recipients_all": None,
                "metadata": {
                    "silent_close": True,
                    "tool_call_count": len(tool_calls),
                    "wakeup_id": ctx.wakeup_id,
                },
            }
            rows.append(
                OutboxRow(
                    task_id=ctx.task_id,
                    wakeup_id=ctx.wakeup_id,
                    kind="mailbox_send",
                    payload=payload,
                    external_id=ext,
                )
            )
        await ctx.repos.outbox.enqueue(rows)
        self.transcript.note(
            f"silent_close_fallback_sent to={sorted(clean_askers)}"
        )
        log.warning(
            "silent_close_fallback",
            wakeup_id=ctx.wakeup_id,
            agent_id=self_id,
            askers=sorted(clean_askers),
            tool_call_count=len(tool_calls),
        )


def _askers_from_mailbox_read(result_json: str) -> set[str]:
    """Parse a mailbox_read tool result JSON and return the set of
    senders whose messages were just auto-marked as read. Returns empty
    set on parse failure / non-inbox box / no auto-mark.

    This is the read side of the silent-close fallback: we remember
    who's waiting on the agent so that if the wakeup never replies, we
    can deliver an apology to them.
    """
    try:
        data = _json.loads(result_json)
    except (ValueError, TypeError):
        return set()
    if not isinstance(data, dict):
        return set()
    if data.get("box", "inbox") != "inbox":
        return set()
    if not data.get("auto_marked_read"):
        return set()
    out: set[str] = set()
    for m in data.get("messages") or []:
        if isinstance(m, dict):
            sender = m.get("sender")
            if isinstance(sender, str) and sender:
                out.add(sender)
    return out
