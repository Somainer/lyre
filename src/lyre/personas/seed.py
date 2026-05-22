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

Persona's ``kind`` frontmatter field drives bootstrap-agent seeding:
  - ``singleton``   — seed one agent; create_agent refuses (owner, dispatcher)
  - ``seeded``      — seed one agent; create_agent allowed for parallel
                      instances (analyst, reviewer)
  - ``spawn_only``  — never auto-seed; create_agent required (worker-maintainer)

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
        display_name=front.get("display_name"),  # None → label falls back to name
        kind=front.get("kind", "spawn_only"),
        role_description=front["role_description"],
        system_prompt=body,
        allowed_lyre_tools=front.get("allowed_lyre_tools", []) or [],
        model_preference=front.get("model_preference"),
        needs_worktree=bool(front.get("needs_worktree", True)),
        status=front.get("status", "approved"),
        metadata=front.get("metadata"),
    )


SHIPPED_PERSONAS_EXCLUDED = {"__init__.py", "seed.py"}

# APPEND.md template: empty so the prompt-assembly path naturally
# skips it. Existing here as a marker file the owner can edit to inject
# voice / style without touching identity.md. Seeded alongside every
# user-personas/<name>/identity.md so the mechanism is discoverable.
APPEND_TEMPLATE = ""


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

    Each shipped ``<name>.md`` becomes ``<user_personas_dir>/<name>/identity.md``,
    accompanied by an empty ``APPEND.md`` so the discoverable customization
    slot exists in the owner's filesystem from day one.

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
            # Persona itself stays as-is; still make sure APPEND.md
            # exists alongside an existing directory layout so re-onboards
            # heal missing companion files without touching content.
            append_target = user_personas_dir / name / "APPEND.md"
            if dir_target.exists() and not append_target.exists():
                append_target.write_text(APPEND_TEMPLATE, encoding="utf-8")
            continue
        dir_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dir_target)
        # Touch APPEND.md alongside so the owner sees both files and
        # learns the customization slot exists. Empty content → runtime
        # appends an empty string, which is harmless in prompt assembly.
        (dir_target.parent / "APPEND.md").write_text(
            APPEND_TEMPLATE, encoding="utf-8"
        )
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
    ``system_prompt``, ``role_description``, ``display_name``, and ``kind``
    are NEVER touched by this path — those are identity facts that live
    in identity.md as the single source of truth. config.toml override
    is reserved for deployment-level runtime knobs (model_preference,
    allowed_lyre_tools).
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
    persona_repo: PersonaRepository,
    agent_repo: AgentRepository,
    memory_root: Path | None = None,
) -> list[str]:
    """Seed one bootstrap agent for every persona whose ``kind`` is
    ``singleton`` or ``seeded`` — those declare "I deserve a default
    standing instance the owner can mailbox out of the box".

    Each such agent's id comes from the persona's ``display_name`` (or
    ``name`` if display_name is unset). That's the one-time-at-seed-time
    copy: once the agent row exists, its id is immutable even if the
    owner later re-edits identity.md's display_name (mail rows already
    reference it via FK).

    ``spawn_only`` personas (workers) are skipped here — the dispatcher
    creates instances of those on demand via the ``create_agent`` tool.

    Idempotent: agents that already exist are not re-created. Notes file
    pre-creation runs unconditionally (so re-onboards heal missing files).

    Returns the list of newly-created agent ids.
    """
    personas = await persona_repo.list_active()
    created: list[str] = []
    for p in personas:
        if p.kind == "spawn_only":
            continue
        agent_id = p.display_name or p.name
        if not await agent_repo.exists(agent_id):
            await agent_repo.create(
                agent_id=agent_id,
                persona_name=p.name,
                parent_agent_id=None,  # bootstrap root
            )
            created.append(agent_id)
        if memory_root is not None:
            ensure_agent_notes_file(memory_root, agent_id)
    await archive_stale_bootstrap_agents(persona_repo, agent_repo)
    return created


async def archive_stale_bootstrap_agents(
    persona_repo: PersonaRepository,
    agent_repo: AgentRepository,
) -> list[str]:
    """Soft-archive bootstrap-seeded agents whose persona's display_name
    has been re-pointed.

    Owner edits ``identity.md`` to change ``display_name`` from
    ``dispatcher`` to ``luna``. We seed ``luna``; the old ``dispatcher``
    agent row should retire (mail history preserved, but mail can no
    longer go to it).

    Eligibility: every parentless agent (``parent_agent_id IS NULL``)
    whose persona is still ``singleton``/``seeded`` but whose id doesn't
    match the persona's CURRENT ``display_name``. User-spawned agents
    (``parent_agent_id`` non-NULL) are untouched.

    Returns the list of archived agent ids.
    """
    personas = {p.name: p for p in await persona_repo.list_active()}

    archived: list[str] = []
    for agent in await agent_repo.list_all(include_archived=False):
        if agent.parent_agent_id is not None:
            continue  # user-spawned, leave alone
        if agent.status == "archived":
            continue
        p = personas.get(agent.persona_name)
        if p is None or p.kind == "spawn_only":
            continue  # not currently a bootstrap-eligible persona
        expected_id = p.display_name or p.name
        if agent.id == expected_id:
            continue
        ok = await agent_repo.archive(agent.id)
        if ok:
            archived.append(agent.id)
    return archived


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
