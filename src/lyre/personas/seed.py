"""Seed the 6 MVP personas into the database from markdown files.

Each persona is a Markdown file with YAML frontmatter. Run once on `lyre init`,
idempotent on re-runs (upserts by name).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ..persistence.models import Persona
from ..persistence.repositories import AgentRepository, PersonaRepository

# Agent ids that always exist after `lyre init`. These are the long-lived
# "well-known" agents users (owner) and the CLI default `lyre send leader ...`
# expect to be addressable from day one. Workers spawn on demand.
DEFAULT_AGENTS: tuple[tuple[str, str], ...] = (
    ("owner", "owner"),   # (agent_id, persona_name)
    ("leader", "leader"),
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


def discover_persona_files() -> list[Path]:
    """All *.md files in src/lyre/personas/ except this module's own files."""
    here = Path(__file__).parent
    excluded = {"__init__.py", "seed.py"}
    return sorted(
        p for p in here.glob("*.md") if p.name not in excluded
    )


async def seed_personas(repo: PersonaRepository) -> list[str]:
    """Upsert all persona files into DB. Returns list of persona names seeded."""
    seeded: list[str] = []
    for path in discover_persona_files():
        persona = load_persona_from_file(path)
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
                created_by="system:init",
            )
            created.append(agent_id)
        if memory_root is not None:
            ensure_agent_notes_file(memory_root, agent_id)
    return created


def ensure_agent_notes_file(memory_root: Path, agent_id: str) -> Path:
    """Create `<memory_root>/facts/agent-<id>-notes.md` if it doesn't yet
    exist. Returns the absolute path either way. Used by `lyre init`
    (via seed_default_agents) and by the `create_agent` tool.
    """
    facts_dir = memory_root / "facts"
    facts_dir.mkdir(parents=True, exist_ok=True)
    path = facts_dir / f"agent-{agent_id}-notes.md"
    if path.exists():
        return path
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    seed = f"""---
name: agent-{agent_id}-notes
description: {agent_id}'s private notebook for cross-wakeup memory.
type: agent_notes
agent_id: {agent_id}
created: {now}
---

# Notes for {agent_id}

This is your private notebook. Every wakeup is stateless — anything you
want to remember across wakeups goes here. The identity preamble points
you at this file by path; you read it with `read_memory(
"facts/agent-{agent_id}-notes.md")` and append to it with shell_exec /
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
