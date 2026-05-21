"""Tests for ``lyre.config`` + ``lyre.onboard``.

The interactive wizard isn't tested end-to-end here — the pure file
writers + ``bootstrap_runtime`` are, since those are what fail in
production if they're wrong.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lyre.config import Config, ModelEntry, PersonaOverride
from lyre.onboard import (
    PROTOCOLS,
    USER_MD_TEMPLATE,
    ModelSpec,
    append_env_line,
    bootstrap_runtime,
    can_reach_env_var,
    write_config_toml,
    write_user_md_template,
)

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def test_config_uses_lyre_home_env_for_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LYRE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)  # avoid picking up project .env

    cfg = Config.from_env()

    assert cfg.lyre_home == tmp_path
    assert cfg.db_path == tmp_path / "lyre.db"
    assert cfg.memory_path == tmp_path / "memory"
    assert cfg.user_md_path == tmp_path / "user.md"
    assert cfg.env_path == tmp_path / ".env"
    assert cfg.user_personas_dir == tmp_path / "personas"


def test_config_reads_owner_from_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LYRE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LYRE_OWNER_NAME", raising=False)
    monkeypatch.delenv("LYRE_OWNER_EMAIL", raising=False)
    (tmp_path / "config.toml").write_text(
        '[owner]\nname = "Alice"\nemail = "alice@example.com"\n',
        encoding="utf-8",
    )

    cfg = Config.from_env()

    assert cfg.owner.name == "Alice"
    assert cfg.owner.email == "alice@example.com"


def test_config_env_var_beats_toml_for_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LYRE_HOME", str(tmp_path))
    monkeypatch.setenv("LYRE_OWNER_NAME", "FromEnv")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text(
        '[owner]\nname = "FromToml"\n', encoding="utf-8",
    )

    cfg = Config.from_env()

    assert cfg.owner.name == "FromEnv"


def test_config_parses_model_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LYRE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text(
        """
[owner]
name = "o"

[[models]]
id = "openrouter.qwen3-coder"
provider = "openai"
endpoint = { base_url = "https://openrouter.ai/api/v1", auth_env = "OPENROUTER_API_KEY" }
capabilities = ["tool_use"]
tier = "workhorse"
""",
        encoding="utf-8",
    )

    cfg = Config.from_env()

    assert len(cfg.models) == 1
    m = cfg.models[0]
    assert isinstance(m, ModelEntry)
    assert m.id == "openrouter.qwen3-coder"
    assert m.provider == "openai"
    assert m.tier == "workhorse"
    assert m.endpoint["base_url"] == "https://openrouter.ai/api/v1"


def test_config_parses_persona_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LYRE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text(
        """
[owner]
name = "o"

[personas.leader]
model_preference = { prefer = ["anthropic.claude-opus-4-7"] }
""",
        encoding="utf-8",
    )

    cfg = Config.from_env()

    assert "leader" in cfg.persona_overrides
    o = cfg.persona_overrides["leader"]
    assert isinstance(o, PersonaOverride)
    assert o.model_preference == {"prefer": ["anthropic.claude-opus-4-7"]}


def test_config_is_onboarded_reflects_config_toml_presence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LYRE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    cfg = Config.from_env()
    assert not cfg.is_onboarded()

    (tmp_path / "config.toml").write_text("[owner]\nname = 'x'\n", encoding="utf-8")
    cfg2 = Config.from_env()
    assert cfg2.is_onboarded()


# ---------------------------------------------------------------------------
# Onboard file writers
# ---------------------------------------------------------------------------


def test_write_user_md_template_creates_file_when_absent(tmp_path: Path) -> None:
    user_md = tmp_path / "user.md"
    written = write_user_md_template(user_md)
    assert written is True
    assert user_md.read_text(encoding="utf-8") == USER_MD_TEMPLATE


def test_write_user_md_template_skips_existing_unless_overwrite(tmp_path: Path) -> None:
    user_md = tmp_path / "user.md"
    user_md.write_text("# Existing user.md, do not touch", encoding="utf-8")

    assert write_user_md_template(user_md) is False
    assert "do not touch" in user_md.read_text(encoding="utf-8")

    assert write_user_md_template(user_md, overwrite=True) is True
    assert "About me" in user_md.read_text(encoding="utf-8")


def _strip_comments(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.startswith("#"))


def test_write_config_toml_minimal(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    write_config_toml(cfg, owner_name="Alice", owner_email=None)

    text = cfg.read_text(encoding="utf-8")
    active = _strip_comments(text)
    assert 'name = "Alice"' in active
    assert "email" not in active
    assert "[runtime]" not in active  # no model passed
    assert "[[models]]" not in active


def test_write_config_toml_header_only_auth(tmp_path: Path) -> None:
    """Header-only auth mode: no auth_env, just [models.endpoint.headers].
    Verifies the wizard's output round-trips through tomllib + the
    ModelEndpoint loader so a real lyre serve picks up the headers."""
    import tomllib

    from lyre.config import Config
    from lyre.runtime.model_registry import ModelEndpoint

    cfg = tmp_path / "config.toml"
    spec = ModelSpec(
        id="internal.claude",
        provider="anthropic",
        endpoint="https://proxy.internal/anthropic",
        auth_env="",  # header-only mode
        headers=(("X-Internal-JWT", "${INTERNAL_JWT}"),),
    )
    write_config_toml(
        cfg, owner_name="Owner", owner_email=None,
        models=[spec], default_model=spec.id,
    )
    text = cfg.read_text(encoding="utf-8")

    # The header sub-table is emitted; auth_env is NOT.
    assert "[models.endpoint.headers]" in text
    assert '"X-Internal-JWT" = "${INTERNAL_JWT}"' in text
    assert "auth_env" not in _strip_comments(text)

    # Round-trip: parse → load via Config → runtime ModelEndpoint.
    monkeypatch_env_for_loader = {"LYRE_HOME": str(tmp_path)}
    import os as _os
    saved = dict(_os.environ)
    _os.environ.update(monkeypatch_env_for_loader)
    _os.environ["INTERNAL_JWT"] = "secret-token"
    try:
        loaded = Config.from_env()
        assert len(loaded.models) == 1
        m = loaded.models[0]
        assert m.id == "internal.claude"
        ep = ModelEndpoint.from_dict(m.endpoint)
        assert ep.auth_env is None
        assert dict(ep.headers)["X-Internal-JWT"] == "secret-token"
        # And the raw parse path matches.
        with cfg.open("rb") as f:
            raw = tomllib.load(f)
        assert raw["models"][0]["endpoint"]["headers"] == {
            "X-Internal-JWT": "${INTERNAL_JWT}",
        }
    finally:
        _os.environ.clear()
        _os.environ.update(saved)


def test_write_config_toml_stacked_auth(tmp_path: Path) -> None:
    """API key + extra headers — the OpenAI org/project pattern."""
    cfg = tmp_path / "config.toml"
    spec = ModelSpec(
        id="openai.gpt",
        provider="openai",
        endpoint="",
        auth_env="OPENAI_API_KEY",
        headers=(
            ("OpenAI-Organization", "org-abc"),
            ("OpenAI-Project", "proj-123"),
        ),
    )
    write_config_toml(
        cfg, owner_name="Owner", owner_email=None,
        models=[spec], default_model=spec.id,
    )
    active = _strip_comments(cfg.read_text(encoding="utf-8"))
    assert 'auth_env = "OPENAI_API_KEY"' in active
    assert "[models.endpoint.headers]" in active
    assert '"OpenAI-Organization" = "org-abc"' in active
    assert '"OpenAI-Project" = "proj-123"' in active


def test_model_summary_line_adapts_to_auth_mode() -> None:
    """The wizard's summary helper labels each entry with its actual
    auth shape — no empty `[$]` placeholders for header-only entries."""
    from lyre.onboard import _model_summary_line

    api_only = ModelSpec(
        id="a", provider="anthropic", endpoint="", auth_env="K",
    )
    assert "key:$K" in _model_summary_line(api_only)

    header_only = ModelSpec(
        id="b", provider="anthropic", endpoint="", auth_env="",
        headers=(("X-Auth", "x"), ("X-Other", "y")),
    )
    line = _model_summary_line(header_only)
    assert "2 custom header(s)" in line
    assert "$" not in line  # no `[$]` placeholder bug

    stacked = ModelSpec(
        id="c", provider="openai", endpoint="", auth_env="K",
        headers=(("X-Auth", "x"),),
    )
    assert "key:$K + 1 header(s)" in _model_summary_line(stacked)


def test_is_valid_header_name_accepts_real_and_rejects_garbage() -> None:
    from lyre.onboard import _is_valid_header_name

    assert _is_valid_header_name("Authorization")
    assert _is_valid_header_name("X-API-Key")
    assert _is_valid_header_name("OpenAI-Organization")
    assert _is_valid_header_name("X_Custom")

    assert not _is_valid_header_name("")
    assert not _is_valid_header_name(" Has-Space")
    assert not _is_valid_header_name("Has Space")  # typo: space in middle
    assert not _is_valid_header_name("1-starts-with-digit")
    assert not _is_valid_header_name("中文")  # only ascii letters


def test_write_config_toml_with_one_model_default_endpoint(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    spec = ModelSpec(
        id="anthropic.claude-sonnet-4-6",
        provider="anthropic",
        endpoint="",  # empty = use SDK default
        auth_env="ANTHROPIC_API_KEY",
    )
    write_config_toml(
        cfg, owner_name="Bob", owner_email="bob@x.com",
        models=[spec], default_model=spec.id,
    )

    text = cfg.read_text(encoding="utf-8")
    active = _strip_comments(text)
    assert 'name = "Bob"' in active
    assert 'email = "bob@x.com"' in active
    assert 'default_model = "anthropic.claude-sonnet-4-6"' in active
    assert "[[models]]" in active
    assert 'id = "anthropic.claude-sonnet-4-6"' in active
    assert 'provider = "anthropic"' in active
    # No base_url written when endpoint is empty — adapter uses SDK default.
    assert "base_url" not in active
    assert 'auth_env = "ANTHROPIC_API_KEY"' in active


def test_write_config_toml_with_custom_endpoint(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    spec = ModelSpec(
        id="anthropic.deepseek-v4-flash",
        provider="anthropic",
        endpoint="https://api.deepseek.com/anthropic",
        auth_env="DEEPSEEK_API_KEY",
    )
    write_config_toml(
        cfg, owner_name="Alice", owner_email=None,
        models=[spec], default_model=spec.id,
    )

    text = cfg.read_text(encoding="utf-8")
    assert 'base_url = "https://api.deepseek.com/anthropic"' in text
    assert 'auth_env = "DEEPSEEK_API_KEY"' in text
    assert 'default_model = "anthropic.deepseek-v4-flash"' in text


def test_write_config_toml_with_multiple_models(tmp_path: Path) -> None:
    """Wizard supports configuring N models with one default picked among them."""
    cfg = tmp_path / "config.toml"
    specs = [
        ModelSpec("anthropic.claude-sonnet-4-6", "anthropic", "", "ANTHROPIC_API_KEY"),
        ModelSpec(
            "anthropic.deepseek-v4-flash", "anthropic",
            "https://api.deepseek.com/anthropic", "DEEPSEEK_API_KEY",
        ),
        ModelSpec(
            "openai.gpt-4o-mini", "openai", "", "OPENAI_API_KEY",
        ),
    ]
    write_config_toml(
        cfg, owner_name="Alice", owner_email=None,
        models=specs, default_model="anthropic.deepseek-v4-flash",
    )

    text = cfg.read_text(encoding="utf-8")
    active = _strip_comments(text)
    assert active.count("[[models]]") == 3
    assert 'id = "anthropic.claude-sonnet-4-6"' in active
    assert 'id = "anthropic.deepseek-v4-flash"' in active
    assert 'id = "openai.gpt-4o-mini"' in active
    assert 'default_model = "anthropic.deepseek-v4-flash"' in active


def test_config_loader_round_trips_wizard_written_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: what the wizard writes, the loader reads back as a usable
    model registry entry."""
    monkeypatch.setenv("LYRE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    spec = ModelSpec(
        id="anthropic.deepseek-v4-flash",
        provider="anthropic",
        endpoint="https://api.deepseek.com/anthropic",
        auth_env="DEEPSEEK_API_KEY",
    )
    write_config_toml(
        tmp_path / "config.toml",
        owner_name="Alice", owner_email=None,
        models=[spec], default_model=spec.id,
    )

    cfg = Config.from_env()

    assert len(cfg.models) == 1
    m = cfg.models[0]
    assert m.id == "anthropic.deepseek-v4-flash"
    assert m.provider == "anthropic"
    assert m.endpoint["base_url"] == "https://api.deepseek.com/anthropic"
    assert m.endpoint["auth_env"] == "DEEPSEEK_API_KEY"
    assert cfg.default_model == "anthropic.deepseek-v4-flash"


def test_config_loader_round_trips_multiple_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LYRE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    specs = [
        ModelSpec("anthropic.claude-sonnet-4-6", "anthropic", "", "ANTHROPIC_API_KEY"),
        ModelSpec("openai.gpt-4o-mini", "openai", "", "OPENAI_API_KEY"),
    ]
    write_config_toml(
        tmp_path / "config.toml",
        owner_name="o", owner_email=None,
        models=specs, default_model=specs[1].id,
    )
    cfg = Config.from_env()
    assert {m.id for m in cfg.models} == {
        "anthropic.claude-sonnet-4-6", "openai.gpt-4o-mini"
    }
    assert cfg.default_model == "openai.gpt-4o-mini"


def test_write_config_toml_emits_api_responses_when_non_default(
    tmp_path: Path,
) -> None:
    """When ModelSpec.api='responses', the written config.toml must
    surface it under [models.endpoint] so the loader and adapter
    factory route to OpenAIResponsesAdapter."""
    cfg = tmp_path / "config.toml"
    spec = ModelSpec(
        id="openai.proxy-gpt5",
        provider="openai",
        endpoint="https://gateway.internal/responses",
        auth_env="PROXY_API_KEY",
        api="responses",
    )
    write_config_toml(
        cfg, owner_name="Alice", owner_email=None,
        models=[spec], default_model=spec.id,
    )
    text = cfg.read_text(encoding="utf-8")
    active = _strip_comments(text)
    assert 'api = "responses"' in active
    assert "[models.endpoint]" in active
    assert 'base_url = "https://gateway.internal/responses"' in active


def test_write_config_toml_omits_api_when_default(tmp_path: Path) -> None:
    """`api` field is opt-in noise — for the standard chat-completions
    dialect we should NOT clutter config.toml with a redundant
    `api = "chat-completions"` line."""
    cfg = tmp_path / "config.toml"
    spec = ModelSpec(
        id="openai.gpt-4o",
        provider="openai",
        endpoint="",
        auth_env="OPENAI_API_KEY",
        # api defaults to "chat-completions"
    )
    write_config_toml(
        cfg, owner_name="Alice", owner_email=None,
        models=[spec], default_model=spec.id,
    )
    active = _strip_comments(cfg.read_text(encoding="utf-8"))
    assert "api = " not in active


def test_config_loader_round_trips_endpoint_api_responses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: ModelSpec(api='responses') → config.toml → Config
    loader preserves the field so the runtime registry sees
    `endpoint.api='responses'`."""
    monkeypatch.setenv("LYRE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    spec = ModelSpec(
        id="openai.proxy-gpt5",
        provider="openai",
        endpoint="https://gateway.internal/responses",
        auth_env="PROXY_API_KEY",
        api="responses",
    )
    write_config_toml(
        tmp_path / "config.toml",
        owner_name="Alice", owner_email=None,
        models=[spec], default_model=spec.id,
    )

    cfg = Config.from_env()
    assert len(cfg.models) == 1
    m = cfg.models[0]
    assert m.endpoint["api"] == "responses"

    # And the runtime registry routes through OpenAIResponsesAdapter.
    from lyre.runtime.model_registry import load_registry_for_config
    reg = load_registry_for_config(cfg)
    entry = reg.by_id("openai.proxy-gpt5")
    assert entry is not None
    assert entry.endpoint.api == "responses"


def test_write_config_toml_escapes_quotes_in_name(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    write_config_toml(cfg, owner_name='Eve "the" Owner', owner_email=None)
    text = cfg.read_text(encoding="utf-8")
    assert r'name = "Eve \"the\" Owner"' in text


def test_append_env_line_creates_with_chmod_600(tmp_path: Path) -> None:
    envp = tmp_path / ".env"
    append_env_line(envp, "ANTHROPIC_API_KEY", "sk-test")

    assert envp.read_text(encoding="utf-8").strip() == "ANTHROPIC_API_KEY=sk-test"
    mode = envp.stat().st_mode & 0o777
    # On platforms where chmod is honored, expect 0o600. On others (rare), at
    # least confirm the file exists; don't fail the test for non-Unix.
    if os.name == "posix":
        assert mode == 0o600


def test_append_env_line_replaces_existing_key(tmp_path: Path) -> None:
    envp = tmp_path / ".env"
    envp.write_text("ANTHROPIC_API_KEY=old\nOTHER=keep\n", encoding="utf-8")

    append_env_line(envp, "ANTHROPIC_API_KEY", "new-value")

    text = envp.read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY=new-value" in text
    assert "ANTHROPIC_API_KEY=old" not in text
    assert "OTHER=keep" in text


def test_append_env_line_adds_new_key_alongside_existing(tmp_path: Path) -> None:
    envp = tmp_path / ".env"
    envp.write_text("OTHER=keep\n", encoding="utf-8")

    append_env_line(envp, "ANTHROPIC_API_KEY", "sk-new")

    lines = envp.read_text(encoding="utf-8").strip().splitlines()
    assert "OTHER=keep" in lines
    assert "ANTHROPIC_API_KEY=sk-new" in lines


# ---------------------------------------------------------------------------
# can_reach_env_var + PROTOCOLS
# ---------------------------------------------------------------------------


def test_protocols_cover_two_compatible_protocols() -> None:
    """Two top-level provider slots:
      * Anthropic-compatible — /v1/messages shape (Claude API et al.)
      * OpenAI-compatible — covers both Chat Completions (OpenAI proper,
        DeepSeek-OAI, OpenRouter, vLLM-OAI, …) and the Responses API
        (newer surface, internal gateways like bytedance ai-coder).
        Dialect is picked by `endpoint.api`, asked as a sub-question
        in the wizard — NOT a separate protocol.
    Anything else is just a custom endpoint over one of these."""
    keys = {p.key for p in PROTOCOLS}
    assert keys == {"anthropic", "openai"}


def test_can_reach_env_var_detects_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ok, msg = can_reach_env_var("ANTHROPIC_API_KEY")
    assert ok is False
    assert "not set" in msg


def test_can_reach_env_var_accepts_real_looking_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-something-real-looking")
    ok, msg = can_reach_env_var("ANTHROPIC_API_KEY")
    assert ok is True
    assert "set" in msg


# ---------------------------------------------------------------------------
# bootstrap_runtime
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_runtime_creates_db_and_seeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LYRE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    cfg = Config.from_env()

    created_agents = await bootstrap_runtime(cfg)

    # First bootstrap creates owner + leader.
    assert set(created_agents) == {"owner", "leader"}
    assert cfg.db_path.exists()
    assert (cfg.memory_path / "facts").is_dir()
    # Per-agent notebook files appeared.
    assert (cfg.memory_path / "facts" / "agent-owner-notes.md").is_file()
    assert (cfg.memory_path / "facts" / "agent-leader-notes.md").is_file()


@pytest.mark.asyncio
async def test_bootstrap_runtime_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LYRE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    cfg = Config.from_env()

    await bootstrap_runtime(cfg)
    # Second call must not raise and must not re-create existing agents.
    second_created = await bootstrap_runtime(cfg)
    assert second_created == []


# ---------------------------------------------------------------------------
# Persona dir layout: shipped → ~/.lyre/personas/<name>/identity.md
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_copies_shipped_personas_as_directory_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After bootstrap, ~/.lyre/personas/<name>/identity.md exists for every
    shipped persona — that directory is the SSOT going forward."""
    monkeypatch.setenv("LYRE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    cfg = Config.from_env()

    await bootstrap_runtime(cfg)

    user_personas = cfg.user_personas_dir
    # Every shipped persona has been materialized as <name>/identity.md.
    expected_personas = {"owner", "leader", "worker-maintainer", "reviewer-pr",
                         "reviewer-skill", "summary-agent"}
    for name in expected_personas:
        assert (user_personas / name / "identity.md").is_file(), name


@pytest.mark.asyncio
async def test_user_persona_edits_survive_re_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ensure_user_personas only copies *missing* files. User edits stick
    across `lyre serve` restarts that re-run bootstrap_runtime."""
    monkeypatch.setenv("LYRE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    cfg = Config.from_env()
    await bootstrap_runtime(cfg)

    leader_identity = cfg.user_personas_dir / "leader" / "identity.md"
    user_marker = "\n\n# USER-EDITED LINE (do not overwrite)\n"
    leader_identity.write_text(
        leader_identity.read_text(encoding="utf-8") + user_marker,
        encoding="utf-8",
    )

    await bootstrap_runtime(cfg)

    assert user_marker.strip() in leader_identity.read_text(encoding="utf-8")


def test_discover_persona_prefers_directory_over_flat(tmp_path: Path) -> None:
    """If both <name>.md and <name>/identity.md exist, directory wins."""
    from lyre.personas.seed import discover_persona_files

    (tmp_path / "leader.md").write_text(
        "---\nname: leader\nrole_description: flat\n---\nflat body",
        encoding="utf-8",
    )
    leader_dir = tmp_path / "leader"
    leader_dir.mkdir()
    (leader_dir / "identity.md").write_text(
        "---\nname: leader\nrole_description: dir\n---\ndir body",
        encoding="utf-8",
    )

    files = discover_persona_files(tmp_path)
    leader_files = [p for p in files if p.stem in ("leader", "identity")]
    assert len(leader_files) == 1
    # Directory wins → the resolved file lives under leader/ .
    assert leader_files[0] == leader_dir / "identity.md"


def test_discover_persona_falls_back_to_shipped_when_user_dir_empty(
    tmp_path: Path,
) -> None:
    """Tests that bypass bootstrap (don't populate user dir) still get
    personas — needed for non-onboard test fixtures."""
    from lyre.personas.seed import discover_persona_files

    empty = tmp_path / "empty_personas"
    empty.mkdir()
    files = discover_persona_files(empty)
    # Falls back to shipped, so we get the same 6 personas Lyre ships.
    names = {p.stem for p in files}
    assert names == {"owner", "leader", "worker-maintainer", "reviewer-pr",
                     "reviewer-skill", "summary-agent"}
