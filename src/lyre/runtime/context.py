"""Context assembly — turns a Task + Persona into LyreMessage list for the adapter.

Sprint 1 scope:
- Persona role + body → system prompt
- Global memory index (per FOUNDATION §3.8 / Q*: filesystem-backed memory) is
  injected at the END of the system prompt when a memory_root is provided
- task.goal / task.acceptance / optional checkpoint → initial user message

This module is the single seam where wakeup context is shaped, so the memory
index injection lives here (not buried in Scheduler).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..adapter.llm_adapter import LyreContentBlock, LyreMessage
from ..persistence.models import Persona, Task
from ..persistence.repositories import TaskRepository
from .memory import build_memory_index_for_prompt
from .skills import format_skills_for_prompt, load_skills_for_context


def _format_agents_directory(
    agents: list[Any], self_id: str, personas: list[Persona] | None = None
) -> str:
    """Render an 'address book' of live agent instances (not personas).

    Excludes self. Each entry shows agent_id + persona role description so
    the model knows what each agent can do and where to mailbox_send.
    """
    if not agents:
        return ""
    persona_desc: dict[str, str] = {}
    for p in personas or []:
        persona_desc[p.name] = (p.role_description or "").strip()

    lines = [
        "## Agents you can mailbox_send to",
        "",
        "(addresses are AGENT IDs, not persona names. Use `to: \"<id>\"` or "
        "`to: [\"<id1>\", \"<id2>\"]` for broadcast.)",
    ]
    for a in agents:
        if a.id == self_id:
            continue
        if getattr(a, "status", "idle") == "archived":
            continue
        desc = persona_desc.get(a.persona_name, "")
        own_desc = a.description if getattr(a, "description", None) else ""
        suffix = own_desc or desc or a.persona_name
        lines.append(f"- {a.id} (persona={a.persona_name}): {suffix}")
    return "\n".join(lines)


def _walk_agents_md(cwd: Path) -> list[Path]:
    """Find AGENTS.md / CLAUDE.md from `cwd` upward to filesystem root.

    Order: most-specific (cwd) first, then parents. PI does the same walk;
    we keep both filenames so Claude Code conventions (CLAUDE.md) work
    without per-agent config. Order matters because concatenation is
    leaf-to-root — agents read deeper-context first.

    Stops at filesystem root or the first directory that is also `cwd`'s
    own parent of the home dir (we never walk into ~/.. since user-level
    AGENTS.md should be in ~ explicitly).
    """
    found: list[Path] = []
    seen: set[Path] = set()
    try:
        cwd = cwd.resolve()
    except OSError:
        return found
    current = cwd
    while True:
        for name in ("AGENTS.md", "CLAUDE.md"):
            f = current / name
            if f.is_file() and f.resolve() not in seen:
                found.append(f)
                seen.add(f.resolve())
        parent = current.parent
        if parent == current:  # filesystem root
            break
        current = parent
    return found


def assemble_system_prompt(
    persona: Persona,
    agent_id: str | None = None,
    memory_root: Path | None = None,
    lyre_home: Path | None = None,
    worktree_cwd: Path | None = None,
    other_agents: list[Any] | None = None,
    # Back-compat: old call site passed `other_personas=`. We accept it but
    # treat each persona as a single (degenerate) agent named after itself,
    # since the bootstrap-seeded agents have id == persona.name.
    other_personas: list[Persona] | None = None,
) -> str:
    """Combine persona.role_description + persona.system_prompt + (optional)
    memory index + (optional) agents directory into one system prompt.

    The memory index is a markdown section listing every approved skill,
    proposed-skill, fact, and persona profile in the memory dir. The agents
    directory lists every approved persona so the agent knows who it can
    mailbox_send to. Both are "menu in the prompt, body on demand" per the
    Hermes/Pi pattern.
    """
    # Identity + universal protocols. THIS BLOCK IS BYTE-IDENTICAL ACROSS
    # WAKEUPS for the same (agent_id, persona, allowed_tools, peers).
    # Putting it first maximizes the cached prefix on Anthropic & DeepSeek.
    effective_id = agent_id or persona.name
    parent_agent_id: str | None = None
    # Peer bootstrap agents — first parent-less agent per persona name,
    # excluding self. Lets persona bodies stay generic ("hand off to the
    # analyst") while the preamble names the actual agent ids the owner
    # set via ``display_name`` in each persona's identity.md.
    peer_bootstrap: dict[str, str] = {}
    if other_agents:
        for a in other_agents:
            if getattr(a, "id", None) == effective_id:
                parent_agent_id = getattr(a, "parent_agent_id", None)
                continue
            if getattr(a, "parent_agent_id", None) is not None:
                continue  # spawned, not a bootstrap singleton
            if getattr(a, "status", "idle") == "archived":
                continue
            p_name = getattr(a, "persona_name", None)
            if p_name and p_name not in peer_bootstrap:
                peer_bootstrap[p_name] = getattr(a, "id", "")
    identity = _build_identity_preamble(
        effective_id, persona.name, parent_agent_id=parent_agent_id,
        peer_bootstrap=peer_bootstrap or None,
    )

    parts: list[str] = [
        identity,
        "",
        persona.role_description,
        "",
        persona.system_prompt,
    ]

    # ----- Stable suffixes (rare changes) ---------------------------
    # APPEND.md is owner-edited, changes manually & rarely.
    if lyre_home is not None:
        append_path = lyre_home / "personas" / persona.name / "APPEND.md"
        if append_path.is_file():
            try:
                parts.append("")
                parts.append(append_path.read_text(encoding="utf-8").strip())
            except OSError:
                pass

    # SYSTEM.md is owner-edited deployment-wide, changes rarely.
    if lyre_home is not None:
        sys_path = lyre_home / "SYSTEM.md"
        if sys_path.is_file():
            try:
                global_addend = sys_path.read_text(encoding="utf-8").strip()
                if global_addend:
                    parts.append("")
                    parts.append(global_addend)
            except OSError:
                pass

    # Owner identity / preferences — user-authored, agents never write here.
    # `lyre onboard` creates the initial template at ~/.lyre/user.md.
    if lyre_home is not None:
        user_md_path = lyre_home / "user.md"
        if user_md_path.is_file():
            try:
                user_body = user_md_path.read_text(encoding="utf-8").strip()
                if user_body:
                    parts.append("")
                    parts.append("## Owner identity & preferences (set by owner in ~/.lyre/user.md)")
                    parts.append(user_body)
            except OSError:
                pass

    # AGENTS.md walk — repo-specific, stable per worktree session.
    if worktree_cwd is not None:
        agents_files = _walk_agents_md(worktree_cwd)
        if agents_files:
            chunks: list[str] = []
            for f in agents_files:
                try:
                    chunks.append(
                        f"# {f}\n\n{f.read_text(encoding='utf-8').strip()}"
                    )
                except OSError:
                    continue
            if chunks:
                parts.append("")
                parts.append("## Project-local instructions (AGENTS.md / CLAUDE.md walk)")
                parts.append("\n\n---\n\n".join(chunks))

    # ----- More volatile sections (move toward tail) ----------------
    # Memory index — changes whenever a fact/persona file is added/edited.
    effective_lyre_home: Path | None = lyre_home
    if effective_lyre_home is None and memory_root is not None:
        effective_lyre_home = memory_root.parent
    if memory_root is not None:
        index_md = build_memory_index_for_prompt(
            memory_root, allowed_tools=list(persona.allowed_lyre_tools or [])
        )
        if index_md:
            parts.append("")
            parts.append(index_md)

    # Skills XML — changes whenever a skill is approved/archived.
    if effective_lyre_home is not None:
        result = load_skills_for_context(
            effective_lyre_home,
            agent_id=effective_id,
            persona_name=persona.name,
        )
        skills_block = format_skills_for_prompt(result.skills)
        if skills_block:
            parts.append(skills_block)

    # NOTE: the "Agents directory" used to live here and was the worst
    # cache breaker — every create_agent / archive_agent invalidated
    # every other agent's system prompt. Removed in favor of having
    # agents call list_agents() on demand. The identity preamble's
    # Mail Protocol points them at that tool. Unused parameter kept
    # for back-compat with legacy test callsites.
    del other_agents  # noqa: F841 — explicitly unused; kept in signature
    del other_personas  # noqa: F841

    return "\n".join(parts).strip()


def _build_identity_preamble(
    agent_id: str,
    persona_name: str,
    *,
    parent_agent_id: str | None = None,
    peer_bootstrap: dict[str, str] | None = None,
) -> str:
    """The universal head of every system prompt. Byte-identical for a
    given (agent_id, persona_name, parent_agent_id, peer_bootstrap) —
    maximizes Anthropic / DeepSeek prefix-cache hits across wakeups. The
    parent-agent line only appears when there IS a parent (spawned
    agents); bootstrap roots like `owner`/`dispatcher` see the prompt
    without that line, preserving cache reuse for them too.

    ``peer_bootstrap`` maps persona name → current agent id for each
    bootstrap-seeded peer OTHER than self. Persona bodies refer to
    peers generically ("the analyst", "your reviewer") so they stay
    correct when the owner customizes agent ids via ``display_name``
    in each persona's identity.md; this section in the preamble
    grounds those generic references to the actual ids."""
    # `persona/name` ids would otherwise hint at a directory layer in
    # the notes / scratchpad paths; flatten `/` to `-` to match the
    # ensure_agent_*_file filename convention.
    _flat_id = agent_id.replace("/", "-")
    notes_path = f"~/.lyre/memory/facts/agent-{_flat_id}-notes.md"
    scratchpad_path = f"~/.lyre/memory/scratchpad/{_flat_id}.md"
    scratchpad_rel = f"scratchpad/{_flat_id}.md"
    parent_line = ""
    if parent_agent_id:
        parent_line = (
            f"You were spawned by `{parent_agent_id}` — that is your "
            f"parent agent. If you need clarification, are blocked, or "
            f"want to escalate, mailbox_send to `{parent_agent_id}` "
            f"rather than going to `owner` directly. The parent "
            f"orchestrates the work; let them decide whether owner needs "
            f"to be looped in.\n"
        )
    peers_block = ""
    if peer_bootstrap:
        # Sort by persona for stable cache-friendly ordering.
        lines = [
            f"  • the {p} = `{aid}`"
            for p, aid in sorted(peer_bootstrap.items())
        ]
        peers_block = (
            "\n**YOUR TEAM — IMPORTANT.** When this prompt below talks "
            "generically about \"the dispatcher\" / \"the analyst\" / "
            "\"the reviewer\", those refer to these CURRENT live agent "
            "ids — use them as recipients in mailbox_send / dispatch_task:\n"
            + "\n".join(lines)
            + "\nThe owner may have renamed these via config.toml, so the "
            "names above are the source of truth — DO NOT use generic "
            "strings like \"dispatcher\" as recipients unless that "
            "literally matches an id above.\n"
        )
    return (
        f"You are agent **{agent_id}** (persona: `{persona_name}`).\n"
        f"Your mailbox key is `{agent_id}` — when you call mailbox_read "
        f"without a `recipient`, that's what it defaults to. When other "
        f"agents send mail to you they address it to `{agent_id}`.\n"
        f"Do not refer to yourself by any other name. In particular, do "
        f"not synthesize variants like `{agent_id}-scheduler` or "
        f"`{persona_name}-foo`.\n"
        + parent_line
        + peers_block +
        f"\n"
        f"**HOW WAKEUPS END — REQUIRED.** Every wakeup MUST terminate "
        f"by calling `end_wakeup(...)` as your LAST tool call. The "
        f"runtime stops processing further tool_use blocks after it "
        f"fires. Without an explicit declaration the runtime cannot "
        f"tell whether your work succeeded, is waiting on something, "
        f"or failed — it will nudge once for a declaration, then "
        f"force-record `failed / silent_close` as the only honest "
        f"fallback (which surfaces as an alert).\n"
        f"\n"
        f"The four statuses (pick the one that matches reality):\n"
        f"  • `done` — task goal met. Read informational mail with "
        f"nothing to do? Still `done` — that IS the work.\n"
        f"  • `awaiting` — blocked on an external event. Specify "
        f"`awaiting_on` (`mail` / `subtask` / `time` / "
        f"`human_decision`); `awaiting_ref` pins the specific id "
        f"when applicable. Typical after `dispatch_task` (await "
        f"subtask) or a question to the owner (await mail).\n"
        f"  • `in_progress` — you deliberately yielded mid-task; you "
        f"want another wakeup to resume soon.\n"
        f"  • `failed` — cannot make progress. Specify "
        f"`failure_reason` (closed enum: `loop_exhausted` / "
        f"`tool_error` / `provider_error` / `precondition_failed` / "
        f"`dependency_failed` / `cancelled_by_owner` / "
        f"`cancelled_by_parent` / `policy_violation`); set "
        f"`recoverable=True` if retry might succeed.\n"
        f"\n"
        f"The \"ack-and-stop antipattern\" (\"I'll look into it\" → "
        f"stop without doing the work or declaring) is now a runtime "
        f"error: the wakeup gets recorded as failed/silent_close and "
        f"surfaces to your supervisor. If you genuinely need to "
        f"defer, declare `awaiting` with the trigger you're waiting "
        f"on — don't just stop calling tools.\n"
        f"\n"
        f"**HOW YOU COMMUNICATE — IMPORTANT.** Plain text in your response "
        f"is NOT delivered to anyone. It is internal monologue: useful "
        f"for reasoning, but owner / other agents never see it directly. "
        f"The only way to actually reach another party is a tool call:\n"
        f"  • Reply to a sender → `mailbox_send(to=\"<sender-agent-id>\", "
        f"title=\"...\", body=...)`\n"
        f"  • Hand work off → `dispatch_task(agent=\"<id>\", goal=..., acceptance=...)`. "
        f"After dispatching, **just stop calling tools** and let the wakeup "
        f"close — the subagent will mail you back when done, "
        f"auto-wake-on-mail will start a fresh wakeup for you to read "
        f"their result. There is NO blocking 'wait for children' primitive; "
        f"events are the only synchronisation.\n"
        f"Every owner / user-visible response must go through "
        f"`mailbox_send`. Plain text alone reaches no one.\n"
        f"\n"
        f"**MAIL PROTOCOL — IMPORTANT.**\n"
        f"  • `mailbox_read()` (default `box=\"inbox\"`) returns ONLY "
        f"unread mail (sorted blocker → high → normal → low). LISTINGS "
        f"ONLY: id, sender, urgency, title, body_chars — NOT full body. "
        f"Calling it AUTO-MARKS returned mail as read; they won't appear "
        f"in future inbox reads.\n"
        f"  • `mailbox_read(box=\"sent\")` returns mail YOU sent, "
        f"newest-first. Use this to recall what you said/promised in "
        f"prior wakeups. Pass `recipient=\"<id>\"` to filter to a "
        f"specific person. No auto-mark.\n"
        f"  • To read a specific message's full body: "
        f"`mailbox_get_message(msg_id=N)`. Use `body_chars` from the "
        f"listing to decide whether the body is worth fetching.\n"
        f"  • To dismiss FYI mail without reading the inbox: "
        f"`mark_read(msg_id=N)`. Or just let the next inbox read "
        f"auto-mark it.\n"
        f"  • Archive (already-read) inbox: "
        f"`mailbox_read(include_read=True)`. No auto-mark.\n"
        f"  • When you SEND mail, always provide a clear `title` "
        f"(≤140 char subject line). Readers see ONLY your title in "
        f"their inbox — they decide whether to fetch the body. "
        f"Good: \"PR #123 ready: typo fix\". Bad: \"update\", \"FYI\", empty.\n"
        f"\n"
        f"**ACK-AND-STOP IS A LIE — IMPORTANT.** When someone asks you "
        f"to do something, sending an IOU (\"I'll look into X\" / "
        f"\"我去看看\" / \"let me start a background task\" / "
        f"\"稍后回复\") and then producing no further tool calls is the "
        f"single most common Lyre failure mode. There is no background "
        f"thread; once you stop calling tools the wakeup closes and "
        f"the asker waits forever. Do the work in this wakeup, then "
        f"`mailbox_send` with the actual result.\n"
        f"\n"
        f"If the work genuinely takes more than one wakeup, the "
        f"legitimate \"later\" paths are:\n"
        f"  • `dispatch_task` to a worker — you get a real `task_id` "
        f"back. Then `mailbox_send` quoting that id is honest.\n"
        f"  • `mailbox_send(to=\"<yourself>\", deliver_in=\"30m\", "
        f"body=...)` — future-mail yourself a reminder; tell the "
        f"asker when you'll come back.\n"
        f"Either way the legitimacy comes from a real tool call you "
        f"already made, not a verbal promise.\n"
        f"\n"
        f"If the work genuinely needs more than one wakeup (hours, "
        f"awaiting external systems, etc.), the LEGITIMATE paths are:\n"
        f"  • `dispatch_task` to a worker — real subagent. Then "
        f"`mailbox_send(\"kicked off task <id>, will report when it "
        f"finishes\")` is honest because the task_id is real.\n"
        f"  • Future-mail yourself a reminder (`deliver_in=\"30m\"`) and "
        f"send the asker an interim progress mailbox_send. Next wakeup "
        f"you continue.\n"
        f"Either way, the legitimacy comes from an actual tool call you "
        f"already made, not from a verbal claim.\n"
        f"\n"
        f"**KNOWING THE TEAM.** The other agents you can mailbox_send to "
        f"or dispatch_task to are NOT listed in this prompt — call "
        f"`list_agents()` when you need the current roster. Use "
        f"`list_personas()` to see role templates you can spawn fresh "
        f"agents of via `create_agent`. The roster changes often; "
        f"caching it in your prompt would burn tokens every change.\n"
        f"\n"
        f"**STATELESS WAKEUPS — IMPORTANT.** Every wakeup is a fresh "
        f"process. The messages in this conversation (your plain-text "
        f"reasoning, the assistant_text content blocks, your scratch "
        f"thinking) will be DISCARDED once this wakeup closes. There "
        f"is NO automatic memory of what you thought, planned, or "
        f"promised in prior wakeups.\n"
        f"\n"
        f"**WAKEUP ROUTINE — every wakeup MUST start with these two "
        f"reads, in this order**, before doing any task work:\n"
        f"  1. `read_memory(\"{scratchpad_rel}\")` — what past-you was "
        f"tracking (open commitments, pending checks, half-finished "
        f"thoughts). Skipping this is the single most common failure "
        f"mode: you act on the current message in isolation, drop "
        f"yesterday's promises on the floor, and the owner has to "
        f"chase you about them.\n"
        f"  2. `mailbox_read()` — what arrived since you slept.\n"
        f"Then proceed. Both reads are cheap; the cost of skipping is "
        f"that ALL the cross-wakeup memory machinery below is "
        f"effectively write-only.\n"
        f"\n"
        f"**WHAT IS NOT DISCARDED** — these are durable across wakeups, "
        f"and you can read them at any time in any wakeup:\n"
        f"  • Your mailbox (sent + received). `mailbox_read()` for "
        f"new mail, `mailbox_read(include_read=True)` to re-read "
        f"already-seen mail, `mailbox_read(box=\"sent\")` to see what "
        f"YOU sent. `mailbox_get_message(msg_id=N)` for any single "
        f"message's full body. Owner's old messages to you are all "
        f"still there — read them.\n"
        f"  • The filesystem under `~/.lyre/memory/` — your notes, "
        f"persona profiles, facts. Read via `read_memory(...)` or "
        f"`shell_exec cat ...`.\n"
        f"  • Persisted facts about agents (list_agents shows everyone).\n"
        f"\n"
        f"So the *correct* reaction when an asker references prior "
        f"context (\"the X you were investigating\", \"my earlier "
        f"message about Y\") is NOT to say \"I'm stateless, I forgot.\" "
        f"It's to **go fetch the durable record**: "
        f"`mailbox_read(include_read=True)` to see what they sent "
        f"before, `mailbox_read(box=\"sent\")` to see what you "
        f"replied, `read_memory(...)` for your own notes.\n"
        f"\n"
        f"If you need state to survive until the next wakeup, you MUST "
        f"persist it explicitly via one of these:\n"
        f"  1. **Scratchpad (your short-term memory) — start here.** "
        f"A markdown file at `{scratchpad_path}` that persists across "
        f"wakeups. It is YOURS — read at the start of every wakeup, "
        f"write whenever you make a commitment or decide a next step. "
        f"This is the SINGLE most important answer to \"what was I "
        f"doing / what did I promise\":\n"
        f"      • read:      `read_memory(\"{scratchpad_rel}\")` — "
        f"FIRST thing every wakeup, before doing anything else.\n"
        f"      • append:    `update_scratchpad(content=\"- promised "
        f"X by Y\", mode=\"append\")` when committing to anything.\n"
        f"      • clean up:  `update_scratchpad(content=<pruned "
        f"version>, mode=\"overwrite\")` after finishing items. "
        f"**Done items MUST be removed** — leaving them in pollutes "
        f"every future wakeup's context and creates the \"I keep "
        f"forgetting what's done\" failure mode.\n"
        f"  2. **Future mail to yourself** — for time-bound reminders. "
        f"`mailbox_send(to=\"{agent_id}\", title=\"reminder: …\", "
        f"body=\"…\", deliver_in=\"5m\")`. Scheduler wakes you at the "
        f"due time with full context inline. Use for any \"come back "
        f"to this later\" intent.\n"
        f"  3. **Notes file (long-term memory)** — a pre-created "
        f"markdown file lives at `{notes_path}`. Different from "
        f"scratchpad: scratchpad = \"what I'm tracking right now, "
        f"clears as items finish\"; notes = \"what I've LEARNED — "
        f"owner preferences, project decisions, gotchas\". Notes "
        f"persist forever; scratchpad cycles. The runtime also "
        f"appends an `## Auto-summary log` here at every wakeup end. "
        f"Read with `read_memory(\"facts/agent-{_flat_id}-notes.md\")`. "
        f"Append via `shell_exec` / `python_exec` if you have them, "
        f"or delegate to an analyst.\n"
        f"  4. **Your own sent mail** — `mailbox_read(box=\"sent\")` "
        f"shows what you said. Treat this as a FALLBACK audit trail, "
        f"not as primary memory — scratchpad is what you should "
        f"actually rely on for \"did I follow through\".\n"
        f"You don't need to use all four every wakeup — but the "
        f"scratchpad pair (read at start, update on commit/done) is "
        f"required if you make ANY commitment that outlives this wakeup.\n"
        f"\n"
        f"**DELEGATING WORK — IMPORTANT.** When you `dispatch_task(...)`, "
        f"a subagent gets a fresh wakeup with its own goal/acceptance. "
        f"You do NOT block — control returns to you immediately and you "
        f"can dispatch more, or stop calling tools and let the wakeup "
        f"close. The subagent will mail you back when done; "
        f"auto-wake-on-mail starts a fresh wakeup of yours when that "
        f"mail lands. For soft-timeout / status check, schedule a mail "
        f"to yourself (`mailbox_send(to=<self>, deliver_in=\"30m\", "
        f"...)`) — when it fires, query_task_status the children and "
        f"decide whether to wait more, re-dispatch, or escalate. "
        f"There is NO blocking 'wait for all children' primitive; the "
        f"runtime is event-driven all the way down. Three invariants:\n"
        f"  • **No phantom delegation.** Words like \"I started a "
        f"background task\", \"我安排了 worker 去做\", \"let me run "
        f"this in the background\", \"kicked off a job\" are only "
        f"truthful if you JUST called `dispatch_task` and have a "
        f"real task_id from the tool result. If you cannot quote the "
        f"task_id, do NOT use those words. \"Background\" with no "
        f"task_id behind it is a hallucination — and the most common "
        f"failure mode of Lyre agents.\n"
        f"  • **Always report before idling.** After a real "
        f"`dispatch_task`, send the owner a mailbox_send saying "
        f"\"I've kicked off task <id> with <agent>; will report "
        f"back\". Don't just go silent — from the owner's POV that "
        f"looks like you dropped the ball.\n"
        f"  • **Track delegated work in your scratchpad.** Append the "
        f"task_id, who you delegated to, and what's expected back via "
        f"`update_scratchpad(mode=\"append\")`. When the subagent's "
        f"reply arrives in a future wakeup you'll have the trail. "
        f"Once that work lands, remove the line via mode=\"overwrite\".\n"
        f"\n"
        f"**PROGRESS VIA MAIL.** Mail is the universal channel for "
        f"long-running work. If a task takes multiple wakeups or hours, "
        f"emit periodic progress mail to whoever's waiting (often the "
        f"owner). The pattern is:\n"
        f"  • Schedule a recurring self-ping: "
        f"`mailbox_send(to=\"{agent_id}\", title=\"check in on …\", "
        f"body=\"...\", recur_every=\"30m\")` — on each wake decide "
        f"whether to report up.\n"
        f"  • Or just send `mailbox_send(to=\"<owner-or-asker>\", "
        f"title=\"progress: …\", body=\"...\")` when you have something "
        f"worth saying.\n"
        f"Silence on a multi-wakeup task = the asker assumes you "
        f"forgot. Periodic mail is cheap insurance against that."
    )


async def assemble_initial_user_message(
    task: Task,
    tasks_repo: TaskRepository | None = None,
    mailbox_repo: Any | None = None,
    agent_id: str | None = None,
) -> LyreMessage:
    """Build the initial user-role message for a wakeup.

    Composition:
      - task.goal + acceptance (always)
      - subagent children with status (if `tasks_repo` given and any)

    Cross-wakeup recall is NOT inlined here. Agents are stateless across
    wakeups but have explicit channels for self-recall:
      - `mailbox_read(box="sent")` to see what they sent / promised
      - `read_memory` / `shell_exec cat ~/.lyre/memory/...` for notes
    The identity preamble teaches them this. Auto-injecting recent sends
    is fighting the model's agency.
    """
    del mailbox_repo, agent_id  # No longer auto-inlined; reserved in sig.

    body = f"""【任务 goal】
{task.goal}

【验收标准】
{task.acceptance}
"""

    if tasks_repo is not None:
        children = await tasks_repo.find_children(task.id)
        if children:
            body += "\n【你 dispatch 的 subagent 任务】\n"
            for c in children:
                line = f"- id={c.id} persona={c.persona_name} status={c.status}"
                body += line + "\n"
            body += (
                "\n用 query_task_status(<id>) 拿任意子任务的完整 checkpoint / "
                "wakeup 记录。\n"
            )

    return LyreMessage(role="user", content=[LyreContentBlock(type="text", text=body)])
