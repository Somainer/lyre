"""Filesystem-backed PersonaRepository.

Personas are markdown files at ``<user_personas_dir>/<name>/identity.md``
with YAML frontmatter. This module is the SSOT-correct PersonaRepository
implementation that satisfies the ``PersonaRepository`` Protocol entirely
via file operations — no DB. ``personas`` is no longer a SQLite table.

Rationale (PR #25 follow-up to the persona/display_name SSOT shift):

Persona definitions live on disk and the owner (or worker proposals)
mutate files. Mirroring those files into a SQLite ``personas`` table
was historical accident — it added a sync direction (file → DB at
bootstrap) and made the DB authoritative for the
``proposed`` / ``approved`` / ``deprecated`` state machine, which
should be filesystem-native (same pattern as ``memory/skills/proposed``
and ``memory/skills/approved``).

Layout::

    ~/.lyre/personas/<name>/identity.md           # frontmatter + body
    ~/.lyre/personas/<name>/APPEND.md             # optional owner add-on
    ~/.lyre/personas/<name>/scratch.md            # owner-private notes

Status, display_name, kind, allowed_lyre_tools, model_preference,
metadata, proposed_by_task_id, reviewer all live in the identity.md
frontmatter. ``approve()`` rewrites that frontmatter in place;
``propose()`` writes a new identity.md with ``status: proposed``.

Read path is cheap: a few-dozen files, walked from disk on demand.
No cache layer for MVP — re-add if list_active ever shows up in a
profile.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from ..personas.seed import (
    _parse_markdown_with_frontmatter,
    load_persona_from_file,
)
from .models import Persona

if TYPE_CHECKING:
    from ..config import PersonaOverride

log = structlog.get_logger()


def _persona_identity_path(personas_dir: Path, name: str) -> Path:
    """``<personas_dir>/<name>/identity.md`` — the SSOT for a persona.

    The directory layout (one folder per persona) leaves room for
    companion files: ``APPEND.md`` for owner customisation,
    runtime-curated indexes, etc.
    """
    return personas_dir / name / "identity.md"


def _frontmatter_for(persona: Persona, **extra: Any) -> dict[str, Any]:
    """Build the frontmatter dict from a Persona model.

    Kept in one place so the propose / upsert / approve paths all
    produce the same shape. ``extra`` overlays additional fields
    (e.g. ``proposed_by_task_id`` only present on propose path).
    """
    front: dict[str, Any] = {
        "name": persona.name,
        "role_description": persona.role_description,
    }
    if persona.display_name is not None:
        front["display_name"] = persona.display_name
    front["kind"] = persona.kind
    if persona.allowed_lyre_tools:
        front["allowed_lyre_tools"] = list(persona.allowed_lyre_tools)
    if persona.model_preference is not None:
        front["model_preference"] = persona.model_preference
    front["status"] = persona.status
    if persona.proposed_by_task_id is not None:
        front["proposed_by_task_id"] = persona.proposed_by_task_id
    if persona.reviewer is not None:
        front["reviewer"] = persona.reviewer
    if persona.metadata is not None:
        front["metadata"] = persona.metadata
    front.update(extra)
    return front


def _serialize_persona(persona: Persona) -> str:
    """Render a Persona back to identity.md text (frontmatter + body)."""
    import yaml

    front = _frontmatter_for(persona)
    body = (persona.system_prompt or "").lstrip("\n")
    return (
        "---\n"
        + yaml.safe_dump(front, sort_keys=False, allow_unicode=True)
        + "---\n\n"
        + body
    )


class FilesystemPersonaRepository:
    """``PersonaRepository`` impl backed by ``<user_personas_dir>``.

    ``persona_overrides`` (from ``config.toml [personas.<name>]``) are
    applied lazily on every read — there's no merged copy persisted
    anywhere, so identity.md stays the unambiguous source of truth for
    the role and config.toml for the deployment knobs (model_preference,
    allowed_lyre_tools).
    """

    def __init__(
        self,
        personas_dir: Path,
        persona_overrides: dict[str, PersonaOverride] | None = None,
    ):
        self.personas_dir = Path(personas_dir)
        self._overrides = persona_overrides or {}

    def _apply_overrides(self, persona: Persona) -> Persona:
        ov = self._overrides.get(persona.name)
        if ov is None:
            return persona
        updates: dict[str, Any] = {}
        if ov.model_preference is not None:
            updates["model_preference"] = ov.model_preference
        if ov.allowed_lyre_tools is not None:
            updates["allowed_lyre_tools"] = list(ov.allowed_lyre_tools)
        if not updates:
            return persona
        return persona.model_copy(update=updates)

    async def get(self, name: str) -> Persona | None:
        path = _persona_identity_path(self.personas_dir, name)
        if not path.is_file():
            # Fall back to the legacy flat layout in case someone has
            # ``<name>.md`` at the personas dir root (test fixtures,
            # pre-directory-layout user installs). Same precedence as
            # ``discover_persona_files``.
            flat = self.personas_dir / f"{name}.md"
            if flat.is_file():
                path = flat
            else:
                return None
        try:
            return self._apply_overrides(load_persona_from_file(path))
        except (OSError, ValueError, KeyError) as exc:
            log.warning(
                "persona_load_failed",
                name=name, path=str(path), error=str(exc),
            )
            return None

    async def list_active(self, status: str = "approved") -> list[Persona]:
        """Walk ``personas_dir``, return personas whose frontmatter
        ``status`` matches (default ``approved``).

        Directories whose ``identity.md`` is missing or malformed are
        skipped with a warning rather than raised — a half-edited
        persona shouldn't kill the whole list.
        """
        out: list[Persona] = []
        if not self.personas_dir.is_dir():
            return out
        for child in sorted(self.personas_dir.iterdir()):
            identity = child / "identity.md" if child.is_dir() else None
            # Legacy flat ``<name>.md`` files at the personas root.
            if identity is None and child.suffix == ".md" and child.is_file():
                identity = child
            if identity is None or not identity.is_file():
                continue
            try:
                p = load_persona_from_file(identity)
            except (OSError, ValueError, KeyError) as exc:
                log.warning(
                    "persona_skip_malformed",
                    path=str(identity), error=str(exc),
                )
                continue
            if p.status == status:
                out.append(self._apply_overrides(p))
        return out

    async def upsert(self, persona: Persona) -> None:
        """Write the persona's identity.md. Creates the directory if
        missing. Existing companion files (APPEND.md, etc.) are
        untouched."""
        path = _persona_identity_path(self.personas_dir, persona.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_serialize_persona(persona), encoding="utf-8")

    async def propose(
        self,
        name: str,
        role_description: str,
        system_prompt: str,
        allowed_lyre_tools: list[str],
        source_task_id: str,
        **kwargs: Any,
    ) -> None:
        """Write a new persona file with ``status: proposed``.

        Reviewer flips the status to ``approved`` (or ``deprecated``)
        via ``approve()`` later — same pattern as the
        ``memory/skills/proposed/`` ↔ ``memory/skills/approved/`` flow
        for skills.
        """
        persona = Persona(
            name=name,
            role_description=role_description,
            system_prompt=system_prompt,
            allowed_lyre_tools=allowed_lyre_tools,
            model_preference=kwargs.get("model_preference"),
            status="proposed",
            proposed_by_task_id=source_task_id,
            kind=kwargs.get("kind", "spawn_only"),
            metadata=kwargs.get("metadata"),
        )
        await self.upsert(persona)

    async def approve(
        self,
        persona_name: str,
        reviewer: str,
        status: str,
        comment: str | None = None,
    ) -> None:
        """Flip the persona's frontmatter ``status`` and record the
        reviewer. The body is untouched. Idempotent on already-applied
        decisions."""
        del comment  # not persisted (no field for it); could live in metadata
        path = _persona_identity_path(self.personas_dir, persona_name)
        if not path.is_file():
            raise FileNotFoundError(
                f"no persona file at {path}; cannot approve {persona_name!r}"
            )
        text = path.read_text(encoding="utf-8")
        front, body = _parse_markdown_with_frontmatter(text)
        front["status"] = status
        front["reviewer"] = reviewer
        import yaml
        new_text = (
            "---\n"
            + yaml.safe_dump(front, sort_keys=False, allow_unicode=True)
            + "---\n\n"
            + body.lstrip("\n")
        )
        path.write_text(new_text, encoding="utf-8")
