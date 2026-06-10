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

import os
import re
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import structlog

from ..adapter.llm_adapter import (
    ContentDelta,
    LLMAdapter,
    LyreContentBlock,
    LyreMessage,
    StreamEvent,
    TurnComplete,
)
from ..fsutil import atomic_write_text
from .adapter_factory import model_name_for_provider
from .agent_loop import AgentLoopResult
from .identity import agent_notes_rel_path, flat_id
from .model_registry import ModelEntry
from .model_router import ModelPreference, ModelRouter, NoEligibleModelError

log = structlog.get_logger()


SUMMARY_SECTION_HEADER = "## Auto-summary log"

# Idempotency sentinel for the one-time stray-notes heal in
# _append_to_notes: its presence in the canonical notebook means the
# legacy unflattened file's content has already been folded in.
_STRAY_MERGE_MARKER = "<!-- merged from pre-fix stray notes path -->"
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
    object_store_path: Path | None = None,
    notes_max_entries: int = 0,
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
        # Rotate the (now one-longer) auto-summary log down into the
        # cold-archive tier if it crossed the configured ceiling. Own
        # guard so a rotation hiccup never masks the successful append —
        # the summary is already durable on disk.
        _maybe_rotate_notes(
            memory_path=memory_path,
            object_store_path=object_store_path,
            agent_id=agent_id,
            max_entries=notes_max_entries,
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
    try:
        async for evt in stream:
            if isinstance(evt, ContentDelta):
                pieces.append(evt.text)
            elif isinstance(evt, TurnComplete):
                break
    except Exception:  # noqa: BLE001 — caller decides what to do
        return ""
    finally:
        # Breaking on TurnComplete leaves stream_turn suspended mid-stream;
        # the adapter's `async with messages.stream(...)` __aexit__ (HTTP
        # release) only runs when the generator is closed. aclose() makes
        # cleanup deterministic instead of GC-deferred. Guard it so a
        # close-time error can't mask the summary result.
        try:
            await stream.aclose()
        except Exception:  # noqa: BLE001
            pass
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
    # identity.agent_notes_rel_path flattens `persona/name` ids — the SAME
    # path seed creates and the identity preamble advertises. Building the
    # path from the raw id here used to fork every spawned agent's memory
    # into a stray `facts/agent-<persona>/<name>-notes.md` nobody reads.
    notes = memory_path / agent_notes_rel_path(agent_id)
    notes.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    short_id = wakeup_id[:8]
    entry = f"\n### {ts} · wakeup {short_id}\n{summary.strip()}\n"

    existing = notes.read_text(encoding="utf-8") if notes.exists() else ""

    # One-time heal for installs that already accumulated summaries at the
    # pre-fix stray path: fold that file's content into the canonical
    # notebook. ORDER MATTERS (kill-test law): the stray file is removed
    # only AFTER atomic_write_text below has durably published the merged
    # content — unlink-first would let a SIGKILL/ENOSPC in between
    # permanently destroy the very history this heal rescues. The marker
    # makes the merge idempotent: a kill after write-before-unlink leaves
    # the stray file behind, and the next wakeup just removes it without
    # folding the content in twice. Best-effort — a failure here must not
    # block the append (the stray file just survives for next time).
    legacy: Path | None = None
    if "/" in agent_id:
        candidate = memory_path / "facts" / f"agent-{agent_id}-notes.md"
        try:
            if candidate.is_file():
                legacy = candidate
                if _STRAY_MERGE_MARKER not in existing:
                    # errors="replace": salvaging mojibake beats letting a
                    # UnicodeDecodeError escape and permanently block every
                    # future auto-summary append for this agent.
                    stray = candidate.read_text(
                        encoding="utf-8", errors="replace"
                    ).strip()
                    if stray:
                        sep = (
                            "" if (not existing or existing.endswith("\n"))
                            else "\n"
                        )
                        existing = (
                            existing + sep
                            + f"\n{_STRAY_MERGE_MARKER}\n"
                            + stray + "\n"
                        )
        except OSError as exc:
            legacy = None
            log.warning(
                "stray_notes_merge_failed", agent_id=agent_id, error=str(exc)
            )

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

    # Atomic write: a SIGKILL mid-write must not truncate the durable
    # long-term notes file (kill-test law).
    atomic_write_text(notes, new_content)

    # Only now is the merged content durable — safe to drop the stray file.
    if legacy is not None:
        try:
            legacy.unlink()
            # The stray layout created facts/agent-<persona>/; drop the
            # directory once emptied so the facts/ scan stays clean.
            try:
                legacy.parent.rmdir()
            except OSError:
                pass
        except OSError as exc:
            log.warning(
                "stray_notes_cleanup_failed", agent_id=agent_id, error=str(exc)
            )


# ---------------------------------------------------------------------------
# RB-3: notes rotation → cold-archive (LONG_RUNNING_ROBUSTNESS.md §5)
#
# The `## Auto-summary log` section grows by one entry per wakeup and is never
# trimmed; an agent that reads its own notes loads the whole (unbounded) file
# into context. When `notes_max_entries > 0`, rotate the oldest entries beyond
# the ceiling down into the cold-archive tier, keeping the hot file bounded.
# The hand-written region ABOVE the log header is never touched.
# ---------------------------------------------------------------------------

# An entry header is exactly what `_append_to_notes` writes:
#   ### 2026-06-04T12:00:00Z · wakeup deadbeef
# Anchored + timestamp-shaped so a stray "### " inside a model-written summary
# body can't be mistaken for an entry boundary.
_ENTRY_HEADER_RE = re.compile(
    r"^### \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z · wakeup ([0-9a-fA-F]+)\s*$",
    re.MULTILINE,
)
# The pointer line we leave behind after rotating. Stripped before re-parsing
# so it never accretes or gets absorbed into the trailing entry block.
_POINTER_RE = re.compile(
    r"^> _Earlier auto-summaries archived to .*$\n?", re.MULTILINE
)


def _archived_wakeup_ids(archive_file: Path) -> set[str]:
    """Wakeup ids already present in the cold archive. Best-effort: a read
    failure returns empty (we'd then re-append, at-least-once with a possible
    duplicate — tolerable for a research-only archive)."""
    try:
        text = archive_file.read_text(encoding="utf-8")
    except OSError:
        return set()
    return {m.group(1) for m in _ENTRY_HEADER_RE.finditer(text)}


def _maybe_rotate_notes(
    *,
    memory_path: Path,
    object_store_path: Path | None,
    agent_id: str,
    max_entries: int,
) -> None:
    """Rotate the oldest auto-summary entries to cold-archive if the section
    exceeds `max_entries`. No-op when disabled (max_entries<=0), no object
    store configured, or the file/section is small. Best-effort: swallows its
    own errors (the summary append already succeeded and is durable)."""
    if max_entries <= 0 or object_store_path is None:
        return
    notes = memory_path / agent_notes_rel_path(agent_id)
    try:
        if not notes.exists():
            return
        text = notes.read_text(encoding="utf-8")
        marker_pos = text.find(SUMMARY_SECTION_HEADER)
        if marker_pos < 0:
            return
        line_end = text.find("\n", marker_pos)
        if line_end < 0:
            return
        # prefix = hand-written region + the log header line (NEVER rewritten
        # below the header except for the entries themselves).
        prefix = text[: line_end + 1]
        body = _POINTER_RE.sub("", text[line_end + 1 :])

        matches = list(_ENTRY_HEADER_RE.finditer(body))
        if len(matches) <= max_entries:
            return
        leading = body[: matches[0].start()]
        entries: list[tuple[str, str]] = []  # (wakeup_id, block) — newest-first
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            entries.append((m.group(1), body[m.start() : end]))

        keep_recent = max(1, max_entries // 2)
        keep = entries[:keep_recent]        # newest
        overflow = entries[keep_recent:]    # oldest → archive

        # (1) Append overflow to the cold archive FIRST (oldest-first, fsync).
        # Ordering is the kill-safety hinge: archive-then-rewrite means a crash
        # between the two re-archives the same overflow next time — deduped by
        # wakeup id — never loses an entry.
        archive_dir = object_store_path / "notes_archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_file = archive_dir / f"agent-{flat_id(agent_id)}.md"
        seen = _archived_wakeup_ids(archive_file)
        to_archive = [
            blk for (wid, blk) in reversed(overflow) if wid and wid not in seen
        ]
        if to_archive:
            with open(archive_file, "a", encoding="utf-8") as f:
                for blk in to_archive:
                    f.write(blk if blk.endswith("\n") else blk + "\n")
                f.flush()
                os.fsync(f.fileno())

        # (2) Rewrite the hot file atomically: kept entries + a pointer to the
        # archive where the older ones now live.
        pointer = (
            f"\n> _Earlier auto-summaries archived to "
            f"`notes_archive/agent-{flat_id(agent_id)}.md`._\n"
        )
        kept_text = "".join(blk for _, blk in keep)
        atomic_write_text(notes, prefix + leading + kept_text + pointer)
        log.info(
            "notes_rotated",
            agent_id=agent_id,
            kept=len(keep),
            archived=len(overflow),
        )
    except Exception as e:  # noqa: BLE001 — best-effort sidecar, never bubble
        log.warning(
            "notes_rotation_failed",
            agent_id=agent_id,
            error=str(e),
            type=type(e).__name__,
        )


# Provided for callers (and tests) that want to construct a synchronous
# no-op stand-in — e.g. test schedulers that don't care about summaries.
async def _noop(**_: object) -> None:
    return None


SummarizerHook = Callable[..., Awaitable[str | None]]
