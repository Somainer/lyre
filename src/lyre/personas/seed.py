"""Persona file loading + bootstrap-agent seeding.

After ``lyre onboard`` (or the first ``lyre serve``), the single source of
truth for personas is ``~/.lyre/personas/<name>/identity.md`` — read
directly by ``FilesystemPersonaRepository``. Shipped personas at
``src/lyre/personas/*.md`` are only used to populate that directory on
first bootstrap (``ensure_user_personas``); once they're there, the user
can edit / rename / delete freely without further surprise from the
runtime.

Two layouts are supported in ``~/.lyre/personas/`` (directory wins if
both exist for the same name):

  * Directory:  ``<name>/identity.md`` (frontmatter + system prompt)
                — preferred. Allows companion files like APPEND.md.
  * Flat:       ``<name>.md`` — legacy / minimal-fuss alternative.

Per-field overrides from ``Config.persona_overrides`` (loaded from
``config.toml [personas.<name>]``) are applied on read by the repo,
not baked into the files.

Persona's ``kind`` frontmatter field drives bootstrap-agent seeding:
  - ``singleton``   — seed one agent; create_agent refuses (owner, dispatcher)
  - ``seeded``      — seed one agent; create_agent allowed for parallel
                      instances (analyst, reviewer)
  - ``spawn_only``  — never auto-seed; create_agent required (worker-maintainer)
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

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
        # ``needs_worktree`` in frontmatter is silently ignored (kept for
        # back-compat with user-edited identity.md files); every LLM
        # persona unconditionally gets an empty-tmpdir worktree now,
        # and git provisioning is per-task (TaskSpec.git_context).
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


def shipped_persona_names() -> list[str]:
    """Names of every persona Lyre ships (one per shipped ``<name>.md``)."""
    return sorted(p.stem for p in _shipped_persona_files())


def refresh_user_persona(
    user_personas_dir: Path, name: str, *, backup: bool = True
) -> tuple[Path, Path | None]:
    """Re-copy the shipped ``<name>.md`` over ``<user>/<name>/identity.md``.

    ``ensure_user_personas`` deliberately never overwrites identity.md (it's the
    user SSOT), so a shipped persona EDIT never reaches an already-onboarded
    install on its own. This pulls one in on demand, backing up the current
    identity.md first (unless ``backup=False``) so local edits survive. Personas
    are read straight from the filesystem, so the change is live on the next
    wakeup. Returns ``(identity_path, backup_path_or_None)``; raises
    ``KeyError`` if no shipped persona has that name.
    """
    shipped = {p.stem: p for p in _shipped_persona_files()}
    src = shipped.get(name)
    if src is None:
        raise KeyError(name)
    dest_dir = user_personas_dir / name
    dest_dir.mkdir(parents=True, exist_ok=True)
    identity = dest_dir / "identity.md"
    bak: Path | None = None
    if identity.exists() and backup:
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        bak = dest_dir / f"identity.md.bak-{ts}"
        shutil.copy(identity, bak)
    shutil.copy(src, identity)
    # Heal the companion APPEND.md slot if missing (mirrors ensure_user_personas).
    append_target = dest_dir / "APPEND.md"
    if not append_target.exists():
        append_target.write_text(APPEND_TEMPLATE, encoding="utf-8")
    return identity, bak


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


async def seed_default_agents(
    persona_repo: PersonaRepository,
    agent_repo: AgentRepository,
    memory_root: Path | None = None,
) -> list[str]:
    """Ensure one live bootstrap agent exists for every persona whose
    ``kind`` is ``singleton`` or ``seeded``. Their addressable id comes
    from the persona's ``display_name`` (or ``name`` if unset).

    Behaviour, three cases:

      * agent_id doesn't exist  → create (parent=None, idle)
      * agent_id exists, idle   → no-op
      * agent_id exists, archived → **unarchive** (revive)

    The unarchive path is the self-healing recovery from owner-typo
    cascades. Previously a separate ``archive_stale_bootstrap_agents``
    pass silently killed any parentless agent whose id didn't match
    the persona's CURRENT display_name — so a fat-fingered identity.md
    edit + restart would archive the live agent; correcting the edit +
    restart would archive the freshly-seeded one too. Two typos in a
    row wiped every dispatcher / analyst / reviewer. The auto-archive
    pass has been removed: changing display_name is now treated as
    declaring a NEW agent, never as a rename. Owners clean up the old
    agent manually (``lyre agent archive <id>`` or via the dashboard).

    ``spawn_only`` personas (workers) are skipped — the dispatcher
    creates instances of those on demand via the ``create_agent`` tool.

    Notes file pre-creation runs unconditionally so re-onboards heal
    any missing companion files.

    Returns the list of agent ids that were newly created OR
    unarchived (i.e. agents the runtime brought to life this call).
    """
    personas = await persona_repo.list_active()
    seeded: list[str] = []
    for p in personas:
        if p.kind == "spawn_only":
            continue
        agent_id = p.display_name or p.name

        existing = await agent_repo.get(agent_id)
        if existing is None:
            await agent_repo.create(
                agent_id=agent_id,
                persona_name=p.name,
                parent_agent_id=None,  # bootstrap root
            )
            seeded.append(agent_id)
        elif existing.status == "archived":
            # The display_name flipped back to a previously-archived id
            # (typical owner-rollback scenario). Bring it back to life
            # with full mail / task history intact — don't create a
            # duplicate row.
            if await agent_repo.unarchive(agent_id):
                seeded.append(agent_id)
        # else: exists + active → no-op

        if memory_root is not None:
            ensure_agent_notes_file(memory_root, agent_id)
            ensure_agent_scratchpad_file(memory_root, agent_id)
    return seeded


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


def _flatten_agent_id(agent_id: str) -> str:
    return agent_id.replace("/", "-")


def scratchpad_rel_path(agent_id: str) -> str:
    """Path of the agent's scratchpad relative to ``memory_root``.

    Used both for ``read_memory(rel_path)`` and inside the
    ``update_scratchpad`` tool's sandbox check. Centralised so the
    runtime and the tool never disagree on where a scratchpad lives.
    """
    return f"scratchpad/{_flatten_agent_id(agent_id)}.md"


def ensure_agent_scratchpad_file(memory_root: Path, agent_id: str) -> Path:
    """Create ``<memory_root>/scratchpad/<flat-id>.md`` if absent.
    Returns the absolute path either way.

    Scratchpad is the agent's working / short-term memory — distinct
    from ``facts/agent-<id>-notes.md`` (long-term notes that the
    runtime also appends auto-summary entries to). Owner-curated
    knowledge stays in ``facts/<topic>.md``. Three separate purposes,
    three separate buckets under ``memory/``.

    Seeded empty (no frontmatter, no template) on purpose: the model
    owns this file end-to-end. Any starter text would tempt models to
    feel constrained by the template instead of using it as a clean
    workspace.
    """
    scratchpad_dir = memory_root / "scratchpad"
    scratchpad_dir.mkdir(parents=True, exist_ok=True)
    flat_id = _flatten_agent_id(agent_id)
    path = scratchpad_dir / f"{flat_id}.md"
    if not path.exists():
        path.write_text("", encoding="utf-8")
    return path
