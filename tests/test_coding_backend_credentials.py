"""CD-2: the credential broker — a gated shell_exec opt-in.

`shell_exec(credentials="<backend>")` injects an owner-declared external
coding-agent key into ONE subprocess so a discovered coding-agent skill can
authenticate. The secret is read server-side (never returned to the agent);
bundles are config-declared, optionally persona-gated, and default off. Without
the opt-in, shell_exec strips all secrets as before. See
docs/design/CAPABILITY_DISCOVERY.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from lyre.config import CodingBackend, Config
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.tools import ToolContext, ToolError
from lyre.runtime.tools.shell import SHELL_EXEC

# Print one env var's value to stdout (portable; no shell expansion needed).
_PRINT_ENV = [sys.executable, "-c", "import os,sys; sys.stdout.write(os.environ.get('FAKE_CODE_KEY',''))"]


def _ctx(
    repos: SqliteRepositories, tmp_path: Path, *, backends: dict, persona: str = "worker-maintainer"
) -> ToolContext:
    wt = tmp_path / "wt"
    wt.mkdir(exist_ok=True)
    return ToolContext(
        repos=repos, task_id="t", wakeup_id="w", persona_name=persona,
        extras={"worktree": str(wt), "coding_backends": backends},
    )


@pytest.mark.asyncio
async def test_credentials_injects_secret_into_subprocess(
    repos: SqliteRepositories, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CODE_KEY", "sekret-123")
    ctx = _ctx(repos, tmp_path, backends={"codex": CodingBackend(auth_env="FAKE_CODE_KEY")})
    out = await SHELL_EXEC.handler(ctx, {"argv": _PRINT_ENV, "credentials": "codex"})
    assert out["exit_code"] == 0
    assert "sekret-123" in out["stdout"]


@pytest.mark.asyncio
async def test_without_opt_in_secret_is_stripped(
    repos: SqliteRepositories, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The key is in the runtime env, but a plain shell_exec must NOT leak it.
    monkeypatch.setenv("FAKE_CODE_KEY", "sekret-123")
    ctx = _ctx(repos, tmp_path, backends={"codex": CodingBackend(auth_env="FAKE_CODE_KEY")})
    out = await SHELL_EXEC.handler(ctx, {"argv": _PRINT_ENV})  # no credentials
    assert "sekret-123" not in out["stdout"]


@pytest.mark.asyncio
async def test_unknown_bundle_errors(
    repos: SqliteRepositories, tmp_path: Path
) -> None:
    ctx = _ctx(repos, tmp_path, backends={})
    with pytest.raises(ToolError, match="unknown credentials bundle"):
        await SHELL_EXEC.handler(ctx, {"argv": _PRINT_ENV, "credentials": "nope"})


@pytest.mark.asyncio
async def test_persona_not_allowed_errors(
    repos: SqliteRepositories, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CODE_KEY", "sekret-123")
    backends = {"codex": CodingBackend(auth_env="FAKE_CODE_KEY", allowed_personas=("analyst",))}
    ctx = _ctx(repos, tmp_path, backends=backends, persona="worker-maintainer")
    with pytest.raises(ToolError, match="not allowed"):
        await SHELL_EXEC.handler(ctx, {"argv": _PRINT_ENV, "credentials": "codex"})


@pytest.mark.asyncio
async def test_allowed_persona_passes(
    repos: SqliteRepositories, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CODE_KEY", "sekret-123")
    backends = {"codex": CodingBackend(auth_env="FAKE_CODE_KEY", allowed_personas=("worker-maintainer",))}
    ctx = _ctx(repos, tmp_path, backends=backends, persona="worker-maintainer")
    out = await SHELL_EXEC.handler(ctx, {"argv": _PRINT_ENV, "credentials": "codex"})
    assert "sekret-123" in out["stdout"]


@pytest.mark.asyncio
async def test_missing_env_secret_errors(
    repos: SqliteRepositories, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FAKE_CODE_KEY", raising=False)
    ctx = _ctx(repos, tmp_path, backends={"codex": CodingBackend(auth_env="FAKE_CODE_KEY")})
    with pytest.raises(ToolError, match="not set"):
        await SHELL_EXEC.handler(ctx, {"argv": _PRINT_ENV, "credentials": "codex"})


# --- config parsing ---
def test_config_parses_coding_backends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LYRE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text(
        '[coding_backends.codex]\n'
        'auth_env = "OPENAI_API_KEY"\n'
        'allowed_personas = ["worker-maintainer"]\n'
        '[coding_backends.claude]\n'
        'auth_env = "ANTHROPIC_API_KEY"\n',
        encoding="utf-8",
    )
    cfg = Config.from_env()
    assert cfg.coding_backends["codex"].auth_env == "OPENAI_API_KEY"
    assert cfg.coding_backends["codex"].allowed_personas == ("worker-maintainer",)
    assert cfg.coding_backends["claude"].auth_env == "ANTHROPIC_API_KEY"
    assert cfg.coding_backends["claude"].allowed_personas is None


def test_config_skips_backend_missing_auth_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LYRE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text(
        '[coding_backends.broken]\ndescription = "no auth_env"\n', encoding="utf-8"
    )
    cfg = Config.from_env()
    assert "broken" not in cfg.coding_backends  # skipped, not a crash


def test_config_coding_backends_empty_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LYRE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    assert Config.from_env().coding_backends == {}
