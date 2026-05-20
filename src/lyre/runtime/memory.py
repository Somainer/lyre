"""Filesystem-backed memory layer.

Memory lives in `~/.lyre/memory/` (per Config.memory_path). Three categories,
each a directory of `<name>.md` files with YAML frontmatter + markdown body:

    memory/
    ├── skills/
    │   ├── approved/<name>.md      ← reviewer-skill has promoted here
    │   └── proposed/<name>.md      ← worker's draft, awaiting review
    ├── facts/<topic>.md            ← global facts (kind, scope in frontmatter)
    └── personas/<name>.md          ← persona profiles incl. owner Soul

No new tools. Agents read via `shell_exec cat`, write via `shell_exec` redirects.
At wakeup start, Scheduler scans the dir, reads JUST the frontmatters, and
injects an index ("MEMORY index") into the system prompt — so every agent
sees what's available without searching. This is the anti-spike / anti-
non-convergence mechanism (Hermes/Pi style menu-of-skills).

Frontmatter expected fields (all optional):
    description  : one-line summary used in the index
    scope        : free-form string ("lisa-lang", "global", ...)
    triggers     : list[str], when the skill applies
    kind         : for facts, the fact category ("repo_info", "api_quirk", ...)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

MemoryKind = Literal["fact", "persona"]


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

    # Skills live in ~/.lyre/skills/ now (B1: PI Agent Skills standard),
    # not under memory/. Memory keeps the durable knowledge files:
    # personas (Souls) and facts.
    layout: dict[str, MemoryKind] = {
        "facts": "fact",
        "personas": "persona",
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
    DeepSeek v4 pro: leader has no shell/python tools but was told to
    `shell_exec cat`, then gave up when the call was blocked).
    """
    if not entries:
        return ""

    groups: dict[MemoryKind, list[MemoryEntry]] = {
        "fact": [],
        "persona": [],
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
        for e in groups["fact"]:
            lines.append(_format_line(e))

    if groups["persona"]:
        lines.append("")
        lines.append("### Persona profiles")
        for e in groups["persona"]:
            lines.append(_format_line(e))

    return "\n".join(lines)


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
    root: Path, allowed_tools: list[str] | None = None
) -> str:
    """One-shot helper used at wakeup start."""
    return format_memory_index(scan_memory_dir(root), allowed_tools=allowed_tools)


# ---------------------------------------------------------------------------
# Seed helpers — used by `lyre init` to put the directory skeleton in place
# plus a placeholder owner Soul so the index isn't empty on day one.
# ---------------------------------------------------------------------------


SKELETON_SUBDIRS = (
    # Skills moved to ~/.lyre/skills/ in B1 — see runtime.skills.
    "facts",
    "personas",
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


OWNER_SOUL_PLACEHOLDER = """---
description: Owner preferences and style — read on every wakeup
kind: persona_profile
---

# Owner Soul

This is a placeholder. The summary-agent will populate it over time as
patterns emerge from owner feedback.

Default conventions until updated:

- Communication style: concise, technical, no fluff
- Code style: prefer testability and clarity over cleverness
- Decision philosophy: prefer simpler universal mechanisms over specialized tools
- When unsure: ask via `mailbox_send to=owner urgency=blocker`
"""


def write_default_owner_soul(root: Path) -> Path:
    """Drop the placeholder owner Soul into `personas/owner.md` if absent."""
    target = root / "personas" / "owner.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text(OWNER_SOUL_PLACEHOLDER, encoding="utf-8")
    return target
