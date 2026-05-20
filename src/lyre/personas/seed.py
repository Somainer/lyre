"""Seed personas into the database from markdown files.

After ``lyre onboard`` (or the first ``lyre serve``), the single source of
truth for personas is ``~/.lyre/personas/``. Shipped personas at
``src/lyre/personas/*.md`` are only used to populate that directory on
bootstrap — once they're there, the user can edit / rename / delete
freely without further surprise from the runtime.

Two layouts are supported in ``~/.lyre/personas/`` (directory wins if
both exist for the same name):

  * Directory:  ``<name>/identity.md`` (frontmatter + system prompt)
                — preferred. Allows companion files like APPEND.md.
  * Flat:       ``<name>.md`` — legacy / minimal-fuss alternative.

Plus optional per-field overrides from ``Config.persona_overrides`` (loaded
from ``config.toml [personas.<name>]``), applied last on whichever file won.

Idempotent on re-runs (upserts by name).
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from ..config import PersonaOverride
from ..persistence.models import Persona
from ..persistence.repositories import AgentRepository, PersonaRepository

# Agent ids that always exist after `lyre onboard`. These are the long-lived
# "well-known" agents users (owner) and the CLI default `lyre send leader ...`
# expect to be addressable from day one. Workers spawn on demand.
#
# `reviewer-1` is seeded so worker can mailbox_send directly to it without
# going through leader — auto-wake-on-mail picks it up. Multiple reviewers
# can be created later via create_agent if parallel review throughput is
# needed.
DEFAULT_AGENTS: tuple[tuple[str, str], ...] = (
    ("owner", "owner"),   # (agent_id, persona_name)
    ("leader", "leader"),
    ("reviewer-1", "reviewer"),
)


def _parse_markdown_with_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split YAML frontmatter and markdown body. Returns ({}, full_text) if no frontmatter."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    front_text = text[4:end]
    body = text[end + 5 :].lstrip("\n")
    front = yaml.safe_load(front_text) or {}
    return front, body


def load_persona_from_file(path: Path) -> Persona:
    raw = path.read_text(encoding="utf-8")
    front, body = _parse_markdown_with_frontmatter(raw)

    return Persona(
        name=front["name"],
        role_description=front["role_description"],
        system_prompt=body,
        allowed_lyre_tools=front.get("allowed_lyre_tools", []) or [],
        model_preference=front.get("model_preference"),
        needs_worktree=bool(front.get("needs_worktree", True)),
        status=front.get("status", "approved"),
        metadata=front.get("metadata"),
    )


SHIPPED_PERSONAS_EXCLUDED = {"__init__.py", "seed.py"}


def _shipped_personas_dir() -> Path:
    return Path(__file__).parent


def _shipped_persona_files() -> list[Path]:
    return sorted(
        p for p in _shipped_personas_dir().glob("*.md")
        if p.name not in SHIPPED_PERSONAS_EXCLUDED
    )


def ensure_user_personas(
    user_personas_dir: Path, *, overwrite: bool = False
) -> list[str]:
    """Copy shipped personas into ``user_personas_dir`` using directory layout.

    Each shipped ``<name>.md`` becomes ``<user_personas_dir>/<name>/identity.md``.
    Skips any name that already has either layout present in the user dir
    (so user edits, renames, and deletions are preserved across re-runs).

    Returns the list of names actually copied.
    """
    user_personas_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for src in _shipped_persona_files():
        name = src.stem
        flat_target = user_personas_dir / f"{name}.md"
        dir_target = user_personas_dir / name / "identity.md"
        if not overwrite and (flat_target.exists() or dir_target.exists()):
            continue
        dir_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dir_target)
        copied.append(name)
    return copied


def discover_persona_files(user_personas_dir: Path | None = None) -> list[Path]:
    """All persona ``*.md`` files Lyre will load.

    In production, ``bootstrap_runtime`` calls :func:`ensure_user_personas`
    first so ``~/.lyre/personas/`` is populated; ``user_personas_dir`` is the
    single source of truth. Resolution per name (directory wins over flat
    if both exist):

      * ``<user_personas_dir>/<name>/identity.md``  ← preferred
      * ``<user_personas_dir>/<name>.md``           ← legacy / minimal

    Fallback for callers that bypass bootstrap (mostly test fixtures): if
    ``user_personas_dir`` is None or empty, return the shipped files
    directly.
    """
    if user_personas_dir is not None and user_personas_dir.is_dir():
        by_name: dict[str, Path] = {}

        # Flat <name>.md first (gets overridden by directory layout below).
        for p in sorted(user_personas_dir.glob("*.md")):
            if p.name.startswith("."):
                continue
            by_name[p.stem] = p

        # Directory layout: <name>/identity.md
        for d in sorted(user_personas_dir.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            identity = d / "identity.md"
            if identity.is_file():
                by_name[d.name] = identity

        if by_name:
            return sorted(
                by_name.values(),
                key=lambda p: (p.parent.name if p.name == "identity.md" else p.stem),
            )

    # Fallback: shipped personas (test fixtures only — production paths
    # always populate user_personas_dir first via bootstrap_runtime).
    return _shipped_persona_files()


def _apply_field_override(persona: Persona, override: PersonaOverride) -> Persona:
    """Apply single-field ``[personas.<name>]`` overrides from config.toml.

    Each field replaces the persona's value if non-None; the persona's
    ``system_prompt`` and ``role_description`` are never touched by this
    path (use a whole-file override in ``~/.lyre/personas/`` for that).
    """
    updates: dict[str, Any] = {}
    if override.model_preference is not None:
        updates["model_preference"] = override.model_preference
    if override.allowed_lyre_tools is not None:
        updates["allowed_lyre_tools"] = list(override.allowed_lyre_tools)
    if not updates:
        return persona
    return persona.model_copy(update=updates)


async def seed_personas(
    repo: PersonaRepository,
    user_personas_dir: Path | None = None,
    persona_overrides: dict[str, PersonaOverride] | None = None,
) -> list[str]:
    """Upsert all persona files into DB. Returns list of persona names seeded.

    Lookup order per name: ``user_personas_dir/<name>.md`` > shipped.
    Per-field overrides from ``persona_overrides`` apply last.
    """
    overrides = persona_overrides or {}
    seeded: list[str] = []
    for path in discover_persona_files(user_personas_dir):
        persona = load_persona_from_file(path)
        if persona.name in overrides:
            persona = _apply_field_override(persona, overrides[persona.name])
        await repo.upsert(persona)
        seeded.append(persona.name)
    return seeded


async def seed_default_agents(
    repo: AgentRepository, memory_root: Path | None = None
) -> list[str]:
    """Ensure the well-known `owner` and `leader` agents exist.

    Idempotent: skips any agent that already exists. Workers are NOT seeded —
    leader (or owner via CLI) creates them on demand. If `memory_root` is
    provided, a notes file is pre-created at
    `<memory_root>/facts/agent-<id>-notes.md` for each seeded agent — this
    is the "agent's private scratchpad for cross-wakeup memory" that the
    identity preamble teaches about (Codex-style: pre-create the path so
    the agent naturally `ls` / `cat`s it).
    """
    created: list[str] = []
    for agent_id, persona_name in DEFAULT_AGENTS:
        if not await repo.exists(agent_id):
            await repo.create(
                agent_id=agent_id,
                persona_name=persona_name,
                parent_agent_id=None,  # bootstrap roots
            )
            created.append(agent_id)
        if memory_root is not None:
            ensure_agent_notes_file(memory_root, agent_id)
    return created


def ensure_agent_notes_file(memory_root: Path, agent_id: str) -> Path:
    """Create `<memory_root>/facts/agent-<flattened-id>-notes.md` if
    it doesn't yet exist. Returns the absolute path either way.

    `persona/name` ids would otherwise create a directory layer
    (`agent-worker/foo-notes.md` ≠ a single file); we flatten `/` to `-`
    in the filename so every agent's notes live as one flat file under
    facts/. The frontmatter still records the unflattened `agent_id`.
    """
    facts_dir = memory_root / "facts"
    facts_dir.mkdir(parents=True, exist_ok=True)
    flat_id = agent_id.replace("/", "-")
    path = facts_dir / f"agent-{flat_id}-notes.md"
    if path.exists():
        return path
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    seed = f"""---
name: agent-{flat_id}-notes
description: {agent_id}'s private notebook for cross-wakeup memory.
type: agent_notes
agent_id: {agent_id}
created: {now}
---

# Notes for {agent_id}

This is your private notebook. Every wakeup is stateless — anything you
want to remember across wakeups goes here. The identity preamble points
you at this file by path; you read it with `read_memory(
"facts/agent-{flat_id}-notes.md")` and append to it with shell_exec /
python_exec.

Suggested sections (free-form — edit as you like):

## Open threads
- (e.g. "owner asked me to investigate /pi on 2026-05-18 — still pending")

## Owner preferences / gotchas
- (things you've learned about how the owner likes things done)

## Delegated tasks (waiting on)
- (task_id → agent → what's expected back)

## Decisions / facts worth remembering
"""
    path.write_text(seed, encoding="utf-8")
    return path
