"""Wakeup-lifecycle and side-effect tools.

  * ``end_wakeup`` — agent's terminal declaration of how this wakeup
    is ending. Must be the LAST tool call. The status drives
    ``tasks.status`` / ``wakeups.end_status`` deterministically; see
    ``docs/design/WAKEUP_END_CONTRACT.md``.

  * ``report_side_effect`` — agent declares an externally visible side
    effect (e.g., 'opened PR'). Goes through outbox as a tier1
    notification; the dispatcher fans it out to subscribers (e.g.,
    owner mailbox).

A previous ``report_progress`` tool lived here too — it persisted a
free-form JSON blob into ``tasks.checkpoint``. The audit in
``docs/design/WAKEUP_END_CONTRACT.md`` §3a found it vestigial
(``update_scratchpad`` covers the same continuity story with better
scope and curation), so it has been removed along with the column.
The terminal-declaration tool ``end_wakeup`` replaces it as the
canonical wakeup-end signal.
"""

from __future__ import annotations

from typing import Any

from ...persistence.models import OutboxRow
from . import Tool, ToolContext, ToolError


async def _report_side_effect(
    ctx: ToolContext, args: dict[str, Any]
) -> dict[str, Any]:
    kind = args.get("kind")
    payload = args.get("payload") or {}
    if not kind or not isinstance(kind, str):
        raise ToolError("kind required (e.g. 'opened_pr', 'pushed_branch')")
    if not isinstance(payload, dict):
        raise ToolError("payload must be an object")

    tool_use_id = args.get("_tool_use_id")
    if not tool_use_id:
        raise ToolError("internal: missing tool_use_id (agent loop bug)")
    external_id = f"{ctx.wakeup_id}:{tool_use_id}:side_effect"

    notification_body = {
        "kind": kind,
        "task_id": ctx.task_id,
        "persona": ctx.persona_name,
        "details": payload,
    }
    await ctx.repos.outbox.enqueue(
        [
            OutboxRow(
                task_id=ctx.task_id,
                wakeup_id=ctx.wakeup_id,
                kind="tier1_notification",
                payload=notification_body,
                external_id=external_id,
            )
        ]
    )
    return {"status": "queued", "external_id": external_id, "kind": kind}


REPORT_SIDE_EFFECT = Tool(
    name="report_side_effect",
    description=(
        "Declare an externally visible side effect (git push, PR open, file delete, etc.) "
        "so Lyre can record it for audit and fan a notification to the owner."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "description": "Side effect class: opened_pr / pushed_branch / sent_email / called_api / etc.",
            },
            "payload": {
                "type": "object",
                "description": "Structured details — e.g. {url, sha, branch}.",
            },
        },
        "required": ["kind"],
    },
    handler=_report_side_effect,
)


# Closed enum, matches the spec (WAKEUP_END_CONTRACT.md §3b). The
# "silent_close" value is reserved for the runtime's hard fallback —
# agents must not pass it themselves; we still validate it through so
# the runtime can drive the handler too.
_END_WAKEUP_STATUSES: frozenset[str] = frozenset(
    {"done", "in_progress", "awaiting", "failed"}
)
_AWAITING_KINDS: frozenset[str] = frozenset(
    {"mail", "subtask", "time", "human_decision"}
)
_FAILURE_REASONS: frozenset[str] = frozenset(
    {
        "loop_exhausted",
        "tool_error",
        "provider_error",
        "precondition_failed",
        "dependency_failed",
        "cancelled_by_owner",
        "cancelled_by_parent",
        "policy_violation",
        "silent_close",
    }
)


async def _end_wakeup(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Validate the declared end-state and stash it on the ToolContext
    for the agent loop to pick up. This handler does NOT itself end
    the wakeup — it captures the agent's declaration; the loop reads
    ``ctx.end_wakeup_declaration`` after dispatch and terminates.
    """
    status = args.get("status")
    summary = args.get("summary")
    if not isinstance(status, str) or status not in _END_WAKEUP_STATUSES:
        raise ToolError(
            f"status must be one of {sorted(_END_WAKEUP_STATUSES)}; got {status!r}"
        )
    if not isinstance(summary, str) or not summary.strip():
        raise ToolError("summary required (non-empty string)")

    awaiting_on = args.get("awaiting_on")
    awaiting_ref = args.get("awaiting_ref")
    failure_reason = args.get("failure_reason")
    recoverable = args.get("recoverable")

    # Status-specific required fields. Catches the common mistake of
    # "status=awaiting" without specifying what we're waiting on (the
    # scheduler would have nothing to gate resume on).
    if status == "awaiting":
        if not isinstance(awaiting_on, str) or awaiting_on not in _AWAITING_KINDS:
            raise ToolError(
                f"awaiting_on required when status='awaiting'; must be one of "
                f"{sorted(_AWAITING_KINDS)}; got {awaiting_on!r}"
            )
        if awaiting_ref is not None and not isinstance(awaiting_ref, str):
            raise ToolError("awaiting_ref must be a string when provided")
    elif awaiting_on is not None or awaiting_ref is not None:
        raise ToolError(
            "awaiting_on / awaiting_ref only valid with status='awaiting'"
        )

    if status == "failed":
        if not isinstance(failure_reason, str) or failure_reason not in _FAILURE_REASONS:
            raise ToolError(
                f"failure_reason required when status='failed'; must be one of "
                f"{sorted(_FAILURE_REASONS)}; got {failure_reason!r}"
            )
        if recoverable is not None and not isinstance(recoverable, bool):
            raise ToolError("recoverable must be a boolean when provided")
    elif failure_reason is not None or recoverable is not None:
        raise ToolError(
            "failure_reason / recoverable only valid with status='failed'"
        )

    declaration: dict[str, Any] = {
        "status": status,
        "summary": summary.strip(),
        "awaiting_on": awaiting_on,
        "awaiting_ref": awaiting_ref,
        "failure_reason": failure_reason,
        "recoverable": recoverable,
    }
    # The agent loop reads this after each tool dispatch and breaks
    # the loop. Mutating the context is the simplest channel — the
    # alternative (special-casing the tool name in the loop's tool
    # dispatch path) would couple the loop to one tool's name string.
    ctx.end_wakeup_declaration = declaration
    return {"acknowledged": True, "status": status}


END_WAKEUP = Tool(
    name="end_wakeup",
    description=(
        "Declare the outcome of this wakeup. MUST be the last tool you "
        "call — the runtime stops processing further tool_use blocks "
        "in the same turn after this fires.\n\n"
        "status:\n"
        "  - done:        Task goal met; no further wakeups needed.\n"
        "  - in_progress: You deliberately yield; another wakeup will\n"
        "                 resume soon to continue.\n"
        "  - awaiting:    Blocked on something external. Specify\n"
        "                 awaiting_on (mail / subtask / time /\n"
        "                 human_decision); awaiting_ref pins the\n"
        "                 specific identifier when applicable.\n"
        "  - failed:      Cannot make progress. Specify failure_reason\n"
        "                 (closed enum); recoverable hints whether\n"
        "                 retry might succeed.\n"
        "\n"
        "Without this call the wakeup cannot end cleanly — the runtime "
        "will nudge once for a declaration, then force-record "
        "'failed / silent_close' as the only honest fallback. Don't "
        "rely on the silent-close path; it surfaces as an alert."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": sorted(_END_WAKEUP_STATUSES),
            },
            "summary": {
                "type": "string",
                "description": (
                    "One- or two-sentence wrap-up of what this wakeup "
                    "accomplished or why it's ending here. Lands in "
                    "the wakeup row and downstream supervisor mail."
                ),
            },
            "awaiting_on": {
                "type": "string",
                "enum": sorted(_AWAITING_KINDS),
                "description": (
                    "Required iff status='awaiting'. What the next "
                    "wakeup is gated on."
                ),
            },
            "awaiting_ref": {
                "type": "string",
                "description": (
                    "Optional. Identifier the scheduler can use to "
                    "resume precisely — sender agent id / subtask id "
                    "/ ISO timestamp."
                ),
            },
            "failure_reason": {
                "type": "string",
                "enum": sorted(_FAILURE_REASONS - {"silent_close"}),
                "description": (
                    "Required iff status='failed'. Categorises the "
                    "failure so supervisors and task_terminated mail "
                    "can react."
                ),
            },
            "recoverable": {
                "type": "boolean",
                "description": (
                    "Only meaningful with status='failed'. True hints "
                    "retry might succeed; false hints the same wedged "
                    "state will recur."
                ),
            },
        },
        "required": ["status", "summary"],
    },
    handler=_end_wakeup,
)
