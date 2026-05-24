"""Wakeup-lifecycle and side-effect tools.

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
