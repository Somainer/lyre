"""Unit tests for the PI-style skill loader (B1 + B2).

Covers:
  - Directory-per-skill discovery (SKILL.md inside a dir is the skill root).
  - Strict name + description validation (matches PI agent-skills spec).
  - Scope filtering: global / persona=<name> / agent=<id>.
  - Collision detection: first wins, loser becomes a diagnostic.
  - XML format injection (the <available_skills> block).
  - disable-model-invocation hides a skill from the menu.
"""

from __future__ import annotations

from pathlib import Path

from lyre.runtime.skills import (
    SkillScope,
    ensure_skills_skeleton,
    format_skills_for_prompt,
    load_skills_for_context,
)


def _write_skill(
    root: Path, name: str, *,
    state: str = "approved",
    description: str = "describe me",
    scope: str | None = None,
    disable: bool = False,
    extra_files: dict[str, str] | None = None,
) -> Path:
    """Lay out a PI-style skill dir under root/skills/<state>/<name>/."""
    d = root / "skills" / state / name
    d.mkdir(parents=True, exist_ok=True)
    fm = [f"name: {name}", f'description: "{description}"']
    if scope is not None:
        fm.append(f"scope: {scope}")
    if disable:
        fm.append("disable-model-invocation: true")
    body = "---\n" + "\n".join(fm) + "\n---\n\nbody text"
    (d / "SKILL.md").write_text(body, encoding="utf-8")
    for fname, content in (extra_files or {}).items():
        (d / fname).write_text(content, encoding="utf-8")
    return d


# ---------------------------------------------------------------------------
# Scope parsing
# ---------------------------------------------------------------------------


def test_scope_parse_global_default() -> None:
    assert SkillScope.parse(None).kind == "global"
    assert SkillScope.parse("").kind == "global"
    assert SkillScope.parse("global").kind == "global"


def test_scope_parse_persona_and_agent() -> None:
    p = SkillScope.parse("persona=worker-maintainer")
    assert p.kind == "persona" and p.target == "worker-maintainer"
    a = SkillScope.parse("agent=alice")
    assert a.kind == "agent" and a.target == "alice"


def test_scope_parse_rejects_garbage() -> None:
    import pytest
    with pytest.raises(ValueError):
        SkillScope.parse("nonsense")  # no = separator
    with pytest.raises(ValueError):
        SkillScope.parse("worktree=x")  # unknown kind
    with pytest.raises(ValueError):
        SkillScope.parse("persona=")  # empty target


def test_scope_applies_to() -> None:
    g = SkillScope(kind="global")
    p = SkillScope(kind="persona", target="worker")
    a = SkillScope(kind="agent", target="alice")
    assert g.applies_to(agent_id="alice", persona_name="worker") is True
    assert g.applies_to(agent_id=None, persona_name=None) is True
    assert p.applies_to(agent_id="alice", persona_name="worker") is True
    assert p.applies_to(agent_id="alice", persona_name="leader") is False
    assert a.applies_to(agent_id="alice", persona_name="worker") is True
    assert a.applies_to(agent_id="bob", persona_name="worker") is False


# ---------------------------------------------------------------------------
# Discovery + validation
# ---------------------------------------------------------------------------


def test_ensure_skills_skeleton(tmp_path: Path) -> None:
    created = ensure_skills_skeleton(tmp_path)
    rels = {p.relative_to(tmp_path).as_posix() for p in created}
    assert rels == {
        "skills/approved", "skills/proposed", "skills/archived",
    }
    # Idempotent
    assert ensure_skills_skeleton(tmp_path) == []


def test_load_skips_proposed_and_archived(tmp_path: Path) -> None:
    ensure_skills_skeleton(tmp_path)
    _write_skill(tmp_path, "a", state="approved")
    _write_skill(tmp_path, "b", state="proposed")
    _write_skill(tmp_path, "c", state="archived")
    result = load_skills_for_context(tmp_path)
    names = {s.name for s in result.skills}
    assert names == {"a"}


def test_load_finds_dir_per_skill(tmp_path: Path) -> None:
    ensure_skills_skeleton(tmp_path)
    skill_dir = _write_skill(
        tmp_path, "git-rebase",
        description="how to rebase",
        extra_files={"example.txt": "demo"},
    )
    result = load_skills_for_context(tmp_path)
    assert len(result.skills) == 1
    skill = result.skills[0]
    assert skill.name == "git-rebase"
    assert skill.description == "how to rebase"
    assert skill.base_dir == skill_dir
    assert (skill.base_dir / "example.txt").exists()


def test_load_does_not_recurse_under_skill_root(tmp_path: Path) -> None:
    """Once SKILL.md is found, that dir is the skill root — a node_modules/
    or examples/ subdir mustn't accidentally produce more skills."""
    ensure_skills_skeleton(tmp_path)
    skill_dir = _write_skill(tmp_path, "outer")
    # Nested SKILL.md inside the outer skill — must be ignored.
    inner = skill_dir / "examples" / "inner"
    inner.mkdir(parents=True)
    (inner / "SKILL.md").write_text(
        "---\nname: inner\ndescription: should not surface\n---\nbody"
    )
    result = load_skills_for_context(tmp_path)
    assert {s.name for s in result.skills} == {"outer"}


def test_load_rejects_invalid_name(tmp_path: Path) -> None:
    ensure_skills_skeleton(tmp_path)
    d = tmp_path / "skills" / "approved" / "Bad_Name"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: Bad_Name\ndescription: x\n---\nbody"
    )
    result = load_skills_for_context(tmp_path)
    assert result.skills == []
    assert any(
        d_.level == "warning" and "name must match" in d_.message
        for d_ in result.diagnostics
    )


def test_load_rejects_missing_description(tmp_path: Path) -> None:
    ensure_skills_skeleton(tmp_path)
    d = tmp_path / "skills" / "approved" / "no-desc"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: no-desc\n---\nbody")
    result = load_skills_for_context(tmp_path)
    assert result.skills == []
    assert any("description" in d_.message for d_ in result.diagnostics)


def test_load_falls_back_to_dir_name_when_no_name_field(tmp_path: Path) -> None:
    ensure_skills_skeleton(tmp_path)
    d = tmp_path / "skills" / "approved" / "from-dirname"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\ndescription: name comes from parent dir\n---\nbody"
    )
    result = load_skills_for_context(tmp_path)
    assert {s.name for s in result.skills} == {"from-dirname"}


# ---------------------------------------------------------------------------
# Scope filter at the loader level
# ---------------------------------------------------------------------------


def test_load_filters_global_visible_to_everyone(tmp_path: Path) -> None:
    ensure_skills_skeleton(tmp_path)
    _write_skill(tmp_path, "a", description="any", scope="global")
    out = load_skills_for_context(
        tmp_path, agent_id="alice", persona_name="worker"
    )
    assert {s.name for s in out.skills} == {"a"}


def test_load_filters_persona_scope(tmp_path: Path) -> None:
    ensure_skills_skeleton(tmp_path)
    _write_skill(tmp_path, "for-worker", scope="persona=worker-maintainer")
    _write_skill(tmp_path, "for-reviewer", scope="persona=reviewer-skill")
    out = load_skills_for_context(
        tmp_path, agent_id="alice", persona_name="worker-maintainer"
    )
    assert {s.name for s in out.skills} == {"for-worker"}


def test_load_filters_agent_scope(tmp_path: Path) -> None:
    ensure_skills_skeleton(tmp_path)
    _write_skill(tmp_path, "for-alice", scope="agent=alice")
    _write_skill(tmp_path, "for-bob", scope="agent=bob")
    out = load_skills_for_context(
        tmp_path, agent_id="alice", persona_name="worker"
    )
    assert {s.name for s in out.skills} == {"for-alice"}


# ---------------------------------------------------------------------------
# Collisions
# ---------------------------------------------------------------------------


def test_collision_first_wins(tmp_path: Path) -> None:
    """Two skills with the same name → first kept, second is a
    'collision' diagnostic."""
    ensure_skills_skeleton(tmp_path)
    _write_skill(tmp_path, "dupe", description="first")
    # Force a collision by re-using the name but living in a sibling dir.
    other = tmp_path / "skills" / "approved" / "dupe-clone"
    other.mkdir(parents=True)
    (other / "SKILL.md").write_text(
        "---\nname: dupe\ndescription: second\n---\nbody"
    )
    out = load_skills_for_context(tmp_path)
    assert len(out.skills) == 1
    assert out.skills[0].description == "first"
    collisions = [d for d in out.diagnostics if d.level == "collision"]
    assert len(collisions) == 1


# ---------------------------------------------------------------------------
# Prompt formatting (XML block)
# ---------------------------------------------------------------------------


def test_format_skills_for_prompt_xml_shape(tmp_path: Path) -> None:
    ensure_skills_skeleton(tmp_path)
    _write_skill(
        tmp_path, "git-rebase", description="how to git rebase safely"
    )
    out = load_skills_for_context(tmp_path)
    rendered = format_skills_for_prompt(out.skills)
    assert "<available_skills>" in rendered
    assert "<name>git-rebase</name>" in rendered
    assert "<description>how to git rebase safely</description>" in rendered
    # Location attribute uses absolute path to SKILL.md so the model can
    # read body on demand without guessing where it lives.
    assert "skills/approved/git-rebase/SKILL.md" in rendered


def test_format_skills_for_prompt_omits_disabled(tmp_path: Path) -> None:
    """`disable-model-invocation: true` keeps the skill on disk but hides
    it from the menu — explicit-only invocation."""
    ensure_skills_skeleton(tmp_path)
    _write_skill(tmp_path, "visible")
    _write_skill(tmp_path, "hidden", disable=True)
    out = load_skills_for_context(tmp_path)
    rendered = format_skills_for_prompt(out.skills)
    assert "<name>visible</name>" in rendered
    assert "<name>hidden</name>" not in rendered


def test_format_skills_empty_returns_empty(tmp_path: Path) -> None:
    ensure_skills_skeleton(tmp_path)
    out = load_skills_for_context(tmp_path)
    assert format_skills_for_prompt(out.skills) == ""


def test_format_includes_scope_when_non_global(tmp_path: Path) -> None:
    """Helpful for the model to see why some skill is only available to
    it — but global ones don't need the noise."""
    ensure_skills_skeleton(tmp_path)
    _write_skill(tmp_path, "g")  # global default
    _write_skill(tmp_path, "p", scope="persona=worker")
    out = load_skills_for_context(
        tmp_path, agent_id="x", persona_name="worker"
    )
    rendered = format_skills_for_prompt(out.skills)
    assert "<scope>persona=worker</scope>" in rendered
    # global skill renders WITHOUT a scope element (less noise).
    g_block_pos = rendered.index("<name>g</name>")
    close_pos = rendered.index("</skill>", g_block_pos)
    assert "<scope>" not in rendered[g_block_pos:close_pos]
