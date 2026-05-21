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
    """

    id: str
    provider: str
    endpoint: dict[str, Any]
    capabilities: list[str]
    tier: str
    enabled: bool = True
    prefer: list[str] | None = None
    notes: str | None = None


@dataclass(frozen=True)
class PersonaOverride:
    """Single-field overrides for a shipped persona. Whole-file override is
    done by dropping ``<name>.md`` into ``~/.lyre/personas/`` instead."""

    model_preference: dict[str, Any] | None = None
    allowed_lyre_tools: list[str] | None = None


@dataclass(frozen=True)
class BootstrapConfig:
    """Customizable agent identities for the bootstrap-seeded agents.

    The PERSONA is always one of the shipped roles (dispatcher / analyst /
    reviewer) — this is a system identifier the runtime keys off. The
    AGENT id, however, is what the owner sees and addresses — `lyre send
    luna "..."`, dashboard column "Luna's mailbox", etc. Owner-personal,
    purely cosmetic from runtime's perspective.

    Soul / style customization goes through the existing APPEND.md
    mechanism on the persona directory — see runtime.context system-prompt
    assembly. There's no field here for it because it's a file-content
    affair, not a config affair.
    """

    dispatcher_id: str = "dispatcher"
    analyst_id: str = "analyst-1"
    reviewer_id: str = "reviewer-1"


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
    bootstrap: BootstrapConfig = field(default_factory=BootstrapConfig)

    # ---- defaults: runtime knobs added with config.toml ----
    default_dashboard_port: int = 8765
    auto_wake_on_mail: bool = True
    # How many tasks the scheduler may have in flight at once. Only
    # honored in subprocess mode — inline mode is single-threaded by
    # design and ignores this. Default 1 preserves the historical
    # serial behavior; raise it (3-4 is plenty for personal use) to
    # let agents work in parallel. SQLite WAL + 10s busy_timeout
    # cover cross-process write contention.
    max_concurrent_tasks: int = 1

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
        scheduler_raw = raw.get("scheduler", {}) or {}
        try:
            max_concurrent = int(
                os.environ.get("LYRE_MAX_CONCURRENT_TASKS")
                or scheduler_raw.get("max_concurrent_tasks")
                or 1
            )
        except (ValueError, TypeError):
            max_concurrent = 1
        if max_concurrent < 1:
            # Treat 0 / negative as "disabled, fall back to 1" rather
            # than failing startup — a typo in config shouldn't stop
            # the daemon.
            max_concurrent = 1

        # ---- bootstrap agent id overrides ----
        bootstrap_raw = raw.get("bootstrap") or {}
        bootstrap = BootstrapConfig(
            dispatcher_id=str(bootstrap_raw.get("dispatcher_id", "dispatcher")),
            analyst_id=str(bootstrap_raw.get("analyst_id", "analyst-1")),
            reviewer_id=str(bootstrap_raw.get("reviewer_id", "reviewer-1")),
        )

        return cls(
            db_path=db_path,
            object_store_path=object_store_path,
            memory_path=memory_path,
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
            anthropic_base_url=os.environ.get("ANTHROPIC_BASE_URL"),
            default_model=default_model,
            model_override=model_override,
            compact_threshold=compact_threshold,
            lyre_home=home,
            user_md_path=user_md_path,
            env_path=env_path,
            user_personas_dir=user_personas_dir,
            owner=owner,
            models=models,
            persona_overrides=persona_overrides,
            bootstrap=bootstrap,
            default_dashboard_port=dashboard_port,
            auto_wake_on_mail=bool(auto_wake),
            max_concurrent_tasks=max_concurrent,
        )

    @property
    def config_toml_path(self) -> Path:
        """Convenience pointer for ``lyre onboard`` and diagnostics."""
        return _config_path(self.lyre_home)

    def is_onboarded(self) -> bool:
        """True iff ``~/.lyre/config.toml`` exists. ``lyre onboard`` writes it."""
        return self.config_toml_path.is_file()
