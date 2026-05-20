"""Skill loading & prompt formatting (PI Agent Skills standard, adapted).

Skills are agent capability packages — markdown instructions an agent loads
when a task matches the skill's description. Format borrowed from
github.com/earendil-works/pi (Apache 2.0) so we get the same tooling shape
PI-trained models already follow.

Layout:
    ~/.lyre/skills/
        approved/<skill-name>/SKILL.md   # active; injected into menu
        proposed/<skill-name>/SKILL.md   # under review; not injected
        archived/<skill-name>/SKILL.md   # historical; not injected

Each `<skill-name>/` directory may hold additional files (examples, test
scripts, helper data) which the skill body can reference by relative path.

Frontmatter (YAML, all keys optional unless marked):
    name:        slug, lowercase a-z 0-9 hyphen, ≤64; defaults to dir name
    description: REQUIRED, ≤1024 chars
    scope:       "global" | "persona=<name>" | "agent=<id>"  (default global)
    disable-model-invocation: bool (default false) — hidden from menu
    version:     int (audit only)
    proposed_by: agent_id (audit only)

Discovery: once SKILL.md is found in a dir, that dir is a skill root and
we don't recurse further (so a skill can ship a `node_modules/` without
collisions). Outside of skill roots we keep walking subdirectories.

Scope filter: when injecting for agent X (persona P), include skills with
    scope == "global"
    scope == "persona=P"
    scope == "agent=X"

Collisions: when two sources produce the same skill name, first wins and
the loser is logged as a diagnostic (a `SkillDiagnostic`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

# --------------------------------------------------------------------- Spec

MAX_NAME = 64
MAX_DESCRIPTION = 1024
NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
# Disallow consecutive hyphens (matches PI). The regex above doesn't catch
# them, so we check explicitly.

ScopeKind = Literal["global", "persona", "agent"]


# --------------------------------------------------------------------- Types


@dataclass(frozen=True)
class SkillScope:
    """Parsed `scope:` frontmatter.

    PI doesn't have this; Lyre adds it because we're multi-agent. A skill
    can be:
      - global: anyone can see/use it
      - persona=<name>: only agents of that persona role
      - agent=<id>: only this specific agent instance (the self-evolution
        case — an agent accumulates personal playbooks)
    """

    kind: ScopeKind = "global"
    target: str | None = None

    @classmethod
    def parse(cls, raw: str | None) -> SkillScope:
        if raw is None or raw == "" or raw == "global":
            return cls(kind="global", target=None)
        if "=" not in raw:
            raise ValueError(
                f"scope {raw!r}: expected 'global' or "
                f"'persona=<name>' or 'agent=<id>'"
            )
        kind, _, target = raw.partition("=")
        kind = kind.strip()
        target = target.strip()
        if kind not in ("persona", "agent"):
            raise ValueError(
                f"scope kind {kind!r}: must be 'global', 'persona', or 'agent'"
            )
        if not target:
            raise ValueError(f"scope {raw!r}: empty target after '='")
        return cls(kind=kind, target=target)  # type: ignore[arg-type]

    def applies_to(self, *, agent_id: str | None, persona_name: str | None) -> bool:
        if self.kind == "global":
            return True
        if self.kind == "persona":
            return persona_name is not None and self.target == persona_name
        if self.kind == "agent":
            return agent_id is not None and self.target == agent_id
        return False


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path                # absolute path to SKILL.md
    base_dir: Path            # absolute path to skill root dir (containing SKILL.md)
    source: str               # "approved" | "proposed" | "<rel path>" — for audit
    scope: SkillScope = SkillScope()
    disable_model_invocation: bool = False
    frontmatter: dict[str, Any] = field(default_factory=dict)


@dataclass
class SkillDiagnostic:
    level: Literal["warning", "collision"]
    message: str
    path: Path


@dataclass
class LoadResult:
    skills: list[Skill]
    diagnostics: list[SkillDiagnostic]


# --------------------------------------------------------------------- Parse


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split YAML frontmatter from body. Returns ({}, full_text) if absent."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    front = yaml.safe_load(text[4:end]) or {}
    if not isinstance(front, dict):
        return {}, text
    body = text[end + 5 :].lstrip("\n")
    return front, body


def _validate_name(name: str) -> list[str]:
    errs: list[str] = []
    if len(name) > MAX_NAME:
        errs.append(f"name length > {MAX_NAME}")
    if not NAME_RE.match(name):
        errs.append("name must match [a-z0-9](?:[a-z0-9-]*[a-z0-9])?")
    if "--" in name:
        errs.append("name must not contain consecutive hyphens")
    return errs


def _validate_description(desc: str) -> list[str]:
    errs: list[str] = []
    if not desc or not desc.strip():
        errs.append("description is required (must be non-empty)")
    elif len(desc) > MAX_DESCRIPTION:
        errs.append(f"description length > {MAX_DESCRIPTION}")
    return errs


# --------------------------------------------------------------------- Load


def _load_skill_file(
    path: Path, source: str
) -> tuple[Skill | None, list[SkillDiagnostic]]:
    diagnostics: list[SkillDiagnostic] = []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        diagnostics.append(
            SkillDiagnostic(level="warning", message=str(exc), path=path)
        )
        return None, diagnostics

    front, _body = _parse_frontmatter(raw)
    skill_dir = path.parent
    fallback_name = skill_dir.name
    name = (front.get("name") or fallback_name).strip()
    description = (front.get("description") or "").strip()

    # Description is hard-required: skip the skill but emit a warning.
    desc_errs = _validate_description(description)
    if desc_errs:
        for e in desc_errs:
            diagnostics.append(
                SkillDiagnostic(level="warning", message=e, path=path)
            )
        return None, diagnostics

    # Name errors are also fatal — collisions are harder to debug than typos.
    name_errs = _validate_name(name)
    if name_errs:
        for e in name_errs:
            diagnostics.append(
                SkillDiagnostic(
                    level="warning",
                    message=f"skill {name!r}: {e}",
                    path=path,
                )
            )
        return None, diagnostics

    try:
        scope = SkillScope.parse(
            front.get("scope") if isinstance(front.get("scope"), str) else None
        )
    except ValueError as exc:
        diagnostics.append(
            SkillDiagnostic(level="warning", message=str(exc), path=path)
        )
        return None, diagnostics

    return (
        Skill(
            name=name,
            description=description,
            path=path,
            base_dir=skill_dir,
            source=source,
            scope=scope,
            disable_model_invocation=bool(
                front.get("disable-model-invocation", False)
            ),
            frontmatter=front,
        ),
        diagnostics,
    )


def _scan_dir_for_skills(
    root: Path, source: str
) -> LoadResult:
    """Walk `root` looking for SKILL.md. Once found in a directory, that
    directory is a skill root — do not recurse further (so a skill can
    safely ship example/ test/ subdirs)."""
    skills: list[Skill] = []
    diagnostics: list[SkillDiagnostic] = []
    if not root.exists() or not root.is_dir():
        return LoadResult(skills, diagnostics)

    def walk(dir_: Path) -> None:
        skill_md = dir_ / "SKILL.md"
        if skill_md.is_file():
            skill, diags = _load_skill_file(skill_md, source)
            diagnostics.extend(diags)
            if skill is not None:
                skills.append(skill)
            return  # don't recurse: this dir is the skill root
        for child in sorted(dir_.iterdir()):
            if child.name.startswith("."):
                continue
            if child.is_dir():
                walk(child)

    walk(root)
    return LoadResult(skills, diagnostics)


# --------------------------------------------------------------------- API


def load_skills_for_context(
    lyre_home: Path,
    *,
    agent_id: str | None = None,
    persona_name: str | None = None,
) -> LoadResult:
    """Scan ~/.lyre/skills/ (the canonical location) for active skills.

    Only `approved/` skills are surfaced — proposed/ are under review,
    archived/ are dead. The returned skills are already filtered by scope
    against (agent_id, persona_name).

    Collisions: when two sources produce the same skill name, first wins;
    losers become 'collision' diagnostics. Order: first approved wins (FS
    iteration is sorted, deterministic).
    """
    skills_root = lyre_home / "skills"
    approved_dir = skills_root / "approved"
    raw = _scan_dir_for_skills(approved_dir, source="approved")

    # Scope filter
    in_scope: list[Skill] = [
        s
        for s in raw.skills
        if s.scope.applies_to(agent_id=agent_id, persona_name=persona_name)
    ]

    # Collision dedup
    seen: dict[str, Skill] = {}
    diagnostics = list(raw.diagnostics)
    for s in in_scope:
        if s.name in seen:
            diagnostics.append(
                SkillDiagnostic(
                    level="collision",
                    message=(
                        f"skill name {s.name!r} duplicated; first kept "
                        f"({seen[s.name].path}), this one ignored"
                    ),
                    path=s.path,
                )
            )
            continue
        seen[s.name] = s
    return LoadResult(skills=list(seen.values()), diagnostics=diagnostics)


def format_skills_for_prompt(skills: Iterable[Skill]) -> str:
    """Render skills as the PI Agent Skills XML block.

    Per the standard: location is included so the model loads body
    on demand (read tool / read_memory / etc.). Skills with
    disable-model-invocation are excluded — they exist on disk but the
    model only finds them via explicit `/skill:<name>` style invocation
    (which Lyre doesn't have yet; included for forward compat).
    """
    visible = [s for s in skills if not s.disable_model_invocation]
    if not visible:
        return ""
    lines = [
        "",
        "The following skills provide specialized instructions for "
        "specific tasks. Load a skill's file (read_memory / your file-read "
        "tool, depending on your allowlist) when the task matches its "
        "description. Paths inside a skill body are relative to the "
        "skill directory (parent of SKILL.md).",
        "",
        "<available_skills>",
    ]
    for s in visible:
        lines.append("  <skill>")
        lines.append(f"    <name>{_xml(s.name)}</name>")
        lines.append(f"    <description>{_xml(s.description)}</description>")
        lines.append(f"    <location>{_xml(str(s.path))}</location>")
        if s.scope.kind != "global":
            lines.append(f"    <scope>{_xml(s.scope.kind)}={_xml(s.scope.target or '')}</scope>")
        lines.append("  </skill>")
    lines.append("</available_skills>")
    return "\n".join(lines)


def _xml(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


# --------------------------------------------------------------------- Skeleton


def ensure_skills_skeleton(lyre_home: Path) -> list[Path]:
    """Create ~/.lyre/skills/{approved,proposed,archived}/ if missing."""
    created: list[Path] = []
    for sub in ("approved", "proposed", "archived"):
        d = lyre_home / "skills" / sub
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            created.append(d)
    return created
