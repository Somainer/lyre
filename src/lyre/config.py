"""Config loading from ``~/.lyre/config.toml`` + ``.env`` chain.

Precedence (highest to lowest, first match wins per variable):

  1. Shell env vars (already set when CLI runs)
  2. CWD ``.env`` (dev override)
  3. ``~/.lyre/.env`` (user-level secrets like API keys)
  4. ``~/.lyre/config.toml`` (user-level config: owner, paths, models, runtime)
  5. Hard-coded defaults

API keys are env-only; they never live in ``config.toml``. The wizard
``lyre onboard`` writes ``~/.lyre/config.toml`` + ``~/.lyre/.env`` (the latter
``chmod 600``).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def lyre_home() -> Path:
    """``$LYRE_HOME`` if set (mostly for tests / multi-profile use), else ``~/.lyre``."""
    override = os.environ.get("LYRE_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".lyre"


def _default_db_path(home: Path) -> Path:
    return home / "lyre.db"


def _default_memory_path(home: Path) -> Path:
    """``~/.lyre/memory/`` — agent-authored notes / facts / per-agent scratchpads.

    Agent-write-only by convention; the user reads but doesn't edit. User
    identity / preferences (user-write-only) live separately at
    ``~/.lyre/user.md``.
    """
    return home / "memory"


def _default_object_store_path(home: Path) -> Path:
    return home / "object_store"


def _default_user_md_path(home: Path) -> Path:
    """``~/.lyre/user.md`` — owner identity & preferences. User-write-only;
    agents never write here. Injected verbatim into every system prompt.
    Named ``user.md`` to align with conventional agent-framework naming
    (the file that describes *who the human is*)."""
    return home / "user.md"


def _default_env_path(home: Path) -> Path:
    return home / ".env"


def _default_user_personas_dir(home: Path) -> Path:
    """``~/.lyre/personas/<name>.md`` whole-file overrides for shipped personas."""
    return home / "personas"


def _config_path(home: Path) -> Path:
    return home / "config.toml"


# ---------------------------------------------------------------------------
# .env chain
# ---------------------------------------------------------------------------


def _find_repo_root_with_pyproject() -> Path | None:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return None


_DOTENV_LOADED = False


def load_dotenv_chain() -> list[Path]:
    """Load ``.env`` files into the process env. Existing env vars always win.

    Order applied (so later doesn't override earlier, ``override=False``):

      1. ``~/.lyre/.env``  — user secrets (the canonical place)
      2. CWD ``.env``      — dev override
      3. repo-root ``.env``  (dev convenience when running from a checkout)

    Returns the paths actually loaded, for log output.
    """
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return []
    loaded: list[Path] = []

    home_env = _default_env_path(lyre_home())
    if home_env.is_file():
        load_dotenv(home_env, override=False)
        loaded.append(home_env.resolve())

    cwd_env = Path.cwd() / ".env"
    if cwd_env.is_file() and cwd_env.resolve() not in loaded:
        load_dotenv(cwd_env, override=False)
        loaded.append(cwd_env.resolve())

    root = _find_repo_root_with_pyproject()
    if root is not None:
        root_env = root / ".env"
        if root_env.is_file() and root_env.resolve() not in loaded:
            load_dotenv(root_env, override=False)
            loaded.append(root_env.resolve())

    _DOTENV_LOADED = True
    return loaded


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_compact_threshold(raw: str | None, default: float = 0.7) -> float:
    """Validate fraction of context_window for auto-compaction. (0, 1) only."""
    if raw is None:
        return default
    try:
        v = float(raw)
    except (ValueError, TypeError):
        return default
    if not (0.0 < v < 1.0):
        return default
    return v


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OwnerConfig:
    """Identity of the human owner of this Lyre instance."""

    name: str
    email: str | None = None


@dataclass(frozen=True)
class ModelEntry:
    """User-supplied model registry entry; merges into the shipped registry.

    Fields mirror ``model_registry.yaml`` so the same struct shape works.
    ``context_window`` and ``cost_per_mtok`` are optional — when omitted
    AND the entry's id matches a shipped registry entry, the shipped
    values are inherited (see ``runtime.model_registry.merge_user_entries``).
    """

    id: str
    provider: str
    endpoint: dict[str, Any]
    capabilities: list[str]
    tier: str
    enabled: bool = True
    prefer: list[str] | None = None
    notes: str | None = None
    # Token count for auto-compact gating + dashboard "ctx N%" display.
    # Almost no reason to write this in config.toml — the same id in
    # the shipped registry has a sane value (e.g. 128000 for DeepSeek).
    # Kept here so users who DO want a custom value have a path.
    context_window: int | None = None
    # Per-million-token pricing. Same inheritance story.
    cost_per_mtok: dict[str, float] | None = None


@dataclass(frozen=True)
class PersonaOverride:
    """Single-field overrides for a shipped persona. Whole-file override is
    done by dropping ``<name>.md`` into ``~/.lyre/personas/`` instead."""

    model_preference: dict[str, Any] | None = None
    allowed_lyre_tools: list[str] | None = None


@dataclass(frozen=True)
class LarkConfig:
    """Lark/Feishu bot channel integration.

    The bot becomes the owner's mailbox over IM: agent→owner mail
    surfaces in Lark; messages from ``authorized_user_id`` become
    mail to agents (default recipient: dispatcher persona's seeded
    singleton — its current display_name from identity.md);
    overridable with `@<agent_id>` prefix or by replying in an
    existing thread).

    Secrets (``app_id`` / ``app_secret``) live in ``~/.lyre/.env``,
    not in config.toml — same convention as model API keys. Only the
    non-sensitive identifying info goes in toml.
    """

    enabled: bool = False
    # Lark **open_id** (``ou_xxx...``) whose messages the bot treats as
    # coming from the owner. Anyone else's messages are silently ignored
    # — prevents random tenant members from injecting tasks.
    #
    # NB: this is the open_id, NOT user_id / employee_id. Using open_id
    # avoids the contact:user.employee_id:readonly scope requirement on
    # outbound sends (Lark equates user_id with employee_id). The field
    # name stayed ``authorized_user_id`` for historical config compat
    # — what you put here should be the ``ou_…`` form.
    authorized_user_id: str | None = None
    # Lark app credentials from .env (LARK_APP_ID / LARK_APP_SECRET).
    # Populated by Config.from_env() reading the env vars; NOT loaded
    # from config.toml so secrets stay out of group-readable files.
    app_id: str | None = None
    app_secret: str | None = None


@dataclass(frozen=True)
class IntegrationsConfig:
    """Top-level holder for external channel configs.

    Each external IM/chat channel that can act as the owner's
    mailbox surface gets a sub-config here. The runtime composes a
    `ChannelRegistry` from the enabled ones; downstream code (outbox
    dispatcher, owner-mail enqueuer) is channel-agnostic — it sees
    the registry, not individual channel types.
    """

    lark: LarkConfig = field(default_factory=LarkConfig)
    # Future: slack: SlackConfig, discord: DiscordConfig, etc.


def _default_home() -> Path:
    return lyre_home()


@dataclass(frozen=True)
class Config:
    # ---- required: existing fields kept first so test helpers still work ----
    db_path: Path
    object_store_path: Path
    memory_path: Path
    anthropic_api_key: str | None
    anthropic_base_url: str | None
    default_model: str

    # ---- defaults: existing runtime knobs ----
    model_override: str | None = None
    compact_threshold: float = 0.7
    # Per-turn output budget passed to the LLM (``max_tokens``) — i.e.
    # the cap on a single assistant message, NOT a lifetime budget.
    # Long-running ≠ large per-turn output; what really sets this floor
    # is the biggest single tool-call argument body Lyre agents emit:
    #
    #   dispatcher / reviewer  ≤ 3k   (mail bodies, dispatch args)
    #   analyst                ≤ 8k   (spec writes via python_exec)
    #   worker-maintainer      ≤ 20k+ (code / diffs via python_exec /
    #                                  shell_exec — the hot path)
    #
    # Extended-thinking models also share this budget between thinking
    # and output. 32k is generous enough for all of the above on any
    # modern flagship; cheap-tier models that can't honor it return a
    # clear API error rather than silently truncating, so the cost of
    # overshooting is low. Set lower if you specifically want to box
    # in a runaway worker.
    max_tokens: int = 32768

    # ---- defaults: paths added with config.toml ----
    lyre_home: Path = field(default_factory=_default_home)
    user_md_path: Path = field(default_factory=lambda: _default_user_md_path(lyre_home()))
    env_path: Path = field(default_factory=lambda: _default_env_path(lyre_home()))
    user_personas_dir: Path = field(
        default_factory=lambda: _default_user_personas_dir(lyre_home())
    )

    # ---- defaults: owner / models / persona overrides ----
    owner: OwnerConfig = field(default_factory=lambda: OwnerConfig(name="owner"))
    models: list[ModelEntry] = field(default_factory=list)
    persona_overrides: dict[str, PersonaOverride] = field(default_factory=dict)
    integrations: IntegrationsConfig = field(default_factory=IntegrationsConfig)

    # ---- defaults: runtime knobs added with config.toml ----
    default_dashboard_port: int = 8765
    auto_wake_on_mail: bool = True
    # How many tasks the scheduler may have in flight at once. Only
    # honored in subprocess mode — inline mode is single-threaded by
    # design and ignores this. Default 4 (a sensible single-user
    # ceiling: dispatcher + a few workers + analyst can all make
    # progress in parallel without saturating a laptop). SQLite WAL +
    # 10s busy_timeout cover cross-process write contention; the
    # ceiling exists for predictable CPU / model-API rate-limit
    # behavior, not because more would break anything.
    max_concurrent_tasks: int = 4
    # Idle-reclaim threshold (seconds). When > 0, `list_agents` marks an
    # AGENT-spawned, NON-ephemeral agent that has been idle this long (no
    # in-flight task, not a leg of an open fan-in barrier) as `stale` — a hint
    # to the Dispatcher that it may archive it. The runtime never auto-archives
    # on this signal: reclaim is a Dispatcher decision (pull model). Only the
    # transient children agents spawn for themselves are ever flagged; the
    # owner's own creations are protected outright (parent_agent_id 'owner') —
    # as are bootstrap singletons (parent_agent_id NULL) and ephemeral agents
    # (the reaper's job) — so a standing specialist the owner wants is never at
    # risk. 0 (default) DISABLES the hint entirely — fitting Lyre's "agents
    # persist across restarts" default; opt in per deployment.
    idle_reclaim_age_s: int = 0
    # Global fan-in barrier TTL (seconds). A backstop ABOVE each group's own
    # `deadline`: when > 0, Phase 0.5 force-expires any `open` fan_in_group
    # older than this, regardless of the per-group deadline (which a coordinator
    # can set up to 24h). 0 (default) DISABLES it — the per-group deadline is
    # the always-on liveness; this is an opt-in global ceiling for operators who
    # want "no barrier lives past N".
    fanin_max_age_s: int = 0

    @classmethod
    def from_env(cls) -> Config:
        """Build the Config from ``~/.lyre/config.toml`` + env + defaults.

        Idempotent: safe to call from every CLI command. Missing config.toml
        is fine — ``lyre onboard`` is what creates one. Without it, you get
        defaults + env-only behaviour (which is what tests and CI use).
        """
        load_dotenv_chain()

        home = lyre_home()
        toml_path = _config_path(home)
        raw = _read_toml(toml_path)

        # ---- paths ----
        paths = raw.get("paths", {}) or {}

        def _path(env_var: str, toml_key: str, default: Path) -> Path:
            v = os.environ.get(env_var) or paths.get(toml_key)
            return Path(v).expanduser() if v else default

        db_path = _path("LYRE_DB_PATH", "db_path", _default_db_path(home))
        memory_path = _path("LYRE_MEMORY_PATH", "memory_path", _default_memory_path(home))
        object_store_path = _path(
            "LYRE_OBJECT_STORE", "object_store", _default_object_store_path(home)
        )
        user_md_path = _path("LYRE_USER_MD_PATH", "user_md_path", _default_user_md_path(home))
        env_path = _default_env_path(home)
        user_personas_dir = _path(
            "LYRE_USER_PERSONAS_DIR",
            "user_personas_dir",
            _default_user_personas_dir(home),
        )

        # Auto-create the directories Lyre writes into. Doesn't materialize
        # user.md or config.toml — that's onboard's job.
        for p in (db_path.parent, memory_path, object_store_path, user_personas_dir):
            p.mkdir(parents=True, exist_ok=True)

        # ---- owner ----
        owner_raw = raw.get("owner") or {}
        owner_name = os.environ.get("LYRE_OWNER_NAME") or owner_raw.get("name")
        if not owner_name:
            # Tests + CI may run without onboarding. Use a placeholder; the
            # onboard wizard is the canonical way to set this.
            owner_name = "owner"
        owner_email = os.environ.get("LYRE_OWNER_EMAIL") or owner_raw.get("email")
        owner = OwnerConfig(name=owner_name, email=owner_email)

        # ---- models (additional registry entries) ----
        models_raw = raw.get("models", []) or []
        models: list[ModelEntry] = []
        for m in models_raw:
            try:
                cost_raw = m.get("cost_per_mtok")
                models.append(
                    ModelEntry(
                        id=m["id"],
                        provider=m["provider"],
                        endpoint=dict(m.get("endpoint") or {}),
                        capabilities=list(m.get("capabilities") or []),
                        tier=m["tier"],
                        enabled=bool(m.get("enabled", True)),
                        prefer=list(m["prefer"]) if m.get("prefer") else None,
                        notes=m.get("notes"),
                        context_window=m.get("context_window"),
                        cost_per_mtok=(
                            dict(cost_raw) if isinstance(cost_raw, dict) else None
                        ),
                    )
                )
            except KeyError as exc:
                raise ValueError(
                    f"~/.lyre/config.toml [[models]] entry missing field: {exc.args[0]!r}"
                ) from exc

        # ---- persona overrides ----
        personas_raw = raw.get("personas", {}) or {}
        persona_overrides: dict[str, PersonaOverride] = {}
        for name, fields in personas_raw.items():
            persona_overrides[name] = PersonaOverride(
                model_preference=fields.get("model_preference"),
                allowed_lyre_tools=(
                    list(fields["allowed_lyre_tools"])
                    if fields.get("allowed_lyre_tools") is not None
                    else None
                ),
            )

        # ---- runtime knobs ----
        runtime_raw = raw.get("runtime", {}) or {}
        default_model = (
            os.environ.get("LYRE_DEFAULT_MODEL")
            or runtime_raw.get("default_model")
            or "claude-sonnet-4-6"
        )
        model_override = os.environ.get("LYRE_MODEL_OVERRIDE") or runtime_raw.get(
            "model_override"
        ) or None
        compact_threshold = _parse_compact_threshold(
            os.environ.get("LYRE_COMPACT_THRESHOLD"),
            default=float(runtime_raw.get("compact_threshold", 0.7)),
        )
        # max_tokens: env > [runtime] > default. Floor at 256 so a
        # misconfigured 0 / negative doesn't immediately starve every
        # wakeup. Default 32k — see Config docstring on max_tokens for
        # the per-turn-output reasoning.
        max_tokens_raw = (
            os.environ.get("LYRE_MAX_TOKENS")
            or runtime_raw.get("max_tokens")
        )
        try:
            max_tokens = (
                max(256, int(max_tokens_raw)) if max_tokens_raw is not None
                else 32768
            )
        except (ValueError, TypeError):
            max_tokens = 32768
        dashboard_port_raw = os.environ.get("LYRE_DASHBOARD_PORT") or runtime_raw.get(
            "default_dashboard_port", 8765
        )
        try:
            dashboard_port = int(dashboard_port_raw)
        except (ValueError, TypeError):
            dashboard_port = 8765
        auto_wake = runtime_raw.get("auto_wake_on_mail", True)
        env_auto = os.environ.get("LYRE_AUTO_WAKE_ON_MAIL")
        if env_auto is not None:
            auto_wake = env_auto.lower() in ("1", "true", "yes", "on")

        # ---- scheduler concurrency ----
        # [scheduler] max_concurrent_tasks = N in config.toml; env var
        # `LYRE_MAX_CONCURRENT_TASKS` wins (matches the existing
        # env-beats-toml convention for runtime knobs).
        # Use explicit `is not None` rather than `or` chain — `or`
        # treats 0 as falsy and falls through to the default, which
        # would silently reactivate parallelism the user tried to
        # disable with `max_concurrent_tasks = 0`.
        scheduler_raw = raw.get("scheduler", {}) or {}
        env_raw = os.environ.get("LYRE_MAX_CONCURRENT_TASKS")
        toml_raw = scheduler_raw.get("max_concurrent_tasks")
        chosen = env_raw if env_raw is not None else toml_raw
        if chosen is None:
            max_concurrent = 4
        else:
            try:
                max_concurrent = int(chosen)
            except (ValueError, TypeError):
                # Garbage input → default 4 rather than crash startup.
                max_concurrent = 4
        if max_concurrent < 1:
            # Explicit 0 / negative is the user asking for serial —
            # clamp to 1 (NOT 4), so a deliberate "disable parallelism"
            # signal is honored.
            max_concurrent = 1

        # ---- idle-reclaim threshold ----
        # [scheduler] idle_reclaim_age_s = N; env `LYRE_IDLE_RECLAIM_AGE`
        # wins (same env-beats-toml convention). 0 / absent / garbage →
        # disabled (no stale hint). Negative is clamped to 0.
        idle_env = os.environ.get("LYRE_IDLE_RECLAIM_AGE")
        idle_toml = scheduler_raw.get("idle_reclaim_age_s")
        idle_chosen = idle_env if idle_env is not None else idle_toml
        if idle_chosen is None:
            idle_reclaim_age_s = 0
        else:
            try:
                idle_reclaim_age_s = int(idle_chosen)
            except (ValueError, TypeError):
                idle_reclaim_age_s = 0
        if idle_reclaim_age_s < 0:
            idle_reclaim_age_s = 0

        # ---- global fan-in TTL ----
        # [scheduler] fanin_max_age_s = N; env `LYRE_FANIN_MAX_AGE` wins.
        # 0 / absent / garbage / negative → disabled (per-group deadline rules).
        fanin_env = os.environ.get("LYRE_FANIN_MAX_AGE")
        fanin_toml = scheduler_raw.get("fanin_max_age_s")
        fanin_chosen = fanin_env if fanin_env is not None else fanin_toml
        if fanin_chosen is None:
            fanin_max_age_s = 0
        else:
            try:
                fanin_max_age_s = int(fanin_chosen)
            except (ValueError, TypeError):
                fanin_max_age_s = 0
        if fanin_max_age_s < 0:
            fanin_max_age_s = 0

        # ---- legacy [bootstrap] deprecation warning ----
        # The old [bootstrap] section let the owner pin dispatcher_id /
        # analyst_id / reviewer_id. Those moved to persona identity.md
        # frontmatter (display_name field). Surface a one-shot warning
        # so anyone with a stale config notices the migration.
        if raw.get("bootstrap"):
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "config.toml [bootstrap] section is deprecated and ignored. "
                "Set display_name in each persona's identity.md frontmatter "
                "instead (~/.lyre/personas/<name>/identity.md)."
            )

        # ---- external channel integrations ----
        # config.toml carries non-sensitive identifiers + the enable
        # flag; secrets (app_id/app_secret) come from .env so they
        # don't leak into group-readable files.
        integrations_raw = raw.get("integrations") or {}
        lark_raw = integrations_raw.get("lark") or {}
        lark_cfg = LarkConfig(
            enabled=bool(lark_raw.get("enabled", False)),
            authorized_user_id=lark_raw.get("authorized_user_id") or None,
            app_id=os.environ.get("LARK_APP_ID") or None,
            app_secret=os.environ.get("LARK_APP_SECRET") or None,
        )
        integrations = IntegrationsConfig(lark=lark_cfg)

        return cls(
            db_path=db_path,
            object_store_path=object_store_path,
            memory_path=memory_path,
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
            anthropic_base_url=os.environ.get("ANTHROPIC_BASE_URL"),
            default_model=default_model,
            model_override=model_override,
            compact_threshold=compact_threshold,
            max_tokens=max_tokens,
            lyre_home=home,
            user_md_path=user_md_path,
            env_path=env_path,
            user_personas_dir=user_personas_dir,
            owner=owner,
            models=models,
            persona_overrides=persona_overrides,
            integrations=integrations,
            default_dashboard_port=dashboard_port,
            auto_wake_on_mail=bool(auto_wake),
            max_concurrent_tasks=max_concurrent,
            idle_reclaim_age_s=idle_reclaim_age_s,
            fanin_max_age_s=fanin_max_age_s,
        )

    @property
    def config_toml_path(self) -> Path:
        """Convenience pointer for ``lyre onboard`` and diagnostics."""
        return _config_path(self.lyre_home)

    def is_onboarded(self) -> bool:
        """True iff ``~/.lyre/config.toml`` exists. ``lyre onboard`` writes it."""
        return self.config_toml_path.is_file()
