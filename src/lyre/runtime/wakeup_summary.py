"""Post-wakeup summary: best-effort cheap-model LLM compresses a wakeup's
outcomes (mail sent, tools used, final text) into a few-bullet line and
appends it to the agent's notes file.

Runs after a wakeup has been finalized in the DB. Failures are swallowed
(logged at WARN, never raised) — the wakeup itself is already durable;
missing a summary line never breaks anything. If the model registry has
no cheap-tier model, the call is skipped entirely.

Design note: this replaces the deleted `summary-agent` persona. The old
design ran a separate agent that read other agents' transcripts on a
schedule; this version runs inline as part of every wakeup's finalize,
which guarantees the note exists right when the agent's next wakeup
loads its memory index — no race, no scheduling decision.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import structlog

from ..adapter.llm_adapter import (
    ContentDelta,
    LLMAdapter,
    LyreContentBlock,
    LyreMessage,
    TurnComplete,
)
from .adapter_factory import model_name_for_provider
from .agent_loop import AgentLoopResult
from .model_registry import ModelEntry
from .model_router import ModelPreference, ModelRouter, NoEligibleModelError

log = structlog.get_logger()


SUMMARY_SECTION_HEADER = "## Auto-summary log"
SUMMARY_PREFERENCE = ModelPreference(
    tier="cheap", requires=("streaming",), prefer=(),
)
SUMMARY_MAX_TOKENS = 256

AdapterForEntry = Callable[[ModelEntry], LLMAdapter]


async def summarize_and_append(
    *,
    wakeup_id: str,
    agent_id: str,
    persona_name: str,
    result: AgentLoopResult,
    memory_path: Path,
    router: ModelRouter,
    adapter_for_entry: AdapterForEntry,
) -> str | None:
    """Run a single cheap-model call, append the result to the agent notes.

    Returns the appended text on success, ``None`` on any failure or skip
    (no cheap model registered / nothing worth summarizing). Never raises.
    """
    try:
        prompt = _build_prompt(persona_name, result)
        if prompt is None:
            return None

        try:
            candidates = router.select(SUMMARY_PREFERENCE)
        except NoEligibleModelError:
            log.debug(
                "wakeup_summary_no_cheap_model",
                wakeup_id=wakeup_id,
            )
            return None

        # `router.select` returns candidates ranked by tier match but does
        # NOT filter out other tiers. For summary we want a STRICT cheap-only
        # behavior — if no cheap-tier model is registered, skip rather than
        # silently consume a workhorse/flagship slot (the wakeup's own
        # adapter queue, in tests). This also keeps cost predictable.
        candidates = [e for e in candidates if e.tier == "cheap"]
        if not candidates:
            log.debug(
                "wakeup_summary_no_strict_cheap_model",
                wakeup_id=wakeup_id,
            )
            return None

        summary = ""
        used_model: str | None = None
        for entry in candidates:
            adapter = adapter_for_entry(entry)
            summary = await _call_for_summary(
                adapter=adapter,
                model=model_name_for_provider(entry),
                prompt=prompt,
                max_tokens=SUMMARY_MAX_TOKENS,
            )
            if summary:
                used_model = entry.id
                break
        if not summary:
            log.debug(
                "wakeup_summary_empty",
                wakeup_id=wakeup_id,
                candidates_tried=len(candidates),
            )
            return None

        _append_to_notes(
            memory_path=memory_path,
            agent_id=agent_id,
            wakeup_id=wakeup_id,
            summary=summary,
        )
        log.info(
            "wakeup_summary_appended",
            wakeup_id=wakeup_id,
            agent_id=agent_id,
            model=used_model,
            chars=len(summary),
        )
        return summary
    except Exception as e:  # noqa: BLE001 — best-effort sidecar, never bubble
        log.warning(
            "wakeup_summary_failed",
            wakeup_id=wakeup_id,
            agent_id=agent_id,
            error=str(e),
            type=type(e).__name__,
        )
        return None


def _build_prompt(persona_name: str, result: AgentLoopResult) -> str | None:
    """Construct a tight summary prompt. Returns None when the wakeup did
    so little that a summary line would be noise."""
    sends = [tc for tc in result.tool_calls if tc.get("name") == "mailbox_send"]
    has_text = bool((result.text or "").strip())
    if not sends and not result.tool_calls and not has_text:
        return None

    parts: list[str] = [
        f"You are summarizing one wakeup of agent persona '{persona_name}'. "
        f"Output ONLY 1-3 short bullet points (each a single line, "
        f"<=120 chars). Capture (1) what the agent committed to / sent, "
        f"(2) any open thread the agent left implied for next time. "
        f"NO preamble, NO section headers, plain '- ' bullets only."
    ]

    if sends:
        parts.append("\nMail this wakeup sent:")
        for s in sends[:8]:
            inp = s.get("input") or {}
            to = inp.get("to")
            title = inp.get("title") or "(no title)"
            body = (inp.get("body") or "")[:300]
            parts.append(f"- to={to} title={title}\n  body={body}")
    else:
        names = [
            str(tc.get("name"))
            for tc in result.tool_calls
            if tc.get("name")
        ]
        if names:
            counts: dict[str, int] = {}
            for n in names:
                counts[n] = counts.get(n, 0) + 1
            tool_summary = ", ".join(
                f"{n}x{c}" for n, c in sorted(counts.items())
            )
            parts.append(f"\nTools used: {tool_summary}")

    if has_text and not sends:
        parts.append(f"\nFinal text:\n{(result.text or '')[:500]}")

    parts.append(f"\nStop: {result.stop_reason}; turns: {result.turns}")
    return "\n".join(parts)


async def _call_for_summary(
    *,
    adapter: LLMAdapter,
    model: str,
    prompt: str,
    max_tokens: int,
) -> str:
    """One non-tool turn against the adapter. Mirrors compact._call_for_summary."""
    user_msg = LyreMessage(
        role="user",
        content=[LyreContentBlock(type="text", text=prompt)],
    )
    pieces: list[str] = []
    try:
        stream = adapter.stream_turn(
            messages=[user_msg],
            tools=[],
            model=model,
            max_tokens=max_tokens,
            system=None,
        )
        async for evt in stream:
            if isinstance(evt, ContentDelta):
                pieces.append(evt.text)
            elif isinstance(evt, TurnComplete):
                break
    except Exception:  # noqa: BLE001 — caller decides what to do
        return ""
    return "".join(pieces).strip()


def _append_to_notes(
    *,
    memory_path: Path,
    agent_id: str,
    wakeup_id: str,
    summary: str,
) -> None:
    """Append summary entry under the canonical trailing section.

    Order is newest-first within the section, so a quick `head` shows
    recent wakeups without scrolling.
    """
    notes = memory_path / "facts" / f"agent-{agent_id}-notes.md"
    notes.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    short_id = wakeup_id[:8]
    entry = f"\n### {ts} · wakeup {short_id}\n{summary.strip()}\n"

    existing = notes.read_text(encoding="utf-8") if notes.exists() else ""

    if SUMMARY_SECTION_HEADER in existing:
        # Insert the new entry immediately after the section header so the
        # newest summary sits at top of the section.
        header_pos = existing.find(SUMMARY_SECTION_HEADER)
        line_end = existing.find("\n", header_pos)
        if line_end < 0:
            existing = existing + "\n"
            line_end = len(existing) - 1
        new_content = (
            existing[: line_end + 1]
            + entry
            + existing[line_end + 1 :]
        )
    else:
        sep = "" if not existing or existing.endswith("\n") else "\n"
        new_content = existing + sep + f"\n{SUMMARY_SECTION_HEADER}\n" + entry

    notes.write_text(new_content, encoding="utf-8")


# Provided for callers (and tests) that want to construct a synchronous
# no-op stand-in — e.g. test schedulers that don't care about summaries.
async def _noop(**_: object) -> None:
    return None


SummarizerHook = Callable[..., Awaitable[str | None]]
