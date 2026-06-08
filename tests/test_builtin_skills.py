"""Builtin skills (Option B — startup mirror).

Packaged skills under src/lyre/data/skills/ are refreshed into
~/.lyre/skills/builtin/ every startup (sync_builtin_skills), surfaced in the
skill menu alongside approved/, and an owner skill of the same name in approved/
shadows the builtin (override). Unlike copy-once shipped personas/facts, builtin
skills track the installed version.
"""

from __future__ import annotations

from pathlib import Path

from lyre.runtime.skills import (
    ensure_skills_skeleton,
    load_skills_for_context,
    sync_builtin_skills,
)


def test_sync_mirrors_packaged_skills(tmp_path: Path) -> None:
    names = sync_builtin_skills(tmp_path)
    assert "adversarial-review" in names
    assert (
        tmp_path / "skills" / "builtin" / "adversarial-review" / "SKILL.md"
    ).is_file()


def test_builtin_skill_surfaces_in_menu(tmp_path: Path) -> None:
    ensure_skills_skeleton(tmp_path)
    sync_builtin_skills(tmp_path)
    by_name = {s.name: s for s in load_skills_for_context(tmp_path).skills}
    assert "adversarial-review" in by_name
    assert by_name["adversarial-review"].source == "builtin"


def test_approved_shadows_builtin_override(tmp_path: Path) -> None:
    ensure_skills_skeleton(tmp_path)
    sync_builtin_skills(tmp_path)
    # Owner override: same-named skill in approved/.
    ov = tmp_path / "skills" / "approved" / "adversarial-review"
    ov.mkdir(parents=True)
    (ov / "SKILL.md").write_text(
        "---\nname: adversarial-review\ndescription: owner override version\n"
        "---\noverridden body\n",
        encoding="utf-8",
    )
    s = {x.name: x for x in load_skills_for_context(tmp_path).skills}["adversarial-review"]
    assert s.source == "approved"  # owner override wins on collision
    assert s.description == "owner override version"


def test_sync_removes_stale_builtin(tmp_path: Path) -> None:
    sync_builtin_skills(tmp_path)
    stale = tmp_path / "skills" / "builtin" / "ghost-skill"
    stale.mkdir(parents=True)
    (stale / "SKILL.md").write_text(
        "---\nname: ghost-skill\ndescription: not in the package\n---\n",
        encoding="utf-8",
    )
    names = sync_builtin_skills(tmp_path)  # wipe + recopy
    assert "ghost-skill" not in names
    assert not stale.exists()
    assert "adversarial-review" in names
