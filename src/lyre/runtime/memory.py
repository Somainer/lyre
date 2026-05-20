"""Filesystem-backed memory layer.

Memory lives in `~/.lyre/memory/` (per Config.memory_path). Agent-write,
user-read-by-convention. Today the canonical category is:

    memory/
    └── facts/<topic>.md            ← agent-curated knowledge (kind, scope in frontmatter)

Skills are tracked separately under `~/.lyre/skills/`. Owner identity &
preferences (user-write, agent-read) live at `~/.lyre/user.md` and are
injected into every system prompt by `context.assemble_system_prompt`;
they never appear here.

No new tools. Agents read via `shell_exec cat` / `read_memory`, write via
`shell_exec` redirects / `python_exec`. At wakeup start, the scheduler scans
this dir, reads JUST the frontmatters, and injects an index ("Available
global memory") into the system prompt — so every agent sees what's
available without searching.

Frontmatter expected fields (all optional):
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
# Seed helpers — used by `lyre onboard` to put the directory skeleton in place.
# ---------------------------------------------------------------------------


SKELETON_SUBDIRS = (
    # Skills live in ~/.lyre/skills/ (see runtime.skills);
    # Owner identity lives in ~/.lyre/user.md (user-only-writable).
    "facts",
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
