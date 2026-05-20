"""Audit timeline builder — combines task transitions, wakeup start/end,
mailbox deliveries, and (for in-flight wakeups) recent tool_use events from
their transcript JSONL.

Goal: one chronological feed that answers "what is the agent doing right
now, what just happened, and was anything sent to anyone?" — the core
auditability surface for the dashboard.

Implementation is poll-based: every refresh re-derives the window from the
DB plus a bounded number of tail lines from each active transcript. No
extra state, no broadcaster needed for this view.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..persistence.models import Agent, MailboxMessage, Task, Wakeup
from ..persistence.repositories import Repositories


@dataclass(frozen=True)
class ActivityEvent:
    at: str           # ISO timestamp, sortable lexicographically
    # "task" | "wakeup_end" | "mailbox" | "tool_use" | "assistant_text"
    # | "thinking" | "note"
    # (wakeup_start and turn_end are intentionally NOT emitted — they
    # were pure lifecycle noise. silent_close / failed surface through
    # wakeup_end severity instead.)
    kind: str
    severity: str     # "info" | "ok" | "warn" | "alert"
    headline: str     # one-line summary
    detail: dict[str, Any]  # structured extras (for the partial template)


def iso_minutes_ago(minutes: int) -> str:
    dt = datetime.now(UTC) - timedelta(minutes=minutes)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


def _short(s: str | None, n: int = 8) -> str:
    return (s or "")[:n]


def _severity_for_task(status: str) -> str:
    if status == "failed":
        return "alert"
    if status == "needs_input":
        return "warn"
    if status == "completed":
        return "ok"
    return "info"


def _severity_for_wakeup(end_status: str | None) -> str:
    if end_status == "failed":
        return "alert"
    # silent_close is "ran but produced no reply" — operator should know.
    if end_status == "silent_close":
        return "alert"
    if end_status == "completed":
        return "ok"
    if end_status == "needs_continuation":
        return "warn"
    return "info"


def _severity_for_urgency(urgency: str) -> str:
    if urgency == "blocker":
        return "alert"
    if urgency == "high":
        return "warn"
    return "info"


# Scheduler's auto-wake goal starts with this exact prefix (see
# scheduler._AUTO_INBOX_GOAL). We use it to identify auto-wake tasks
# so we can either skip them or render them as a compact "📥 inbox
# check" badge — the alternative is showing the 200-char goal verbatim
# next to every wakeup_end, which is the noise the operator wants gone.
_AUTO_INBOX_GOAL_PREFIX = "Check your inbox: call `mailbox_read()`"


def _build_task_events(tasks: list[Task]) -> list[ActivityEvent]:
    """Emit one ActivityEvent per task transition.

    Auto-wake "check inbox" tasks (scheduler-injected on mail arrival)
    are emitted with a compact label since the user-meaningful event
    is the wakeup_end that follows them, not the task lifecycle. They
    keep their `task_id` so click-through still works, but they don't
    spam the goal text. Dispatched tasks (substantive work the agent
    delegated) keep their full headline + goal.
    """
    events: list[ActivityEvent] = []
    for t in tasks:
        at = (
            t.updated_at.isoformat() if isinstance(t.updated_at, datetime)
            else (t.updated_at or "")
        )
        is_auto_wake = (t.goal or "").startswith(_AUTO_INBOX_GOAL_PREFIX)
        if is_auto_wake:
            # Suppress: the corresponding wakeup_end carries everything
            # operationally interesting (status, tokens, ctx, compaction).
            # Showing both creates two near-identical rows.
            continue
        headline = (
            f"task {_short(t.id)} ({t.persona_name}) → {t.status}"
        )
        events.append(
            ActivityEvent(
                at=at,
                kind="task",
                severity=_severity_for_task(t.status),
                headline=headline,
                detail={
                    "task_id": t.id,
                    "persona": t.persona_name,
                    "status": t.status,
                    "goal": (t.goal or "")[:120],
                    "parent_task_id": t.parent_task_id,
                },
            )
        )
    return events


def _build_wakeup_events(
    wakeups: list[Wakeup],
    model_context_windows: dict[str, int] | None = None,
) -> list[ActivityEvent]:
    """Only emit wakeup_end. wakeup_start was pure noise — the "active
    strip" at the top of the page already shows in-flight wakeups, and
    a start has no information not already implied by tool_use events
    that follow it. wakeup_end stays because it carries the terminal
    status (especially `silent_close` / `failed`) which the operator
    needs to notice — and per-wakeup context metrics (peak / window %
    + compaction count) which flag wakeups that ran close to the
    model's context limit.

    `model_context_windows` maps model_id → context_window tokens
    (built from the registry at app startup). Used to compute
    "peak / window %"; if absent, the dashboard shows absolute tokens
    only.
    """
    events: list[ActivityEvent] = []
    windows = model_context_windows or {}
    for w in wakeups:
        ended = (
            w.ended_at.isoformat()
            if isinstance(w.ended_at, datetime)
            else (w.ended_at or None)
        )
        if not ended:
            continue
        peak = w.context_peak_tokens or 0
        window = windows.get(w.model) if w.model else None
        peak_pct = (peak / window * 100) if (peak and window) else None
        compactions = w.compaction_count or 0
        # Headline: status, tokens, wall, tool count, AND context usage.
        ctx_part = ""
        if peak:
            if peak_pct is not None:
                ctx_part = (
                    f", ctx peak {_fmt_tokens(peak)}/{_fmt_tokens(window)} "
                    f"({peak_pct:.0f}%)"
                )
            else:
                ctx_part = f", ctx peak {_fmt_tokens(peak)}"
        if compactions:
            ctx_part += f", compacted ×{compactions}"
        events.append(
            ActivityEvent(
                at=ended,
                kind="wakeup_end",
                severity=_severity_for_wakeup(w.end_status),
                headline=(
                    f"wakeup {_short(w.id)} ended status={w.end_status} "
                    f"(tokens in={_fmt_tokens(w.token_input or 0)}/"
                    f"out={_fmt_tokens(w.token_output or 0)}, "
                    f"wall={w.wall_clock_ms or 0}ms, "
                    f"tools={w.tool_call_count or 0}{ctx_part})"
                ),
                detail={
                    "wakeup_id": w.id,
                    "task_id": w.task_id,
                    "persona": w.persona_name,
                    "model": w.model,
                    "end_status": w.end_status,
                    "context_peak_tokens": peak,
                    "context_window": window,
                    "context_peak_pct": peak_pct,
                    "compaction_count": compactions,
                },
            )
        )
    return events


def _fmt_tokens(n: int) -> str:
    """Compact token count: 12345 → 12.3K, 1234567 → 1.2M."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _build_spawn_events(
    agents: list[Agent], cutoff_iso: str
) -> list[ActivityEvent]:
    """Emit a "spawn" row for every agent created since `cutoff_iso`
    that has a `parent_agent_id`. Centered, low-key — the lineage is
    interesting context, not action."""
    events: list[ActivityEvent] = []
    for a in agents:
        if not a.parent_agent_id:
            continue
        at = (
            a.created_at.isoformat()
            if isinstance(a.created_at, datetime)
            else (a.created_at or "")
        )
        if not at or at < cutoff_iso:
            continue
        events.append(
            ActivityEvent(
                at=at,
                kind="spawn",
                severity="info",
                headline=f"{a.parent_agent_id} → spawned {a.id}",
                detail={
                    "parent": a.parent_agent_id,
                    "child": a.id,
                    "persona": a.persona_name,
                    "note": a.description or "",
                },
            )
        )
    return events


def _build_mailbox_events(msgs: list[MailboxMessage]) -> list[ActivityEvent]:
    events: list[ActivityEvent] = []
    for m in msgs:
        at = (
            m.delivered_at.isoformat()
            if isinstance(m.delivered_at, datetime)
            else (m.delivered_at or "")
        )
        # Activity headline shows the TITLE (subject line, ≤140 char) —
        # full body lives in detail.body for click-to-expand. Listing
        # only design: scanning the feed never burns body bytes.
        title = m.title or (m.body or "")[:80]
        events.append(
            ActivityEvent(
                at=at,
                kind="mailbox",
                severity=_severity_for_urgency(m.urgency),
                headline=(
                    f"mailbox {m.urgency} {m.sender} → {m.recipient}: "
                    f"{title}"
                ),
                detail={
                    "msg_id": m.id,
                    "sender": m.sender,
                    "recipient": m.recipient,
                    "urgency": m.urgency,
                    "title": m.title,
                    "task_id": m.task_id,
                    "body": m.body,
                },
            )
        )
    return events


def _ts_to_iso(ts_ms: Any, fallback: str) -> str:
    """Best-effort ts → ISO; fall back to a known timestamp so events
    still sort sensibly when ts is missing."""
    if isinstance(ts_ms, int):
        return (
            datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC)
            .strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4]
            + "Z"
        )
    return fallback


def _tail_transcript_events(
    transcript_uri: str | None,
    wakeup_id: str,
    persona: str,
    task_id: str,
    started_at: str,
    max_lines: int = 5000,
) -> list[ActivityEvent]:
    """Read up to the LAST `max_lines` of a wakeup's transcript JSONL and
    surface:
      - tool_use events (one per call)
      - note events (one per write_note)
      - assistant_text events — content_delta chunks aggregated per turn
      - thinking events — thinking_delta chunks aggregated per burst

    content_delta / thinking_delta on their own are super noisy (one
    event per token), so we glue consecutive deltas together. The
    accumulated text terminates at the next non-matching event / EOF.

    Default 5000 lines is enough to capture an entire 11-minute
    reasoning-heavy wakeup (~1500 lines empirically) without truncating
    early thinking bursts. Pure-token rows are tiny JSON so the memory
    footprint stays modest; we still cap so a runaway transcript can't
    OOM the dashboard.
    """
    if not transcript_uri or not transcript_uri.startswith("file://"):
        return []
    path = Path(transcript_uri[len("file://"):])
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    raw_lines = text.splitlines()[-max_lines:]

    events: list[ActivityEvent] = []
    # Two parallel rolling accumulators: plain text deltas (the
    # model's "out-loud" text — also never delivered, but the model
    # treats it as visible) and thinking deltas (private reasoning,
    # only the operator should see).
    text_buf: list[str] = []
    text_first_ts: str | None = None
    think_buf: list[str] = []
    think_first_ts: str | None = None

    def _flush_assistant_text() -> None:
        nonlocal text_first_ts
        if not text_buf:
            return
        full = "".join(text_buf).strip()
        if full:
            preview = full if len(full) <= 200 else full[:200] + "…"
            events.append(
                ActivityEvent(
                    at=text_first_ts or started_at,
                    kind="assistant_text",
                    severity="info",
                    headline=f"{persona} said: {preview}",
                    detail={
                        "wakeup_id": wakeup_id,
                        "task_id": task_id,
                        "persona": persona,
                        "text": full,
                    },
                )
            )
        text_buf.clear()
        text_first_ts = None

    def _flush_thinking() -> None:
        nonlocal think_first_ts
        if not think_buf:
            return
        full = "".join(think_buf).strip()
        if full:
            preview = full if len(full) <= 200 else full[:200] + "…"
            events.append(
                ActivityEvent(
                    at=think_first_ts or started_at,
                    kind="thinking",
                    severity="info",
                    headline=f"{persona} thinking: {preview}",
                    detail={
                        "wakeup_id": wakeup_id,
                        "task_id": task_id,
                        "persona": persona,
                        "text": full,
                    },
                )
            )
        think_buf.clear()
        think_first_ts = None

    for raw in raw_lines:
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        kind = obj.get("type")
        at = _ts_to_iso(obj.get("ts"), started_at)

        if kind == "content_delta":
            # A text delta closes any open thinking run (model
            # switched from reasoning to "out-loud"), then opens / extends
            # the text run.
            _flush_thinking()
            if not text_buf:
                text_first_ts = at
            text_buf.append(obj.get("text", ""))
            continue

        if kind == "thinking_delta":
            _flush_assistant_text()
            if not think_buf:
                think_first_ts = at
            think_buf.append(obj.get("text", ""))
            continue

        # Any non-delta event terminates BOTH delta runs.
        _flush_assistant_text()
        _flush_thinking()

        if kind == "tool_use":
            name = obj.get("name", "?")
            inp = obj.get("input") or {}
            inp_preview = ""
            for key in ("argv", "code", "to", "body", "kind", "msg_id", "rel_path"):
                if key in inp:
                    v = inp[key]
                    if isinstance(v, list):
                        v = " ".join(str(x) for x in v[:3])
                        if len(inp.get(key, [])) > 3:
                            v += " …"
                    inp_preview = f"{key}={str(v)[:60]}"
                    break
            # Full input shown in the expanded view — useful for
            # debugging "what did the model actually pass to shell_exec".
            try:
                inp_pretty = json.dumps(inp, ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                inp_pretty = str(inp)
            events.append(
                ActivityEvent(
                    at=at,
                    kind="tool_use",
                    severity="info",
                    headline=f"{persona} → {name}({inp_preview})",
                    detail={
                        "wakeup_id": wakeup_id,
                        "task_id": task_id,
                        "persona": persona,
                        "tool": name,
                        "tool_input_preview": inp_preview,
                        "tool_input_full": inp_pretty,
                    },
                )
            )
        elif kind == "note":
            note_text = obj.get("text", "")
            sev = "warn" if any(
                k in note_text.lower() for k in ("interrupt", "fallback", "kill", "nudge")
            ) else "info"
            events.append(
                ActivityEvent(
                    at=at,
                    kind="note",
                    severity=sev,
                    headline=f"note ({persona}): {note_text[:140]}",
                    detail={
                        "wakeup_id": wakeup_id,
                        "task_id": task_id,
                        "persona": persona,
                        "text": note_text,
                    },
                )
            )
        # turn_end events were noise: each one was a lifecycle marker
        # without standalone signal. The relevant cases (silent wakeup,
        # ended with status) now surface through wakeup_end and through
        # the silent-turn nudge / silent-close notes. Dropped from the
        # feed.
        elif kind == "turn_end":
            continue

    # Final flush in case the tail ends mid-delta (still streaming).
    _flush_assistant_text()
    _flush_thinking()
    return events


async def build_activity(
    repos: Repositories,
    minutes_back: int = 30,
    transcript_lookback_lines: int = 5000,
    agent_id: str | None = None,
    include_transcript: bool = False,
    model_context_windows: dict[str, int] | None = None,
) -> list[ActivityEvent]:
    """Assemble the audit timeline over the last `minutes_back` minutes.

    Two modes:

    **Global overview** (default — `agent_id=None`, `include_transcript=False`):
      Coarse events only — task transitions, wakeup start/end, mailbox
      deliveries. Skips the noisy transcript-derived events (tool_use,
      assistant_text, turn_end, note) so the page is scan-able.

    **Per-agent view** (`agent_id` set, `include_transcript=True`):
      Same coarse events FILTERED to events involving this agent
      (actor / sender / recipient), PLUS the transcript-derived events
      from this agent's wakeups (so you can drill in and read what the
      model said, what tools it called, where it went silent).
    """
    cutoff = iso_minutes_ago(minutes_back)
    tasks = await repos.tasks.find_recently_changed(cutoff, limit=100)
    wakeups = await repos.wakeups.list_since(cutoff, limit=100)
    msgs = await repos.mailbox.read_recent_for_audit(cutoff, limit=200)
    # Spawn events: surface every agent created within the window (with
    # a parent — bootstrap roots are not interesting). Cheap: just one
    # extra list_all + a date filter.
    agents = await repos.agents.list_all(include_archived=True)

    if agent_id is not None:
        # Filter to events that involve this agent. We match on agent_id
        # when the column is populated (post-A3) and fall back to
        # persona_name for legacy rows.
        def _task_matches(t: Task) -> bool:
            return (
                (t.agent_id == agent_id)
                or (t.agent_id is None and t.persona_name == agent_id)
            )

        def _wakeup_matches(w: Wakeup) -> bool:
            return (
                (w.agent_id == agent_id)
                or (w.agent_id is None and w.persona_name == agent_id)
            )

        def _msg_matches(m: MailboxMessage) -> bool:
            return m.sender == agent_id or m.recipient == agent_id

        tasks = [t for t in tasks if _task_matches(t)]
        wakeups = [w for w in wakeups if _wakeup_matches(w)]
        msgs = [m for m in msgs if _msg_matches(m)]
        # Per-agent view: include spawns involving this agent as parent
        # or child — same intuition as a chat showing both sides of a
        # threaded handoff.
        agents = [
            a for a in agents
            if a.id == agent_id or a.parent_agent_id == agent_id
        ]

    events = (
        _build_task_events(tasks)
        + _build_wakeup_events(wakeups, model_context_windows)
        + _build_mailbox_events(msgs)
        + _build_spawn_events(agents, cutoff)
    )

    if include_transcript:
        # Tail transcripts for ALL wakeups (already filtered to this
        # agent if agent_id was set) so the owner can read what the
        # model actually said + which tools fired + where turns went
        # silent. The most common troubleshooting question
        # ("why didn't the agent reply?") is answered here.
        seen: set[str] = set()
        for w in wakeups:
            if w.id in seen:
                continue
            seen.add(w.id)
            started_iso = (
                w.started_at.isoformat()
                if isinstance(w.started_at, datetime)
                else (w.started_at or "")
            )
            events.extend(
                _tail_transcript_events(
                    transcript_uri=w.transcript_uri,
                    wakeup_id=w.id,
                    persona=w.persona_name,
                    task_id=w.task_id,
                    started_at=started_iso,
                    max_lines=transcript_lookback_lines,
                )
            )
        # Plus any still-active wakeup whose row didn't show up in
        # list_since (started before window, still going).
        active = await repos.wakeups.list_active()
        if agent_id is not None:
            active = [
                w for w in active
                if (w.agent_id == agent_id)
                or (w.agent_id is None and w.persona_name == agent_id)
            ]
        for w in active:
            if w.id in seen:
                continue
            seen.add(w.id)
            started_iso = (
                w.started_at.isoformat()
                if isinstance(w.started_at, datetime)
                else (w.started_at or "")
            )
            events.extend(
                _tail_transcript_events(
                    transcript_uri=w.transcript_uri,
                    wakeup_id=w.id,
                    persona=w.persona_name,
                    task_id=w.task_id,
                    started_at=started_iso,
                    max_lines=transcript_lookback_lines,
                )
            )

    # Chronological order (oldest → newest, bottom is most recent).
    # Matches mainstream coding-agent chat experience (Claude Code,
    # Cursor, Codex): you scroll up to read history, the live cursor
    # is at the bottom. Reverse-chronological is for log tails, not
    # for "reading a conversation".
    events.sort(key=lambda e: e.at)
    return events


async def list_active_wakeups(repos: Repositories) -> list[Wakeup]:
    """Convenience for the header strip / agent-status indicator."""
    return await repos.wakeups.list_active()
