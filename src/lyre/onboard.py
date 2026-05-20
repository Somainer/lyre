"""``lyre onboard`` — interactive first-run / reconfigure wizard.

Writes (under ``~/.lyre/``):

  * ``config.toml`` — owner identity, paths, runtime knobs, model entries
  * ``.env``        — API keys (chmod 600); pasted-in keys land here
  * ``user.md``     — initial template for owner identity & preferences
  * ``personas/``   — shipped personas copied here as the SSOT
  * ``lyre.db``     — empty DB with migrations applied
  * ``memory/``     — directory skeleton (agent-write area)
  * ``skills/``     — directory skeleton

Designed to be safely re-run: every step asks before overwriting an
existing artifact. Headless / scripted setup: hand-edit ``config.toml`` +
``.env`` yourself, then run ``lyre serve`` (no wizard needed).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

from .persistence.db import init_db
from .persistence.sqlite_impl import SqliteRepositories
from .personas.seed import ensure_user_personas, seed_default_agents, seed_personas
from .runtime.memory import ensure_skeleton
from .runtime.skills import ensure_skills_skeleton

# ---------------------------------------------------------------------------
# Provider catalog
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderProtocol:
    """An adapter protocol offered in the wizard. Two slots — Anthropic-
    compatible and OpenAI-compatible — cover everything Lyre ships an
    adapter for. Custom endpoints (DeepSeek, OpenRouter, Together, vLLM,
    …) are just non-default URLs over the same protocol."""

    key: str                # matches Adapter id: "anthropic" / "openai"
    display: str
    default_endpoint: str   # shown as default in the prompt
    default_env_var: str
    default_model: str      # suggested model name (gets prefixed with key.)


PROTOCOLS: tuple[ProviderProtocol, ...] = (
    ProviderProtocol(
        key="anthropic",
        display="Anthropic-compatible (Claude API, DeepSeek-anthropic, vLLM-anthropic, …)",
        default_endpoint="https://api.anthropic.com",
        default_env_var="ANTHROPIC_API_KEY",
        default_model="claude-sonnet-4-6",
    ),
    ProviderProtocol(
        key="openai",
        display="OpenAI-compatible (OpenAI API, OpenRouter, Together, DeepSeek-OAI, vLLM-OAI, …)",
        default_endpoint="https://api.openai.com/v1",
        default_env_var="OPENAI_API_KEY",
        default_model="gpt-4o",
    ),
)


@dataclass(frozen=True)
class ModelSpec:
    """The minimal information needed to write one ``[[models]]`` block.

    Built by the wizard from the user's protocol + endpoint + env_var + model
    answers. Passed to :func:`write_config_toml` so the generated config
    self-describes exactly what the user picked.
    """

    id: str            # registry id, e.g. "anthropic.deepseek-v4-flash"
    provider: str      # adapter id, e.g. "anthropic" or "openai"
    endpoint: str      # base_url; "" / None means "use SDK default"
    auth_env: str


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def detect_git_user_name() -> str | None:
    try:
        out = subprocess.run(
            ["git", "config", "--global", "user.name"],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    name = out.stdout.strip()
    return name or None


def detect_git_user_email() -> str | None:
    try:
        out = subprocess.run(
            ["git", "config", "--global", "user.email"],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    email = out.stdout.strip()
    return email or None


# ---------------------------------------------------------------------------
# File writers (pure helpers, no click prompts — easy to unit test)
# ---------------------------------------------------------------------------


USER_MD_TEMPLATE = """\
<!--
~/.lyre/user.md — your identity, preferences, and constraints.

Lyre's agents read this file as part of every system prompt — it's the
canonical "who is the human I'm working for" record. Edit it freely;
agents never write here. Agent-authored notes live in ~/.lyre/memory/
(which YOU don't edit, by convention).

Suggested sections below — write whatever shape works for you. The file
is injected verbatim into every system prompt; no parser, no schema.
-->

# About me

## Communication style
-

## Code / decision preferences
-

## Things to avoid
-
"""


def write_user_md_template(user_md_path: Path, *, overwrite: bool = False) -> bool:
    """Write the user.md template if absent. Returns True iff a file was written."""
    if user_md_path.is_file() and not overwrite:
        return False
    user_md_path.parent.mkdir(parents=True, exist_ok=True)
    user_md_path.write_text(USER_MD_TEMPLATE, encoding="utf-8")
    return True


def write_config_toml(
    config_path: Path,
    *,
    owner_name: str,
    owner_email: str | None,
    models: list[ModelSpec] | None = None,
    default_model: str | None = None,
) -> None:
    """Write ``~/.lyre/config.toml`` with the minimum fields onboard sets.

    The file is intentionally sparse — Lyre falls back to defaults for
    every unset field. When ``models`` is provided, the wizard writes one
    ``[[models]]`` block per spec; when ``default_model`` is provided it's
    written under ``[runtime]`` to fix the router's fallback choice.

    Per-persona model assignment is left to the user via ``[personas.<name>]
    model_preference = ...`` (commented example included in the output).
    """
    config_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# ~/.lyre/config.toml — Lyre user configuration",
        "# Generated by `lyre onboard`. Edit freely; re-running the wizard",
        "# is safe and asks before overwriting.",
        "",
        "[owner]",
        f'name = "{_toml_escape(owner_name)}"',
    ]
    if owner_email:
        lines.append(f'email = "{_toml_escape(owner_email)}"')
    lines.append("")

    if default_model:
        lines.append("[runtime]")
        lines.append(f'default_model = "{_toml_escape(default_model)}"')
        lines.append("")

    for model in models or []:
        # Endpoint: empty string ⇒ use adapter's SDK default (TOML can't
        # express `null`, so we omit base_url to mean "default" instead).
        lines.append("[[models]]")
        lines.append(f'id = "{_toml_escape(model.id)}"')
        lines.append(f'provider = "{_toml_escape(model.provider)}"')
        endpoint_inline_parts: list[str] = []
        if model.endpoint:
            endpoint_inline_parts.append(
                f'base_url = "{_toml_escape(model.endpoint)}"'
            )
        endpoint_inline_parts.append(
            f'auth_env = "{_toml_escape(model.auth_env)}"'
        )
        lines.append(
            "endpoint = { " + ", ".join(endpoint_inline_parts) + " }"
        )
        # Sensible defaults for capabilities + tier. Edit in config.toml
        # later if you need flagship / cheap distinctions.
        lines.append('capabilities = ["tool_use", "streaming"]')
        lines.append('tier = "workhorse"')
        lines.append("")

    # Stubs for the user to fill in later.
    lines.extend([
        "# Add more [[models]] blocks here for additional provider/model entries.",
        "# Same id as the shipped registry REPLACES the shipped entry; a new",
        "# id appends. Example for OpenRouter's Qwen3-coder:",
        "#   [[models]]",
        "#   id = \"openrouter.qwen3-coder\"",
        "#   provider = \"openai\"",
        "#   endpoint = { base_url = \"https://openrouter.ai/api/v1\", auth_env = \"OPENROUTER_API_KEY\" }",
        "#   capabilities = [\"tool_use\"]",
        "#   tier = \"workhorse\"",
        "",
        "# Per-persona single-field overrides (whole-file override = edit",
        "# ~/.lyre/personas/<name>/identity.md directly instead):",
        "#   [personas.leader]",
        "#   model_preference = { prefer = [\"anthropic.claude-opus-4-7\"] }",
        "",
    ])
    config_path.write_text("\n".join(lines), encoding="utf-8")


def _toml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def append_env_line(env_path: Path, var_name: str, value: str) -> None:
    """Append (or replace) one ``KEY=value`` line in ``~/.lyre/.env``.

    Always sets ``chmod 600`` on the file after writing.
    """
    env_path.parent.mkdir(parents=True, exist_ok=True)
    existing: list[str] = []
    if env_path.is_file():
        existing = env_path.read_text(encoding="utf-8").splitlines()

    new_line = f"{var_name}={value}"
    rewritten: list[str] = []
    replaced = False
    for line in existing:
        if line.startswith(f"{var_name}="):
            rewritten.append(new_line)
            replaced = True
        else:
            rewritten.append(line)
    if not replaced:
        rewritten.append(new_line)

    env_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    try:
        env_path.chmod(0o600)
    except OSError:
        # Best-effort on non-Unix or read-only mounts.
        pass


# ---------------------------------------------------------------------------
# Provider connection test
# ---------------------------------------------------------------------------


def can_reach_env_var(env_var: str) -> tuple[bool, str]:
    """Light sanity check: is the env var set and does it look plausible?

    A full handshake against the provider is intentionally NOT done here —
    requires network, real API key, model ids that match the user's account.
    Instead we check the basics and let the first real wakeup do the rest.
    Returns (ok, message).
    """
    val = os.environ.get(env_var, "").strip()
    if not val:
        return False, f"${env_var} is not set"
    if len(val) < 8:
        return False, f"${env_var} looks too short ({len(val)} chars)"
    return True, f"${env_var} is set ({len(val)} chars)"


# ---------------------------------------------------------------------------
# Composer
# ---------------------------------------------------------------------------


@dataclass
class OnboardPlan:
    """What the wizard intends to write. Returned for tests + dry-run."""

    config_path: Path
    user_md_path: Path
    env_path: Path
    db_path: Path
    memory_path: Path
    skills_path: Path
    user_personas_dir: Path
    owner_name: str
    owner_email: str | None
    models: list[ModelSpec]               # all configured by the wizard
    default_model: str | None             # router fallback
    api_keys_in_env: list[str]            # env vars already present in shell
    api_keys_written_to_env_file: list[str]  # env vars pasted into ~/.lyre/.env


async def bootstrap_runtime(cfg: Any) -> list[str]:  # noqa: ANN401 — Config
    """Create DB schema, memory dirs, skills skeleton, seed personas + agents.

    Used by ``lyre onboard`` after the wizard, and by tests as a
    non-interactive subset of the wizard. Idempotent.
    Returns the list of newly-created agent ids.
    """
    # Populate ~/.lyre/personas/ from shipped if needed. Idempotent — only
    # copies what's missing, so user edits / deletions are preserved across
    # subsequent boots.
    ensure_user_personas(cfg.user_personas_dir)

    conn = await init_db(cfg.db_path)
    try:
        repos = SqliteRepositories(conn)
        await repos.mailbox.ensure_mailbox("owner")
        await seed_personas(
            repos.personas,
            user_personas_dir=cfg.user_personas_dir,
            persona_overrides=cfg.persona_overrides,
        )
        ensure_skeleton(cfg.memory_path)
        created_agents = await seed_default_agents(
            repos.agents, memory_root=cfg.memory_path,
        )
        ensure_skills_skeleton(cfg.lyre_home)
        return created_agents
    finally:
        await conn.close()


def run_wizard(*, lyre_home: Path) -> OnboardPlan:
    """Interactive flow. Writes ``config.toml`` / ``.env`` / ``user.md`` /
    bootstrap directories under ``lyre_home``.

    DB initialization and persona seeding happen in the CLI command after
    this returns, so the wizard module stays free of async dependencies.
    """
    config_path = lyre_home / "config.toml"
    user_md_path = lyre_home / "user.md"
    env_path = lyre_home / ".env"
    db_path = lyre_home / "lyre.db"
    memory_path = lyre_home / "memory"
    skills_path = lyre_home / "skills"
    user_personas_dir = lyre_home / "personas"

    click.echo("")
    click.echo(click.style("Lyre setup", bold=True))
    click.echo(f"All files land under {lyre_home}.")
    click.echo("")

    # ---- owner identity ----
    default_name = (
        detect_git_user_name() or os.environ.get("USER") or "owner"
    )
    owner_name = click.prompt("Owner name", default=default_name).strip() or default_name

    default_email = detect_git_user_email() or ""
    owner_email_raw = click.prompt(
        "Owner email (optional, press Enter to skip)",
        default=default_email, show_default=bool(default_email),
    ).strip()
    owner_email = owner_email_raw or None

    # ---- models (loop until user picks "done") ----
    models: list[ModelSpec] = []
    api_keys_in_env: list[str] = []
    api_keys_written: list[str] = []

    while True:
        spec = _prompt_for_one_model(
            existing_count=len(models), env_path=env_path,
            api_keys_in_env=api_keys_in_env,
            api_keys_written=api_keys_written,
        )
        if spec is None:
            break  # user picked "Done"
        models.append(spec)

    # ---- default_model picker (only when >1 configured) ----
    default_model: str | None
    if len(models) == 0:
        default_model = None
    elif len(models) == 1:
        default_model = models[0].id
    else:
        click.echo("")
        click.echo("Configured models:")
        for i, m in enumerate(models, start=1):
            endpoint_label = m.endpoint or "(SDK default)"
            click.echo(f"  {i}) {m.id}  →  {endpoint_label}  [${m.auth_env}]")
        idx = click.prompt(
            "Which one is the router's default (used when no persona preference matches)?",
            type=click.IntRange(1, len(models)), default=1,
        )
        default_model = models[idx - 1].id
        click.echo("")
        click.echo(
            "Tip: per-persona model assignment lives in config.toml as\n"
            "  [personas.<name>] model_preference = { prefer = [\"<model.id>\"] }\n"
            "or in the persona's frontmatter at ~/.lyre/personas/<name>/identity.md."
        )

    # ---- user.md template ----
    click.echo("")
    if user_md_path.is_file():
        click.echo(f"  {user_md_path} already exists — leaving untouched.")
    elif click.confirm(
        f"Write user.md template at {user_md_path}?", default=True,
    ):
        write_user_md_template(user_md_path)
        click.echo(f"  ✓ wrote {user_md_path}")

    # ---- config.toml ----
    if config_path.is_file():
        if not click.confirm(
            f"\n{config_path} already exists — overwrite with new owner/provider settings?",
            default=False,
        ):
            click.echo(f"  ! kept existing {config_path}")
        else:
            write_config_toml(
                config_path,
                owner_name=owner_name,
                owner_email=owner_email,
                models=models,
                default_model=default_model,
            )
            click.echo(f"  ✓ wrote {config_path}")
    else:
        write_config_toml(
            config_path,
            owner_name=owner_name,
            owner_email=owner_email,
            models=models,
            default_model=default_model,
        )
        click.echo(f"  ✓ wrote {config_path}")

    # ---- skeleton dirs ----
    memory_path.mkdir(parents=True, exist_ok=True)
    skills_path.mkdir(parents=True, exist_ok=True)
    user_personas_dir.mkdir(parents=True, exist_ok=True)

    return OnboardPlan(
        config_path=config_path,
        user_md_path=user_md_path,
        env_path=env_path,
        db_path=db_path,
        memory_path=memory_path,
        skills_path=skills_path,
        user_personas_dir=user_personas_dir,
        owner_name=owner_name,
        owner_email=owner_email,
        models=models,
        default_model=default_model,
        api_keys_in_env=api_keys_in_env,
        api_keys_written_to_env_file=api_keys_written,
    )


def _prompt_for_one_model(
    *,
    existing_count: int,
    env_path: Path,
    api_keys_in_env: list[str],
    api_keys_written: list[str],
) -> ModelSpec | None:
    """Walk the user through configuring one model entry.

    Returns ``None`` when the user picks "Done" (the loop in
    :func:`run_wizard` uses that to terminate).
    """
    click.echo("")
    if existing_count == 0:
        click.echo("Configure a starter model (custom endpoints supported below):")
    else:
        click.echo(f"Add another model? (you've configured {existing_count} so far):")
    for i, p in enumerate(PROTOCOLS, start=1):
        marker = "✓" if os.environ.get(p.default_env_var) else " "
        click.echo(f"  {i}) [{marker}] {p.display}")
    done_label = "Skip — I'll configure later" if existing_count == 0 else "Done"
    click.echo(f"  {len(PROTOCOLS) + 1}) {done_label}")

    choice = click.prompt(
        "Choice", type=click.IntRange(1, len(PROTOCOLS) + 1),
        default=1 if existing_count == 0 else len(PROTOCOLS) + 1,
    )
    if choice == len(PROTOCOLS) + 1:
        return None

    protocol = PROTOCOLS[choice - 1]
    endpoint = click.prompt(
        "  Endpoint URL",
        default=protocol.default_endpoint, show_default=True,
    ).strip()
    env_var = click.prompt(
        "  API key env var",
        default=protocol.default_env_var, show_default=True,
    ).strip() or protocol.default_env_var
    model_name = click.prompt(
        "  Model id (registry-style: <ns>.<model>; ns matches the entry, not"
        " the brand)",
        default=f"{protocol.key}.{protocol.default_model}", show_default=True,
    ).strip() or f"{protocol.key}.{protocol.default_model}"
    normalized_endpoint = "" if endpoint == protocol.default_endpoint else endpoint
    spec = ModelSpec(
        id=model_name,
        provider=protocol.key,
        endpoint=normalized_endpoint,
        auth_env=env_var,
    )

    # API key handling — only ask once per unique env var across the loop.
    already_seen = env_var in api_keys_in_env or env_var in api_keys_written
    if not already_seen:
        ok, msg = can_reach_env_var(env_var)
        click.echo(f"  {msg}")
        if ok:
            api_keys_in_env.append(env_var)
        else:
            click.echo(
                f"  How do you want to provide {env_var}?\n"
                f"    a) I'll export it in my shell before running lyre\n"
                f"    b) Paste it now; I'll save it to {env_path} (chmod 600)\n"
                f"    c) Skip"
            )
            sub = click.prompt(
                "Choice", type=click.Choice(["a", "b", "c"]), default="a",
            )
            if sub == "b":
                key = click.prompt(f"Paste {env_var}", hide_input=True).strip()
                if key:
                    append_env_line(env_path, env_var, key)
                    api_keys_written.append(env_var)
                    click.echo(f"  ✓ wrote {env_path} (chmod 600)")

    return spec
