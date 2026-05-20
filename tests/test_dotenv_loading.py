"""Tests for the .env loader chain."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import lyre.config as cfg_mod


@pytest.fixture(autouse=True)
def _reset_dotenv_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg_mod, "_DOTENV_LOADED", False)


def test_load_dotenv_from_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("LYRE_TEST_VAR_A=from_cwd\n", encoding="utf-8")
    monkeypatch.delenv("LYRE_TEST_VAR_A", raising=False)

    loaded = cfg_mod.load_dotenv_chain()
    assert loaded and loaded[0].parent == tmp_path
    assert os.getenv("LYRE_TEST_VAR_A") == "from_cwd"


def test_existing_env_wins_over_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("LYRE_TEST_VAR_B=from_dotenv\n", encoding="utf-8")
    monkeypatch.setenv("LYRE_TEST_VAR_B", "from_real_env")

    cfg_mod.load_dotenv_chain()
    assert os.getenv("LYRE_TEST_VAR_B") == "from_real_env"


def test_repo_root_env_picked_up_when_cwd_lacks_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """We can't easily fake the repo root path without monkey-patching; instead
    we change cwd into a subdir that doesn't have .env and verify the repo
    root .env in this very project is at least *attempted*."""
    sub = tmp_path / "nested"
    sub.mkdir()
    monkeypatch.chdir(sub)
    monkeypatch.delenv("LYRE_TEST_VAR_C", raising=False)
    cfg_mod.load_dotenv_chain()
    # We can't assert presence (this repo's .env might not exist locally), but
    # we can assert no crash and that the load-cycle guard is set.
    assert cfg_mod._DOTENV_LOADED is True


def test_load_is_idempotent_within_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("LYRE_TEST_VAR_D=v1\n", encoding="utf-8")
    cfg_mod.load_dotenv_chain()
    # Mutate the file then call again — second call should be a no-op.
    (tmp_path / ".env").write_text("LYRE_TEST_VAR_D=v2\n", encoding="utf-8")
    second = cfg_mod.load_dotenv_chain()
    assert second == []
    assert os.getenv("LYRE_TEST_VAR_D") == "v1"


def test_config_from_env_picks_up_dotenv_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "ANTHROPIC_API_KEY=sk-from-dotenv\n"
        "LYRE_MODEL_OVERRIDE=deepseek.deepseek-v4-flash\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LYRE_MODEL_OVERRIDE", raising=False)

    cfg = cfg_mod.Config.from_env()
    assert cfg.anthropic_api_key == "sk-from-dotenv"
    assert cfg.model_override == "deepseek.deepseek-v4-flash"
