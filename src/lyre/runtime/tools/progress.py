"""Progress tools: report_progress, report_side_effect.

report_progress     — agent self-checkpoint: persists task.checkpoint so a kill+
                      restart can resume.
report_side_effect — agent declares an externally visible side effect
                      (e.g., 'opened PR'). Goes through outbox as a tier1
                      notification; the dispatcher fans it out to subscribers
                      (e.g., owner mailbox).
"""

from __future__ import annotations

from typing import Any

from ...persistence.models import OutboxRow
from . import Tool, ToolContext, ToolError


async def _report_progress(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    note = args.get("note")
    checkpoint = args.get("checkpoint")
    if not isinstance(checkpoint, dict):
        raise ToolError("checkpoint must be an object (key/value map)")
    if note and not isinstance(note, str):
        raise ToolError("note must be a string")

    # The agent holds the lease for the duration of its wakeup, so it is the
    # legitimate holder for the checkpoint update.
    await ctx.repos.tasks.update_checkpoint(ctx.task_id, checkpoint, ctx.wakeup_id)
    return {"status": "ok", "task_id": ctx.task_id}


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


REPORT_PROGRESS = Tool(
    name="report_progress",
    description=(
        "CRASH-RECOVERY ONLY. Persist your current progress as a task "
        "checkpoint — the scheduler uses this to re-seed the NEXT wakeup "
        "of THE SAME task if this wakeup crashes mid-flight.\n\n"
        "NOT VISIBLE to owner or other agents. NOT a status update channel. "
        "For visibility / progress reports, use `mailbox_send` to the "
        "asker (owner / parent agent / etc.).\n\n"
        "Typical call: at meaningful boundaries (file edited, decision "
        "made, subtask done) so a crash doesn't lose state. Free-form "
        "key/value map."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "checkpoint": {
                "type": "object",
                "description": "Free-form key/value map. Examples: {'phase':'edit','files_changed':['README.md']}.",
            },
            "note": {
                "type": "string",
                "description": "Optional one-line note for the transcript.",
            },
        },
        "required": ["checkpoint"],
    },
    handler=_report_progress,
)

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
