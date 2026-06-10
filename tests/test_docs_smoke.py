"""Doc-drift tripwires.

Fully offline: no subprocess, no DB, no provider keys. Each test pins one
high-value fact in the public docs to the code that implements it, so the
next rename/flag change fails CI instead of failing a new user's first
command.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _seeded_agent_ids() -> set[str]:
    """The agent ids bootstrap actually seeds, derived the same way
    ``seed.seed_default_agents`` does: every shipped persona whose kind is
    not ``spawn_only`` gets one agent whose id is ``display_name or name``.

    Deliberately NOT hardcoded (the original drift was exactly that the
    docs said ``leader`` while the seeded singleton is ``dispatcher``):
    if the persona roster or a display_name changes, this set follows the
    code and the docs must follow too.
    """
    from lyre.personas.seed import discover_persona_files, load_persona_from_file

    # discover_persona_files(None) falls back to the shipped personas —
    # the exact set ensure_user_personas copies on first onboard.
    personas = [load_persona_from_file(p) for p in discover_persona_files(None)]
    return {p.display_name or p.name for p in personas if p.kind != "spawn_only"}


def test_quick_start_send_targets_are_seeded_agent_ids() -> None:
    """Drift class: the user docs said `lyre send leader ...` but
    bootstrap seeds `dispatcher` — the very first command a new user runs
    failed with 'unknown agent'. Pin every `lyre send <target>` in ALL
    three user-facing docs to the seeded set (the drift class hit all of
    them, not just README).
    """
    docs = [
        REPO_ROOT / "README.md",
        REPO_ROOT / "docs" / "getting-started.md",
        REPO_ROOT / "docs" / "concepts.md",
    ]
    seeded = _seeded_agent_ids()
    found_any = False
    for doc in docs:
        text = doc.read_text(encoding="utf-8")
        targets = [
            (lineno, m.group(1))
            for lineno, line in enumerate(text.splitlines(), start=1)
            for m in re.finditer(r"lyre send\s+([A-Za-z0-9_./-]+)", line)
        ]
        found_any = found_any or bool(targets)
        rel = doc.relative_to(REPO_ROOT)
        for lineno, target in targets:
            assert target in seeded, (
                f"{rel}:{lineno} tells users to `lyre send {target}`, but bootstrap "
                f"seeds only {sorted(seeded)} — a new user's first command would fail. "
                f"Update the doc (or the persona roster) so they agree."
            )
    assert found_any, (
        "No user doc shows a `lyre send <target>` quick-start command anymore — "
        "did the quick-start get reworded? Update this test's pattern."
    )


def test_cli_reference_send_flags_all_exist_on_the_click_command() -> None:
    """Drift class: cli-reference.md documented a `--reply-to` flag that
    `lyre send` never had. Every `--flag` the doc shows for `lyre send`
    must be a registered option on the click command (introspected from
    lyre.main — offline, no subprocess).
    """
    from lyre.main import cli

    send_cmd = cli.commands["send"]
    real_flags = {
        opt
        for param in send_cmd.params
        for opt in (*param.opts, *param.secondary_opts)
        if opt.startswith("--")
    }
    real_flags.add("--help")  # click adds this implicitly

    doc = REPO_ROOT / "docs" / "cli-reference.md"
    text = doc.read_text(encoding="utf-8")
    documented: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if "lyre send" not in line:
            continue
        for m in re.finditer(r"--[a-zA-Z][a-zA-Z0-9-]*", line):
            documented.append((lineno, m.group(0)))

    assert documented, (
        "docs/cli-reference.md no longer documents any `lyre send` flags — "
        "did the send table row get renamed? Update this test's line filter."
    )
    for lineno, flag in documented:
        assert flag in real_flags, (
            f"docs/cli-reference.md:{lineno} documents `{flag}` for `lyre send`, "
            f"but the click command only has {sorted(real_flags)} — the doc drifted "
            f"from the CLI."
        )
