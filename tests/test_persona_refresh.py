"""`lyre persona-refresh` / refresh_user_persona — pull shipped persona updates.

ensure_user_personas never overwrites identity.md (user SSOT), so a shipped
persona EDIT can't reach an already-onboarded install on its own. refresh_user_persona
does, backing up the current identity.md first so local edits survive.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lyre.personas.seed import (
    ensure_user_personas,
    refresh_user_persona,
    shipped_persona_names,
)


def test_refresh_overwrites_identity_and_backs_up(tmp_path: Path) -> None:
    personas = tmp_path / "personas"
    ensure_user_personas(personas)
    identity = personas / "dispatcher" / "identity.md"
    identity.write_text("STALE LOCAL CONTENT\n", encoding="utf-8")  # simulate drift

    refreshed, bak = refresh_user_persona(personas, "dispatcher")

    assert refreshed == identity
    body = identity.read_text(encoding="utf-8")
    assert "STALE LOCAL CONTENT" not in body  # shipped content restored
    assert body.startswith("---")  # real frontmatter
    # the prior content is preserved in the backup, so nothing is lost.
    assert bak is not None and bak.exists()
    assert bak.read_text(encoding="utf-8") == "STALE LOCAL CONTENT\n"


def test_refresh_no_backup_when_disabled(tmp_path: Path) -> None:
    personas = tmp_path / "personas"
    ensure_user_personas(personas)
    _, bak = refresh_user_persona(personas, "dispatcher", backup=False)
    assert bak is None


def test_refresh_unknown_persona_raises(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        refresh_user_persona(tmp_path / "personas", "nonesuch")


def test_long_runner_is_shipped_and_refreshable(tmp_path: Path) -> None:
    # the new persona ships and can be pulled into a fresh user dir.
    assert "long-runner" in shipped_persona_names()
    personas = tmp_path / "personas"
    identity, _ = refresh_user_persona(personas, "long-runner")
    assert identity.exists()
    assert "long-runner" in identity.read_text(encoding="utf-8")
