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
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

from .persistence.db import init_db
from .persistence.sqlite_impl import SqliteRepositories
from .personas.seed import ensure_user_personas, seed_default_agents, seed_personas
from .runtime.identity import is_valid_agent_id
from .runtime.memory import ensure_shipped_facts, ensure_skeleton
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

    Built by the wizard from the user's protocol + endpoint + env_var +
    model answers. Passed to :func:`write_config_toml` so the generated
    config self-describes exactly what the user picked.

    Two auth modes are supported:
      * `auth_env` non-empty + `headers` empty
            → standard API-key flow; SDK builds the right Authorization
              header from the env var.
      * `auth_env` empty + `headers` non-empty
            → custom-header flow; useful for proxies / gateways with
              their own auth scheme. Header VALUES may use the
              ``${VAR}`` form to read from the environment at startup
              so secrets stay out of config.toml.
    Both can be set together (API key + extra org/project headers).
    """

    id: str            # registry id, e.g. "anthropic.deepseek-v4-flash"
    provider: str      # adapter id, e.g. "anthropic" or "openai"
    endpoint: str      # base_url; "" / None means "use SDK default"
    auth_env: str      # env-var name; empty string for header-only mode
    headers: tuple[tuple[str, str], ...] = ()
    # Only meaningful when provider == "openai" — picks Chat Completions
    # vs Responses dialect. Empty / "chat-completions" → standard path.
    api: str = "chat-completions"


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
    dispatcher_id: str = "dispatcher",
    analyst_id: str = "analyst-1",
    reviewer_id: str = "reviewer-1",
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

    # Owner-facing names for the three role agents (persona names — dispatcher
    # / analyst / reviewer — stay fixed as they're system identifiers). Edit
    # to taste; the names are what `lyre send <name>` and mailbox addressing
    # use.
    needs_bootstrap = (
        dispatcher_id != "dispatcher"
        or analyst_id != "analyst-1"
        or reviewer_id != "reviewer-1"
    )
    if needs_bootstrap:
        lines.append("[bootstrap]")
        lines.append(f'dispatcher_id = "{_toml_escape(dispatcher_id)}"')
        lines.append(f'analyst_id = "{_toml_escape(analyst_id)}"')
        lines.append(f'reviewer_id = "{_toml_escape(reviewer_id)}"')
        lines.append("")

    for model in models or []:
        # Endpoint: empty string ⇒ use adapter's SDK default (TOML can't
        # express `null`, so we omit base_url to mean "default" instead).
        lines.append("[[models]]")
        lines.append(f'id = "{_toml_escape(model.id)}"')
        lines.append(f'provider = "{_toml_escape(model.provider)}"')
        # Capabilities + tier MUST come before any sub-tables we add
        # below — TOML's "sub-tables belong to the most recent header"
        # rule means anything after `[models.endpoint]` would be read
        # as part of endpoint, not as part of the model entry.
        lines.append('capabilities = ["tool_use", "streaming"]')
        lines.append('tier = "workhorse"')
        # `api` only matters for the OpenAI family. Default is
        # "chat-completions"; only write it when non-default to keep
        # the file uncluttered for the common case.
        non_default_api = (
            model.provider == "openai"
            and (model.api or "chat-completions") != "chat-completions"
        )
        if model.headers or non_default_api:
            # Header-only / stacked auth OR a non-default `api` value:
            # write endpoint + nested headers as proper sub-tables.
            # Inline-table syntax can't be extended with a sub-table
            # per TOML spec, so we can't use the compact
            # `endpoint = { ... }` form here.
            lines.append("[models.endpoint]")
            if model.endpoint:
                lines.append(
                    f'base_url = "{_toml_escape(model.endpoint)}"'
                )
            if model.auth_env:
                lines.append(
                    f'auth_env = "{_toml_escape(model.auth_env)}"'
                )
            if non_default_api:
                lines.append(f'api = "{_toml_escape(model.api)}"')
            if model.headers:
                lines.append("[models.endpoint.headers]")
                for name, value in model.headers:
                    lines.append(
                        f'"{_toml_escape(name)}" = "{_toml_escape(value)}"'
                    )
        else:
            # No custom headers + default `api`: inline-table form is
            # fine and reads more compactly in the file.
            endpoint_inline_parts: list[str] = []
            if model.endpoint:
                endpoint_inline_parts.append(
                    f'base_url = "{_toml_escape(model.endpoint)}"'
                )
            if model.auth_env:
                endpoint_inline_parts.append(
                    f'auth_env = "{_toml_escape(model.auth_env)}"'
                )
            if endpoint_inline_parts:
                lines.append(
                    "endpoint = { " + ", ".join(endpoint_inline_parts) + " }"
                )
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
        "#   [personas.dispatcher]",
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
    # Owner-facing names for the seeded bootstrap role agents. Defaults are
    # the persona name itself ("dispatcher" etc.); owner can override here
    # to give them personality ("luna" / "scribe" / "cassandra").
    dispatcher_id: str = "dispatcher"
    analyst_id: str = "analyst-1"
    reviewer_id: str = "reviewer-1"
    # If non-empty, written to ~/.lyre/personas/dispatcher/APPEND.md and
    # injected at the bottom of dispatcher's system prompt every wakeup.
    # This is where the owner gives the dispatcher its voice / style.
    dispatcher_soul: str = ""


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
        ensure_shipped_facts(cfg.memory_path)
        from .personas.seed import _resolved_default_agents
        bootstrap_pairs = _resolved_default_agents(
            dispatcher_id=cfg.bootstrap.dispatcher_id,
            analyst_id=cfg.bootstrap.analyst_id,
            reviewer_id=cfg.bootstrap.reviewer_id,
        )
        created_agents = await seed_default_agents(
            repos.agents, memory_root=cfg.memory_path,
            agents=bootstrap_pairs,
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
    click.echo(click.style("━━━ Lyre setup ━━━", bold=True))
    click.echo(
        "Three short sections. Press Enter on any prompt to accept the "
        "default in brackets."
    )
    click.echo(f"All files land under {lyre_home}.")
    click.echo("")

    # ---- [1/3] owner identity ----
    click.echo(click.style("[1/3] Owner identity", bold=True))
    click.echo(
        "  Used for the dashboard greeting and any owner-addressed mail."
    )
    default_name = (
        detect_git_user_name() or os.environ.get("USER") or "owner"
    )
    owner_name = click.prompt(
        "  Owner name", default=default_name,
    ).strip() or default_name

    default_email = detect_git_user_email() or ""
    owner_email_raw = click.prompt(
        "  Owner email (optional, press Enter to skip)",
        default=default_email, show_default=bool(default_email),
    ).strip()
    owner_email = owner_email_raw or None

    # ---- [2/3] models (loop until user picks "done") ----
    click.echo("")
    click.echo(click.style("[2/3] Model endpoints", bold=True))
    click.echo(
        "  Configure one or more LLM endpoints. You can mix Anthropic and\n"
        "  OpenAI-compatible providers, point at proxies, etc. Pick 'Done'\n"
        "  when finished."
    )
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
            click.echo(f"  {i}) {_model_summary_line(m)}")
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

    # ---- [3/3] files ----
    click.echo("")
    click.echo(click.style("[3/3] Files", bold=True))

    # ---- user.md template ----
    if user_md_path.is_file():
        click.echo(f"  {user_md_path} already exists — leaving untouched.")
    elif click.confirm(
        f"Write user.md template at {user_md_path}?", default=True,
    ):
        write_user_md_template(user_md_path)
        click.echo(f"  ✓ wrote {user_md_path}")

    # ---- bootstrap agent names + soul ----
    # The persona names (dispatcher / analyst / reviewer) are system
    # identifiers — they stay. The AGENT ids are what the owner sees and
    # addresses, so they're free to personalize: "luna" for the dispatcher,
    # whatever. The soul question writes to APPEND.md, which the runtime
    # injects at the bottom of the persona's system prompt every wakeup.
    click.echo("")
    click.echo(click.style("Bootstrap agents", bold=True))
    click.echo(
        "Lyre seeds three role agents. Persona roles are fixed (dispatcher /\n"
        "analyst / reviewer) but the owner-facing AGENT names are yours to pick."
    )
    dispatcher_id = _prompt_agent_id(
        "Dispatcher name (the agent you'll mostly talk to)",
        default="dispatcher",
    )
    analyst_id = _prompt_agent_id(
        "Analyst name (does research, writes specs)",
        default="analyst-1",
    )
    reviewer_id = _prompt_agent_id(
        "Reviewer name (reviews PRs and skill proposals)",
        default="reviewer-1",
    )

    click.echo("")
    click.echo(
        f"Optional: describe {dispatcher_id}'s voice / style / quirks. This goes"
    )
    click.echo(
        "into ~/.lyre/personas/dispatcher/APPEND.md and is appended to its"
    )
    click.echo(
        "system prompt every wakeup. Examples: 'Concise. British understatement.'"
    )
    click.echo("Press Enter to skip.")
    dispatcher_soul = click.prompt(
        f"{dispatcher_id}'s soul (one line)",
        default="", show_default=False,
    ).strip()

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
                dispatcher_id=dispatcher_id,
                analyst_id=analyst_id,
                reviewer_id=reviewer_id,
            )
            click.echo(f"  ✓ wrote {config_path}")
    else:
        write_config_toml(
            config_path,
            owner_name=owner_name,
            owner_email=owner_email,
            models=models,
            default_model=default_model,
            dispatcher_id=dispatcher_id,
            analyst_id=analyst_id,
            reviewer_id=reviewer_id,
        )
        click.echo(f"  ✓ wrote {config_path}")

    # Write dispatcher soul to APPEND.md if provided. We do this AFTER
    # config.toml + skeleton dirs so the personas/ dir is guaranteed to exist.
    if dispatcher_soul:
        dispatcher_dir = user_personas_dir / "dispatcher"
        dispatcher_dir.mkdir(parents=True, exist_ok=True)
        append_path = dispatcher_dir / "APPEND.md"
        # Preserve existing APPEND.md content if any — owner may have hand-
        # crafted it across re-runs.
        existing = append_path.read_text(encoding="utf-8") if append_path.exists() else ""
        if dispatcher_soul not in existing:
            sep = "\n" if existing and not existing.endswith("\n") else ""
            append_path.write_text(
                existing + sep + f"\n# Voice & style\n{dispatcher_soul}\n",
                encoding="utf-8",
            )
            click.echo(f"  ✓ wrote {append_path}")

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
        dispatcher_id=dispatcher_id,
        analyst_id=analyst_id,
        reviewer_id=reviewer_id,
        dispatcher_soul=dispatcher_soul,
    )


def _model_summary_line(m: ModelSpec) -> str:
    """One-line description of a configured model entry, used in the
    default-model picker and the final wizard summary. Adapts to the
    auth mode so header-only entries don't show an empty `[$]`."""
    endpoint_label = m.endpoint or "(SDK default)"
    if m.auth_env and m.headers:
        auth_label = f"key:${m.auth_env} + {len(m.headers)} header(s)"
    elif m.auth_env:
        auth_label = f"key:${m.auth_env}"
    elif m.headers:
        auth_label = f"{len(m.headers)} custom header(s)"
    else:
        auth_label = "(no auth!)"
    return f"{m.id}  →  {endpoint_label}  [{auth_label}]"


# Header names per RFC 7230 §3.2.6 token rule — but we narrow further:
# letters, digits, hyphen, underscore. Catches typos like "X Token"
# (space) without rejecting any header name a real provider asks for.
_HEADER_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


def _is_valid_header_name(name: str) -> bool:
    return bool(_HEADER_NAME_RE.match(name))


def _normalize_agent_id_input(raw: str, *, default: str) -> tuple[str, str | None]:
    """Massage owner-typed agent-id input into a legal id.

    Returns ``(value, hint)`` where ``hint`` is a message to echo back if
    we adjusted the input (auto-lowercased it), or ``None`` if the value
    was already fine.

    Empty/whitespace falls back to ``default``. If the result still doesn't
    match :func:`is_valid_agent_id`, returns ``("", error)`` so the caller
    can re-prompt.
    """
    s = raw.strip()
    if not s:
        return default, None
    lowered = s.lower()
    if not is_valid_agent_id(lowered):
        return (
            "",
            f"  ✗ {s!r} isn't a valid agent id. Use lowercase letters / "
            f"digits / hyphens; must start with a letter. Try again.",
        )
    if lowered != s:
        return lowered, f"  ✓ stored as {lowered!r} (agent ids are lowercase)."
    return lowered, None


def _prompt_agent_id(message: str, *, default: str) -> str:
    """Prompt loop until the owner types something that passes the grammar.

    Auto-lowercases input that's otherwise valid (so natural names like
    "Subaru" become "subaru" with a one-line confirmation), and re-prompts
    on truly invalid input (spaces, punctuation, etc.).
    """
    while True:
        raw = click.prompt(message, default=default)
        value, hint = _normalize_agent_id_input(raw, default=default)
        if not value:
            click.echo(hint, err=True)
            continue
        if hint:
            click.echo(hint)
        return value


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

    # Within the OpenAI family, ask the API dialect. Anthropic has one
    # shape today so no sub-question. Default is the historical
    # chat-completions surface; "responses" is opt-in for users on the
    # newer /v1/responses endpoint (some corporate gateways).
    api = "chat-completions"
    if protocol.key == "openai":
        click.echo("")
        click.echo("  Which OpenAI API surface does this endpoint expose?")
        click.echo("    1) Chat Completions (POST /v1/chat/completions) — default")
        click.echo("    2) Responses        (POST /v1/responses)        — newer; some gateways")
        api_choice = click.prompt(
            "  Choice", type=click.IntRange(1, 2), default=1,
        )
        api = "responses" if api_choice == 2 else "chat-completions"

    endpoint = click.prompt(
        "  Endpoint URL",
        default=protocol.default_endpoint, show_default=True,
    ).strip()

    # Auth mode picker. Default = API key (standard case). Header mode
    # is for proxies / gateways with their own auth scheme (signed JWT,
    # mTLS-passthrough token, internal SSO, …). The two are stackable
    # but the wizard treats them as exclusive — header-mode users
    # rarely also have an API key, and stacking can be configured by
    # editing config.toml directly afterwards.
    click.echo("")
    click.echo("  How does this endpoint authenticate?")
    click.echo("    1) API key (env var; standard provider auth)")
    click.echo("    2) Custom HTTP headers (no API key — proxy / gateway)")
    auth_mode = click.prompt(
        "  Choice", type=click.IntRange(1, 2), default=1,
    )

    env_var = ""
    headers: list[tuple[str, str]] = []
    if auth_mode == 1:
        env_var = click.prompt(
            "  API key env var",
            default=protocol.default_env_var, show_default=True,
        ).strip() or protocol.default_env_var
    else:
        click.echo("")
        click.echo("  Enter HTTP headers one at a time. Blank header-name to finish.")
        click.echo("  Common patterns:")
        click.echo(
            click.style(
                "    Authorization        Bearer ${MY_PROXY_TOKEN}\n"
                "    X-API-Key            ${MY_PROXY_TOKEN}\n"
                "    X-Internal-JWT       ${INTERNAL_JWT}",
                fg="cyan",
            )
        )
        click.echo(
            "  Values may use ${ENV_VAR} (the whole value, not a "
            "substring) to read from the environment at startup so the\n"
            "  secret never lands in config.toml."
        )
        while True:
            name = click.prompt(
                "  Header name", default="", show_default=False,
            ).strip()
            if not name:
                break
            if not _is_valid_header_name(name):
                click.echo(
                    f"  ✗ {name!r} is not a valid header name "
                    f"(letters/digits/-/_, must start with a letter). "
                    f"Try again."
                )
                continue
            value = click.prompt(
                f"  Value for {name}", default="", show_default=False,
            ).strip()
            if not value:
                click.echo("  (empty value — skipped)")
                continue
            # Resolve-time check: if the value is a pure ${VAR}, peek
            # at the env now and warn if it's unset. Doesn't block —
            # the user might set it later in ~/.lyre/.env.
            interp = re.match(r"^\$\{([A-Z_][A-Z0-9_]*)\}$", value)
            if interp:
                env_name = interp.group(1)
                if not os.environ.get(env_name):
                    click.echo(
                        f"    ⚠ ${env_name} is not currently set — "
                        f"remember to export it or add it to "
                        f"{env_path}."
                    )
            headers.append((name, value))
        if not headers:
            click.echo(
                "  No headers entered. Falling back to API-key mode."
            )
            env_var = protocol.default_env_var

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
        headers=tuple(headers),
        api=api,
    )

    # API key handling — only when API-key mode is selected and only
    # once per unique env var across the loop.
    if env_var:
        already_seen = (
            env_var in api_keys_in_env or env_var in api_keys_written
        )
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
                    key = click.prompt(
                        f"Paste {env_var}", hide_input=True,
                    ).strip()
                    if key:
                        append_env_line(env_path, env_var, key)
                        api_keys_written.append(env_var)
                        click.echo(f"  ✓ wrote {env_path} (chmod 600)")
    else:
        click.echo(
            f"  ✓ Using header-only auth ({len(headers)} header"
            f"{'s' if len(headers) != 1 else ''})."
        )

    return spec
