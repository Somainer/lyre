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
    # Coarse outcome string, kept for back-compat with logging /
    # legacy callers. The DETAILED declaration fields below are
    # authoritative for the scheduler-side persistence path.
    #
    # Possible values:
    #   "completed"          — agent declared status='done'
    #   "yielded"            — agent declared status='in_progress'
    #   "awaiting"           — agent declared status='awaiting'
    #   "failed"             — agent declared status='failed' (any reason)
    #   "silent_close"       — runtime forced fallback because the
    #                          agent never declared, even after nudge
    #   "needs_continuation" — loop hit max_turns / max_tokens before
    #                          any declaration
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
    # End-of-wakeup declaration metadata, captured from the agent's
    # terminal end_wakeup(...) call (or synthesised by the runtime on
    # silent-close fallback). See WAKEUP_END_CONTRACT.md.
    declared_status: str | None = None
    declared_summary: str | None = None
    declared_awaiting_on: str | None = None
    declared_awaiting_ref: str | None = None
    declared_failure_reason: str | None = None
    declared_recoverable: bool | None = None


class AllCandidatesFailedError(RuntimeError):
    """Raised when every model candidate exhausted at least one fallback try."""


def _coarse_status_from_declaration(
    declaration: dict[str, Any],
    final_stop_reason: str | None,
) -> str:
    """Map the end-of-wakeup declaration to the coarse legacy
    ``AgentLoopResult.status`` string.

    The scheduler reads the structured ``declared_*`` fields for
    persistence; this string is mainly for logging / back-compat with
    tests that inspect ``result.status``.
    """
    status = declaration["status"]
    if status == "done":
        return "completed"
    if status == "in_progress":
        return "yielded"
    if status == "awaiting":
        return "awaiting"
    if status == "failed":
        reason = declaration.get("failure_reason")
        if reason == "silent_close":
            return "silent_close"
        if reason == "loop_exhausted" and final_stop_reason != "end_turn":
            return "needs_continuation"
        return "failed"
    return "failed"


# Tools whose presence counts as "the agent did something user-facing this
# wakeup". If the wakeup ends with stop_reason=end_turn and NONE of these
# were called, we suspect the model gathered context and forgot to follow
# through — see the silent-turn nudge logic in AgentLoop.run.
_USER_FACING_TOOLS: frozenset[str] = frozenset(
    {
        "mailbox_send",       # reply / inform sender
        "mailbox_react",      # silent ack — closes a thread without push
        "dispatch_task",      # spawn worker
        "report_side_effect", # publish Tier-1 notification
        "cancel_scheduled_mail",
        "archive_agent",
        "create_agent",       # creating workers is itself a planning step
        "mark_read",          # explicit "I'm done with this mail"
    }
)


_END_WAKEUP_NUDGE_TEMPLATE = (
    "Your last response had no `end_wakeup` call. The wakeup cannot "
    "terminate cleanly without one — without an explicit declaration "
    "the runtime cannot tell whether your work succeeded, is waiting "
    "on something, or failed.\n\n"
    "Call `end_wakeup` now with the status that best describes your "
    "situation:\n"
    "  - status='done' if the task goal is met (even if 'met' means "
    "    'read mail, nothing to do' — that's a valid done).\n"
    "  - status='awaiting' (with awaiting_on='mail' / 'subtask' / "
    "    'time' / 'human_decision') if you're blocked on an event.\n"
    "  - status='in_progress' if you yielded mid-task and want "
    "    another wakeup to resume.\n"
    "  - status='failed' (with failure_reason) if you can't proceed.\n"
    "\n"
    "If you legitimately need to send a reply or do one more thing "
    "BEFORE declaring, do that AND end with end_wakeup in the same "
    "turn. Don't fall into the ack-and-stop pattern (\"I'll look "
    "into X\") — that becomes a forced silent_close, which surfaces "
    "as an alert."
)

# Thrashing cap: if a wakeup compacts this many times, bail to silent_close.
# More than this means the work-in-progress itself produces oversized output
# every turn (e.g. shell_exec emitting megabytes), and the right answer is
# to dispatch_task instead.
_MAX_COMPACTIONS = 3


def _check_phantom_delegation(
    tool_calls: list[dict[str, Any]],
    outcomes: list[bool],
) -> None:
    """Warn when a wakeup mailed the owner without successfully
    dispatching anything in the same wakeup.

    The failure mode (see docs/design/) is: model attempts
    ``create_agent`` / ``dispatch_task``, those fail (bad model id,
    missing persona, etc.), but the model still composes a
    ``mailbox_send(to="owner", body="I'll dispatch the worker...")``
    and ends the wakeup. The owner sees a promise the database has
    no record of.

    We can't reliably tell from the body whether the mail CLAIMS a
    delegation (paraphrases are infinite), so this is observability
    only — a structured warning log surfaces the pattern for
    retrospective review (`lyre wakeups list` / dashboard). The
    prompt-level guard in dispatcher.md is the actual prevention.
    """
    if len(outcomes) != len(tool_calls):
        return  # defensive: shouldn't happen, but don't crash on it

    owner_sends = 0
    successful_dispatches = 0
    for tu, is_error in zip(tool_calls, outcomes, strict=True):
        name = tu.get("name")
        if name == "mailbox_send" and not is_error:
            to = tu.get("input", {}).get("to")
            recipients = to if isinstance(to, list) else [to]
            if "owner" in recipients:
                owner_sends += 1
        elif name == "dispatch_task" and not is_error:
            successful_dispatches += 1

    if owner_sends > 0 and successful_dispatches == 0:
        # Did the wakeup at least TRY to delegate? If yes, the
        # claim-vs-reality gap is more likely a phantom; if no, the
        # mail is probably a legit ack / status update that
        # legitimately doesn't involve any dispatch.
        attempted_delegation = any(
            tu.get("name") in ("dispatch_task", "create_agent")
            for tu in tool_calls
        )
        if attempted_delegation:
            log.warning(
                "phantom_delegation_suspected",
                owner_mail_sends=owner_sends,
                dispatch_attempts=sum(
                    1 for tu in tool_calls
                    if tu.get("name") == "dispatch_task"
                ),
                create_agent_attempts=sum(
                    1 for tu in tool_calls
                    if tu.get("name") == "create_agent"
                ),
                successful_dispatches=successful_dispatches,
            )


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
        max_tokens: int = 32768,
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
        # Parallel to all_tool_calls: True if the dispatch returned
        # is_error. Used by the phantom-delegation observability log
        # at wakeup end (see below) — we need to know which calls
        # actually succeeded, not just which were attempted. Order
        # matches all_tool_calls 1:1.
        tool_outcomes: list[bool] = []
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
        # End-of-wakeup declaration state. ``end_wakeup_called`` flips
        # to True the instant the agent's ``end_wakeup(...)`` tool call
        # is dispatched successfully; the ToolContext carries the
        # captured args via ``end_wakeup_declaration``. Once True, the
        # loop drops any trailing tool calls in the same turn and
        # breaks out — no more LLM calls.
        # ``end_wakeup_nudge_used`` ensures we nudge for a declaration
        # at most once per wakeup. See WAKEUP_END_CONTRACT.md §6b.
        end_wakeup_called = False
        end_wakeup_nudge_used = False
        # ``made_user_facing_action`` is kept for one purpose only: if
        # the runtime ends up force-declaring silent_close (no
        # end_wakeup even after nudge), and the agent never did
        # anything visible to mail senders, fire the silent_close
        # apology mail so askers aren't left in the dark. See
        # ``_emit_silent_close_fallback``.
        made_user_facing_action = False
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
                        tool_outcomes.append(is_error)
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
                    # End-of-wakeup nudge: if the agent ended a turn
                    # with no tool_uses AND no end_wakeup declaration,
                    # inject one nudge requesting an explicit terminal
                    # call. After the nudge, if the agent still doesn't
                    # declare, the post-loop fallback synthesises
                    # failed/silent_close (see WAKEUP_END_CONTRACT.md
                    # §6b). Only one nudge per wakeup — the loop exit
                    # path drops through to the fallback otherwise.
                    #
                    # Skip when no tool_context is wired up — that's
                    # the permissive low-level mechanics path where the
                    # contract has nowhere to land.
                    if (
                        self.tool_context is not None
                        and stop_reason == "end_turn"
                        and not end_wakeup_called
                        and not end_wakeup_nudge_used
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
                                        text=_END_WAKEUP_NUDGE_TEMPLATE,
                                    )
                                ],
                            )
                        )
                        end_wakeup_nudge_used = True
                        self.transcript.note("end_wakeup_nudge_injected")
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

            # Execute tools and feed results back. If end_wakeup fires
            # mid-list, any subsequent tool_uses in this turn are
            # *dropped* with a synthetic error result — the wakeup is
            # terminating, those calls would run on borrowed time and
            # might mutate state the agent didn't intend post-declaration.
            tool_result_blocks = []
            for tu in tool_uses_this_turn:
                if end_wakeup_called:
                    # Trailing tool call after the terminal declaration.
                    # Append a synthetic error tool_result (every
                    # tool_use must have a matching tool_result, even
                    # if we never dispatched) and log a warning so the
                    # operator can see the contract violation.
                    drop_msg = (
                        "dropped: end_wakeup already declared this "
                        "turn; further tool calls are ignored"
                    )
                    tool_result_blocks.append(
                        LyreContentBlock(
                            type="tool_result",
                            tool_use_id=tu["id"],
                            tool_result={"error": drop_msg},
                            is_error=True,
                        )
                    )
                    tool_outcomes.append(True)
                    self.transcript.note(
                        f"wakeup_post_end_tool_calls_ignored: {tu['name']}"
                    )
                    log.warning(
                        "wakeup_post_end_tool_calls_ignored",
                        tool=tu["name"],
                        tool_use_id=tu["id"],
                    )
                    continue
                result, is_error = await self._dispatch_tool(
                    tu["name"], tu["id"], tu["input"]
                )
                tool_outcomes.append(is_error)
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
                # action. Drives the silent_close apology decision: if
                # the runtime ends up force-declaring silent_close AND
                # the agent never did anything visible to the askers,
                # we send a fallback apology so they aren't left in
                # the dark.
                if tu["name"] in _USER_FACING_TOOLS:
                    made_user_facing_action = True
                # Capture the end_wakeup declaration as soon as it
                # fires — the next iteration of this for-loop will
                # short-circuit any trailing tool calls, and the
                # outer for-turn loop will break on end_wakeup_called.
                if (
                    tu["name"] == "end_wakeup"
                    and not is_error
                    and self.tool_context is not None
                    and self.tool_context.end_wakeup_declaration is not None
                ):
                    end_wakeup_called = True
                # Kill point 2 / "mid_action_after_tool": fires right after a
                # successful (or errored) tool dispatch. Lets chaos tests
                # simulate process death partway through real work.
                if self.kill_switch is not None:
                    self.kill_switch.check("mid_action_after_tool")
            messages.append(LyreMessage(role="user", content=tool_result_blocks))

            # If end_wakeup just fired this turn, the wakeup is over —
            # any tool_use blocks after end_wakeup in the same turn were
            # already dropped during dispatch with synthetic error
            # results, the tool_results message is in place, and we
            # break out of the for-turn loop. No further LLM call.
            if end_wakeup_called:
                break

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
            # max_turns is the safety cap on a runaway tool loop.

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
                    # Force-exit with a special stop_reason. The post-loop
                    # silent-close fallback then synthesises the
                    # failed/silent_close declaration since no
                    # end_wakeup call landed.
                    final_stop_reason = "end_turn"
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

        # Resolve the end-of-wakeup declaration.
        #
        # Four paths land here:
        #   1. Agent declared via end_wakeup(...) — declaration is on
        #      the ToolContext, take it as authoritative.
        #   2. Loop exited via end_turn-without-tools, but no
        #      declaration even after the nudge — synthesise a
        #      failed/silent_close declaration so downstream
        #      persistence is uniform.
        #   3. Loop hit max_turns (final_stop_reason != end_turn) —
        #      synthesise failed/loop_exhausted with recoverable=True
        #      (transient wedge, the next dispatch might succeed).
        #   4. Loop was built WITHOUT a tool_context (low-level unit
        #      tests of stream / fallback / interrupt mechanics where
        #      no tool dispatch is ever wired up). The contract has
        #      nowhere to land, so synthesise a "done" declaration
        #      based on whether the loop exited cleanly — this keeps
        #      the loop testable in isolation without forcing every
        #      mechanics test to thread through a real registry.
        declaration: dict[str, Any] | None = (
            self.tool_context.end_wakeup_declaration
            if self.tool_context is not None else None
        )
        forced_silent_close = False
        permissive_no_context = self.tool_context is None
        if declaration is None and permissive_no_context:
            # Path 4: no tool_context → treat clean end_turn as
            # "completed", exhausted loops as failed/loop_exhausted.
            if final_stop_reason == "end_turn":
                declaration = {
                    "status": "done",
                    "summary": "(test) loop ran without tool_context",
                    "awaiting_on": None,
                    "awaiting_ref": None,
                    "failure_reason": None,
                    "recoverable": None,
                }
            else:
                declaration = {
                    "status": "failed",
                    "summary": "(test) loop exhausted turns without tool_context",
                    "awaiting_on": None,
                    "awaiting_ref": None,
                    "failure_reason": "loop_exhausted",
                    "recoverable": True,
                }
        elif declaration is None:
            if final_stop_reason == "end_turn":
                # Path 2: silent close — agent never declared.
                forced_silent_close = True
                declaration = {
                    "status": "failed",
                    "summary": (
                        "(auto) wakeup ended without declaring an "
                        "outcome via end_wakeup. The runtime force-"
                        "recorded silent_close."
                    ),
                    "awaiting_on": None,
                    "awaiting_ref": None,
                    "failure_reason": "silent_close",
                    "recoverable": False,
                }
                self.transcript.note("wakeup_silent_close_forced")
                log.warning(
                    "wakeup_silent_close_forced",
                    turns=turn_count,
                    tool_call_count=len(all_tool_calls),
                )
            else:
                # Path 3: ran out of turns / tokens before declaration.
                declaration = {
                    "status": "failed",
                    "summary": (
                        "(auto) wakeup exhausted its turn budget "
                        "without declaring an outcome."
                    ),
                    "awaiting_on": None,
                    "awaiting_ref": None,
                    "failure_reason": "loop_exhausted",
                    "recoverable": True,
                }
                self.transcript.note("wakeup_loop_exhausted_forced")

        # Silent-close apology mail: only when we force-declared
        # silent_close AND there are mail senders waiting AND the
        # agent never did anything visible. The apology IS the
        # agent's reply, so we only send it when the agent didn't
        # produce one of its own.
        if (
            forced_silent_close
            and not made_user_facing_action
            and bool(all_tool_calls)
        ):
            await self._emit_silent_close_fallback(
                askers=silent_close_askers,
                tool_calls=all_tool_calls,
                final_text=final_text,
            )

        # Coarse legacy status string. Authoritative info is in the
        # declared_* fields below.
        result_status = _coarse_status_from_declaration(
            declaration, final_stop_reason,
        )

        # Phantom-delegation observability: if this wakeup sent any
        # mail to the owner BUT had no successful dispatch_task /
        # create_agent, the body very likely claims work that never
        # happened (see the docs/design failure report). We can't
        # reliably parse the body to confirm, so this is a warning
        # log only — not a hard block. Surfaces in `lyre wakeups list`
        # / dashboard for retrospective review.
        _check_phantom_delegation(all_tool_calls, tool_outcomes)

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
            declared_status=declaration["status"],
            declared_summary=declaration["summary"],
            declared_awaiting_on=declaration["awaiting_on"],
            declared_awaiting_ref=declaration["awaiting_ref"],
            declared_failure_reason=declaration["failure_reason"],
            declared_recoverable=declaration["recoverable"],
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
        if not self.tool_registry:
            return []
        # ``end_wakeup`` is part of the runtime contract — every wakeup
        # must declare termination via it, so it's always advertised to
        # the model regardless of the persona's allowlist.
        # WAKEUP_END_CONTRACT.md §6a.
        names = list(self.allowed_tools) if self.allowed_tools else []
        if "end_wakeup" not in names:
            names.append("end_wakeup")
        return self.tool_registry.specs_for(names)

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
        # end_wakeup is part of the runtime contract — always callable
        # regardless of the persona allowlist. Everything else has to
        # be in the list.
        if name != "end_wakeup" and name not in self.allowed_tools:
            return (
                f"Tool '{name}' is not in this persona's allowlist: {self.allowed_tools}.",
                True,
            )
        tool = self.tool_registry.get(name)
        if tool is None:
            return (f"Unknown tool '{name}'.", True)
        # Adapters that couldn't parse the model's tool-call arguments
        # JSON (e.g. truncated by max_tokens mid-emit) fall back to
        # ``{"_raw": <partial-json-string>}``. The per-tool handler then
        # sees a payload missing every required key and returns the
        # generic "provide 'code'" / "provide 'to'" error — the model
        # then re-tries the same malformed call, burns turns, and the
        # task dies at max_turns. Surfacing the truncation directly
        # lets the model break out of that loop on the next turn.
        if (
            len(tool_input) == 1
            and "_raw" in tool_input
            and isinstance(tool_input["_raw"], str)
        ):
            raw = tool_input["_raw"]
            return (
                f"Tool '{name}' was called with malformed arguments — "
                f"the JSON could not be parsed and was probably "
                f"truncated by the per-turn output budget "
                f"(max_tokens={self.max_tokens}). "
                f"Do NOT retry the same call. Either shrink the "
                f"arguments (split a large input across multiple "
                f"calls, omit verbose inline content, paste-link "
                f"instead of inlining), or skip this tool for now and "
                f"continue the task differently. "
                f"Raw bytes received ({len(raw)} chars): {raw[:200]!r}…",
                True,
            )
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
