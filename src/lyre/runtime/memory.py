"""Filesystem-backed memory layer.

Memory lives in `~/.lyre/memory/` (per Config.memory_path). Agent-write,
user-read-by-convention. Three canonical buckets:

    memory/
    ├── facts/<topic>.md                ← agent-curated knowledge with
    │                                     frontmatter (kind, scope).
    │                                     Long-term, semi-global.
    ├── facts/agent-<id>-notes.md       ← per-agent long-term notebook
    │                                     (owner preferences, decisions,
    │                                     gotchas). Runtime also appends
    │                                     ## Auto-summary log entries at
    │                                     each wakeup end.
    └── scratchpad/<flat-agent-id>.md   ← per-agent SHORT-TERM working
                                          memory. What's being tracked
                                          right now, recent commitments,
                                          next steps. Cycles as items
                                          finish (overwrite mode on the
                                          ``update_scratchpad`` tool).

Three buckets, three durabilities:
    facts/<topic>.md           — knowledge that outlives the agent
    facts/agent-<id>-notes.md  — what one agent has learned over its life
    scratchpad/<id>.md         — what one agent is doing this week

Skills are tracked separately under `~/.lyre/skills/`. Owner identity &
preferences (user-write, agent-read) live at `~/.lyre/user.md` and are
injected into every system prompt by `context.assemble_system_prompt`;
they never appear here.

Read access: any path → `read_memory(rel_path)` (sandboxed).
Write access:
  - facts/ and notes: `shell_exec` / `python_exec` (so they require a
    persona that has them — typically analyst/worker).
  - scratchpad: dedicated `update_scratchpad(content, mode)` tool
    available to every LLM persona, sandboxed to the agent's own file.

At wakeup start, the scheduler scans `facts/`, reads JUST the
frontmatters, and injects an index ("Available global memory") into the
system prompt. Scratchpad files are NOT indexed — they're agent-private,
short-lived, and the model is reminded of its scratchpad path in the
identity preamble.

Frontmatter expected fields (all optional, facts/ only):
    description  : one-line summary used in the index
    scope        : free-form string ("lisa-lang", "global", ...)
    kind         : for facts, the fact category ("repo_info", "api_quirk", ...)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

MemoryKind = Literal["fact"]


@dataclass(frozen=True)
class MemoryEntry:
    kind: MemoryKind
    name: str
    path: Path  # absolute path
    rel_path: str  # path relative to memory_root, e.g. "skills/approved/foo.md"
    frontmatter: dict[str, Any]

    @property
    def description(self) -> str:
        d = self.frontmatter.get("description")
        if isinstance(d, str) and d.strip():
            return d.strip()
        return ""

    @property
    def scope(self) -> str | None:
        s = self.frontmatter.get("scope")
        return str(s) if s else None

    @property
    def type(self) -> str | None:
        """The `type` frontmatter field (e.g. 'spec', 'review_checklist',
        'agent_notes'). Already written by every fact author; the index groups
        by it so the menu stays scannable as facts accumulate. None when unset."""
        t = self.frontmatter.get("type")
        return str(t).strip() if t and str(t).strip() else None

    def applies_to(self, *, agent_id: str | None, persona_name: str | None) -> bool:
        """Whether this fact's scope makes it relevant to the given agent.

        C2: reuses the skills scope grammar ('global' | 'persona=<name>' |
        'agent=<id>'). Existing facts carry FREE-FORM scope strings (e.g.
        'lisa-lang') that the grammar rejects — those fall back to global
        (always-applies), so scoping is strictly opt-in and never hides a fact
        that didn't deliberately scope itself.
        """
        # Local import keeps the memory↔skills dependency one-directional and
        # lazy (skills.py never imports memory.py).
        from .skills import SkillScope

        try:
            parsed = SkillScope.parse(self.scope)
        except ValueError:
            return True  # free-form / unparseable scope → treat as global
        return parsed.applies_to(agent_id=agent_id, persona_name=persona_name)


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Parse YAML frontmatter block; tolerate files without it.

    Format:
        ---
        key: value
        ...
        ---
        body...
    """
    if not text.startswith("---"):
        return {}
    # Find the closing '---' on its own line
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    closing = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            closing = i
            break
    if closing is None:
        return {}
    fm_text = "\n".join(lines[1:closing])
    try:
        loaded = yaml.safe_load(fm_text)
    except yaml.YAMLError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _read_entry(path: Path, kind: MemoryKind, root: Path) -> MemoryEntry | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    fm = _parse_frontmatter(text)
    return MemoryEntry(
        kind=kind,
        name=path.stem,
        path=path,
        rel_path=str(path.relative_to(root)),
        frontmatter=fm,
    )


def scan_memory_dir(root: Path) -> list[MemoryEntry]:
    """Walk the memory directory and return entries grouped by kind.

    Missing subdirectories are tolerated — a freshly initialized dir might
    have only `skills/approved/` populated, that's fine. Hidden files and
    non-`.md` files are skipped.
    """
    if not root.exists():
        return []
    entries: list[MemoryEntry] = []

    # Skills live in ~/.lyre/skills/ (PI Agent Skills standard); Soul lives
    # in ~/.lyre/user.md. Memory keeps agent-authored knowledge files.
    layout: dict[str, MemoryKind] = {
        "facts": "fact",
    }
    for rel, kind in layout.items():
        d = root / rel
        if not d.is_dir():
            continue
        for path in sorted(d.iterdir()):
            if not path.is_file() or path.suffix != ".md":
                continue
            if path.name.startswith("."):
                continue
            # C3: per-agent private notebooks (facts/agent-<id>-notes.md) are
            # already pushed to their owner via the identity preamble and are
            # readable via read_memory. Excluding them from the shared index
            # stops every agent's "Available global memory" from listing every
            # OTHER agent's notebook — an unbounded per-prompt token tax that
            # grows with the system's lifetime agent population.
            if path.name.startswith("agent-") and path.name.endswith("-notes.md"):
                continue
            entry = _read_entry(path, kind, root)
            if entry is not None:
                entries.append(entry)
    return entries


def format_memory_index(
    entries: list[MemoryEntry], allowed_tools: list[str] | None = None
) -> str:
    """Render the entries as a markdown section to inject into system_prompt.

    Empty input → empty string so the caller can append unconditionally
    without worrying about a leftover heading.

    `allowed_tools` is the persona's allowlist. Used to phrase the "how to
    read these" hint with a tool the persona actually has — otherwise the
    model will hallucinate a tool that's not on its list (seen on
    DeepSeek v4 pro: dispatcher has no shell/python tools but was told to
    `shell_exec cat`, then gave up when the call was blocked).
    """
    if not entries:
        return ""

    groups: dict[MemoryKind, list[MemoryEntry]] = {
        "fact": [],
    }
    for e in entries:
        groups[e.kind].append(e)

    allow = set(allowed_tools or [])
    read_hint: str | None
    write_hint: str | None
    # Priority: read_memory (sandboxed) > python_exec > shell_exec > none.
    # We prefer the most-constrained tool when multiple are available, so
    # the model picks the safe path by default.
    if "read_memory" in allow:
        read_hint = (
            "Read the body of any entry with `read_memory(rel_path=\"<rel_path>\")` "
            "— sandboxed read-only access into ~/.lyre/memory/."
        )
        # read_memory is read-only by design; writes go through whichever
        # code tool the persona has (if any).
        if "python_exec" in allow:
            write_hint = (
                "Write proposals (Tier-matrix permitting) with `python_exec` "
                "file writes; see your persona prompt for what you may write to."
            )
        elif "shell_exec" in allow:
            write_hint = (
                "Write proposals (Tier-matrix permitting) with `shell_exec` "
                "redirects; see your persona prompt for what you may write to."
            )
        else:
            write_hint = None
    elif "python_exec" in allow:
        read_hint = (
            "Read the body of any entry with "
            "`python_exec` (e.g. `open(...).read()`) on `~/.lyre/memory/<rel_path>`."
        )
        write_hint = (
            "Write proposals (Tier-matrix permitting) with `python_exec` file writes; "
            "see your persona prompt for what you may write to and where."
        )
    elif "shell_exec" in allow:
        read_hint = "Read the body of any entry with `shell_exec cat ~/.lyre/memory/<rel_path>`."
        write_hint = (
            "Write proposals (Tier-matrix permitting) with `shell_exec` redirects; "
            "see your persona prompt for what you may write to and where."
        )
    else:
        # Persona has no file-access tools.
        read_hint = (
            "You do not have a file-access tool. This index is informational; "
            "do not try to read these files directly — dispatch a worker if "
            "the body is needed."
        )
        write_hint = None

    lines = ["## Available global memory", "", read_hint]
    if write_hint:
        lines.append(write_hint)

    if groups["fact"]:
        lines.append("")
        lines.append("### Facts")
        lines.extend(_format_fact_lines(groups["fact"]))

    return "\n".join(lines)


def _format_fact_lines(facts: list[MemoryEntry]) -> list[str]:
    """Render the facts list, GROUPED by the `type` frontmatter field so the
    menu stays scannable as facts accumulate. The field is already written by
    every fact author (analyst specs, shipped checklists, agent notes) — until
    now the renderer discarded it. Self-scaling: with a single type (or all
    untyped) this degrades to exactly the old flat list, so it's a no-op at low
    volume; the grouping only appears once there's more than one type to
    separate. No new field, no write path, no lifecycle — pure presentation."""
    by_type: dict[str, list[MemoryEntry]] = {}
    for e in facts:
        by_type.setdefault(e.type or "", []).append(e)
    # One bucket → flat (no subheaders): all-same-type or all-untyped.
    if len(by_type) <= 1:
        return [_format_line(e) for e in facts]
    out: list[str] = []
    # Typed groups first (alphabetical, stable); the untyped bucket ("") last
    # under a neutral label so it never jumps to the top by sort order.
    ordered = sorted(k for k in by_type if k) + ([""] if "" in by_type else [])
    for k in ordered:
        out.append(f"**{k or 'other'}**")
        out.extend(_format_line(e) for e in by_type[k])
    return out


def _format_line(e: MemoryEntry) -> str:
    head = f"- `{e.rel_path}`"
    desc = e.description
    scope = e.scope
    suffix_bits: list[str] = []
    if desc:
        suffix_bits.append(desc)
    if scope:
        suffix_bits.append(f"[scope: {scope}]")
    if suffix_bits:
        return head + " — " + "  ".join(suffix_bits)
    return head


def build_memory_index_for_prompt(
    root: Path,
    allowed_tools: list[str] | None = None,
    *,
    agent_id: str | None = None,
    persona_name: str | None = None,
) -> str:
    """One-shot helper used at wakeup start.

    C2: when agent_id/persona_name are given, facts are filtered by their
    scope — only globally-scoped facts (the default for every existing fact)
    plus facts scoped to this persona/agent are injected. Passing neither
    keeps the unfiltered behavior, so non-wakeup callers are unaffected.
    """
    entries = scan_memory_dir(root)
    if agent_id is not None or persona_name is not None:
        entries = [
            e
            for e in entries
            if e.applies_to(agent_id=agent_id, persona_name=persona_name)
        ]
    return format_memory_index(entries, allowed_tools=allowed_tools)


# ---------------------------------------------------------------------------
# Seed helpers — used by `lyre onboard` to put the directory skeleton in place.
# ---------------------------------------------------------------------------


SKELETON_SUBDIRS = (
    # Skills live in ~/.lyre/skills/ (see runtime.skills);
    # Owner identity lives in ~/.lyre/user.md (user-only-writable).
    "facts",
    # facts/archive/ — where an agent `mv`s superseded facts. Subdirs are skipped
    # by the non-recursive scan, so archived facts drop out of the injected menu
    # while staying grep/read_memory-able. Pre-created so `mv` just works (the
    # zero-code "eviction" the design intentionally keeps semantic, not mechanical).
    "facts/archive",
)


def ensure_skeleton(root: Path) -> list[Path]:
    """Make sure all the canonical subdirectories exist. Returns the list of
    paths that were created (empty if already present)."""
    created: list[Path] = []
    for rel in SKELETON_SUBDIRS:
        p = root / rel
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
            created.append(p)
    return created


def _shipped_checklists_dir() -> Path:
    # src/lyre/runtime/memory.py → ../data/checklists/
    return Path(__file__).resolve().parent.parent / "data" / "checklists"


def ensure_shipped_facts(root: Path) -> list[str]:
    """Copy shipped facts (currently: review checklists) into ``<root>/facts/``.

    Idempotent: a fact already present in the user's memory is never
    overwritten — owner edits stick across re-runs of ``lyre onboard``.
    Returns the list of basenames actually copied.
    """
    facts_dir = root / "facts"
    facts_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    src_dir = _shipped_checklists_dir()
    if not src_dir.is_dir():
        return copied
    for src in sorted(src_dir.glob("*.md")):
        target = facts_dir / src.name
        if target.exists():
            continue
        target.write_bytes(src.read_bytes())
        copied.append(src.name)
    return copied
