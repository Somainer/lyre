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


@dataclass(frozen=True)
class CodingBackend:
    """An owner-declared external coding-agent credential bundle (see
    docs/design/CAPABILITY_DISCOVERY.md).

    `shell_exec(credentials="<name>")` injects ``os.environ[auth_env]`` into the
    one subprocess that drives this coding CLI — the agent never sees the value.
    The SECRET stays in the env (`~/.lyre/.env`), same convention as model API
    keys; config holds only the env-var NAME. ``allowed_personas`` optionally
    restricts which personas may invoke this bundle (None = any persona that
    already has shell_exec). The agent discovers HOW to drive the CLI; the owner
    provisions the credential here.
    """

    auth_env: str
    allowed_personas: tuple[str, ...] | None = None


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
    # H1 dead-loop guard: K consecutive identical (tool, args) calls within ONE
    # wakeup → one nudge, then stop the wakeup as needs_continuation. Same-args
    # repetition inside a single synchronous wakeup is degenerate (state can't
    # change between the calls). Default 5 (active); 0 disables. See
    # LONG_RUNNING_ROBUSTNESS_2.md H1.
    loop_repeat_threshold: int = 5
    # A1 per-wakeup wall budget (seconds). The lease heartbeat renews the task
    # lease while a wakeup runs; this caps how long it will do so. When > 0 and
    # a wakeup outlives the budget, the loop is cooperatively stopped
    # (needs_continuation) so a wedged-but-not-repeating wakeup eventually
    # releases its lease for recovery. 0 (default) = no wall: the heartbeat
    # renews indefinitely (a healthy long wakeup is never falsely recovered;
    # H1 + operator cancel still bound the common runaway cases). Opt-in,
    # matching the other safety knobs. See LONG_RUNNING_ROBUSTNESS_2.md A1.
    wakeup_wall_budget_s: int = 0
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
    # O3a per-wakeup TURN budget — the number of model↔tool-loop iterations a
    # single wakeup may run before it's honestly truncated to needs_continuation
    # (distinct from max_tokens above, which caps ONE assistant message). This is
    # the default; a dispatch can raise it per-task via `dispatch_task(max_turns=)`
    # (stored in task.tier_overrides, resolved at the AgentLoop build site). No
    # ceiling — only trusted orchestrators dispatch, and H1/A1 still bound a
    # runaway. 24 matches the long-standing hardcoded default the build site used.
    # See ORCHESTRATION_ROBUSTNESS.md §5 (O3a).
    max_turns: int = 24
    # R1 — LLM transient-error retry budget, passed to the provider SDK client
    # (it retries 408/409/429/500/529 with backoff before raising). 2 matches the
    # SDK default; raise it for flaky providers. Covers the connect / first-token
    # window; a failure AFTER the first token is R2's mid-stream failover, not
    # this. 0 disables SDK retry. See FAILURE_ROBUSTNESS.md §4 (R1).
    llm_max_retries: int = 2
    # C — bound how many times Phase-2 lease recovery silently re-runs a single
    # BOOTSTRAP SINGLETON task (dispatcher etc.) before failing it and escalating
    # to the owner. The singleton is never archived (it must stay reachable for
    # the next mail to revive it via Phase 0). A deterministic setup failure
    # would otherwise re-run forever with nobody notified — the observed
    # "dispatcher wakeup failed, 没人知道" loop. 0 disables the bound. Per-task
    # total (not a time window): singleton recoveries are ~one lease-duration
    # apart. See FAILURE_ROBUSTNESS.md §3 (C).
    singleton_recovery_max: int = 3
    # R2 — per-turn mid-stream failover budget. On a mid-stream LLM failure
    # (some output already streamed), fail over to the next candidate up to this
    # many times instead of killing the wakeup; on exceed, stay fatal. Safe —
    # tools dispatch only post-turn, so a discarded partial has no durable side
    # effect. 0 keeps the old mid-stream-fatal behavior. See FAILURE_ROBUSTNESS.md §5.
    midstream_max_retries: int = 1
    # F2 observability — structured logs to a rotating file under
    # <lyre_home>/logs/ in addition to the console. log_level filters BOTH
    # sinks; log_to_file=False keeps the pre-F2 console-only behavior.
    # The file is JSONL (one structlog event per line) so incidents are a
    # grep/jq away instead of "whatever scrolled past in the terminal" —
    # and wakeup SUBPROCESSES append to the same file, whose stdout was
    # previously discarded save a 512-byte stderr tail.
    log_level: str = "INFO"
    log_to_file: bool = True
    log_dir: Path = field(default_factory=lambda: lyre_home() / "logs")
    log_max_bytes: int = 10 * 1024 * 1024
    log_backup_count: int = 5

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
    # Owner-declared external coding-agent credential bundles, keyed by name
    # (e.g. "codex", "claude"). See CodingBackend + CAPABILITY_DISCOVERY.md.
    # shell_exec(credentials=<name>) injects the named secret into one
    # subprocess. Empty by default — no coding backend is reachable until the
    # owner declares one in config.toml [coding_backends].
    coding_backends: dict[str, CodingBackend] = field(default_factory=dict)
    # Global fan-in barrier TTL (seconds). A backstop ABOVE each group's own
    # `deadline`: when > 0, Phase 0.5 force-expires any `open` fan_in_group
    # older than this, regardless of the per-group deadline (which a coordinator
    # can set up to 24h). 0 (default) DISABLES it — the per-group deadline is
    # the always-on liveness; this is an opt-in global ceiling for operators who
    # want "no barrier lives past N".
    fanin_max_age_s: int = 0
    # Per-agent notes rotation threshold (entries in the `## Auto-summary log`
    # section). When > 0, after each wakeup-end summary append, the oldest
    # entries beyond this count are rotated down into the cold-archive tier
    # (`object_store/notes_archive/agent-<id>.md`), keeping the hot notes file
    # bounded so an agent reading its own notes can't blow the context window.
    # 0 (default) DISABLES rotation — matches "notes persist forever" until an
    # operator opts in. The hand-written region (above the log header) is never
    # touched. See LONG_RUNNING_ROBUSTNESS.md RB-3.
    notes_max_entries: int = 0
    # C4 DB retention (days). When > 0, a low-frequency scheduler maintenance
    # phase (and the `lyre maintenance` CLI) prune terminal/delivered rows older
    # than this — delivered outbox, ended wakeups (keeping the most-recent K per
    # agent), terminal scheduled_mail, resolved/expired fan_in — and checkpoint
    # the WAL. NEVER touches mailbox_messages / blobs / artifacts. 0 (default)
    # DISABLES it (matches "persist forever" until an operator opts in). See
    # LONG_RUNNING_ROBUSTNESS_3.md C4.
    retention_days: int = 0
    # How often the scheduler maintenance phase runs (seconds), when
    # retention_days > 0. Default 6h — maintenance is cheap (delete + WAL
    # checkpoint; full VACUUM is CLI-only) so it needn't run every tick.
    maintenance_interval_s: int = 21600

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
        try:
            _compact_default = float(runtime_raw.get("compact_threshold", 0.7))
        except (ValueError, TypeError):
            # Garbage [runtime] compact_threshold -> default 0.7 rather than
            # crash every CLI command inside Config.from_env(). Matches the
            # defensive parsing of max_tokens/dashboard_port/max_concurrent below.
            _compact_default = 0.7
        compact_threshold = _parse_compact_threshold(
            os.environ.get("LYRE_COMPACT_THRESHOLD"),
            default=_compact_default,
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
        # H1 dead-loop guard threshold: env LYRE_LOOP_REPEAT_THRESHOLD >
        # [runtime] loop_repeat_threshold > default 5. 0 / negative disables;
        # garbage falls back to the default rather than silently disabling it.
        loop_repeat_raw = os.environ.get(
            "LYRE_LOOP_REPEAT_THRESHOLD"
        ) or runtime_raw.get("loop_repeat_threshold")
        try:
            loop_repeat_threshold = (
                int(loop_repeat_raw) if loop_repeat_raw is not None else 5
            )
        except (ValueError, TypeError):
            loop_repeat_threshold = 5
        if loop_repeat_threshold < 0:
            loop_repeat_threshold = 0
        # A1 per-wakeup wall budget: env LYRE_WAKEUP_WALL_BUDGET_S > [runtime]
        # wakeup_wall_budget_s > default 0 (off). Negative/garbage → 0.
        wall_raw = os.environ.get(
            "LYRE_WAKEUP_WALL_BUDGET_S"
        ) or runtime_raw.get("wakeup_wall_budget_s")
        try:
            wakeup_wall_budget_s = int(wall_raw) if wall_raw is not None else 0
        except (ValueError, TypeError):
            wakeup_wall_budget_s = 0
        if wakeup_wall_budget_s < 0:
            wakeup_wall_budget_s = 0
        # O3a per-wakeup turn budget: env LYRE_MAX_TURNS > [runtime] max_turns >
        # default 24. Floor at 1 (a wakeup needs at least one turn); garbage
        # falls back to the default rather than wedging at 0 turns.
        max_turns_raw = os.environ.get("LYRE_MAX_TURNS") or runtime_raw.get(
            "max_turns"
        )
        try:
            max_turns = int(max_turns_raw) if max_turns_raw is not None else 24
        except (ValueError, TypeError):
            max_turns = 24
        if max_turns < 1:
            max_turns = 1
        # R1 LLM SDK retry budget: env LYRE_LLM_MAX_RETRIES > [runtime]
        # llm_max_retries > default 2. Floor at 0 (0 disables SDK retry);
        # garbage falls back to the default.
        llm_retries_raw = os.environ.get("LYRE_LLM_MAX_RETRIES") or runtime_raw.get(
            "llm_max_retries"
        )
        try:
            llm_max_retries = int(llm_retries_raw) if llm_retries_raw is not None else 2
        except (ValueError, TypeError):
            llm_max_retries = 2
        if llm_max_retries < 0:
            llm_max_retries = 0
        # C bootstrap-singleton recovery bound: env LYRE_SINGLETON_RECOVERY_MAX >
        # [runtime] singleton_recovery_max > default 3. Floor 0 (0 disables).
        sing_rec_raw = os.environ.get(
            "LYRE_SINGLETON_RECOVERY_MAX"
        ) or runtime_raw.get("singleton_recovery_max")
        try:
            singleton_recovery_max = (
                int(sing_rec_raw) if sing_rec_raw is not None else 3
            )
        except (ValueError, TypeError):
            singleton_recovery_max = 3
        if singleton_recovery_max < 0:
            singleton_recovery_max = 0
        # R2 mid-stream failover budget: env LYRE_MIDSTREAM_MAX_RETRIES >
        # [runtime] midstream_max_retries > default 1. Floor 0 (0 = old fatal).
        mid_raw = os.environ.get("LYRE_MIDSTREAM_MAX_RETRIES") or runtime_raw.get(
            "midstream_max_retries"
        )
        try:
            midstream_max_retries = int(mid_raw) if mid_raw is not None else 1
        except (ValueError, TypeError):
            midstream_max_retries = 1
        if midstream_max_retries < 0:
            midstream_max_retries = 0
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

        # ---- per-agent notes rotation threshold ----
        # [scheduler] notes_max_entries = N; env `LYRE_NOTES_MAX_ENTRIES` wins.
        # 0 / absent / garbage / negative → disabled (notes never rotated).
        notes_env = os.environ.get("LYRE_NOTES_MAX_ENTRIES")
        notes_toml = scheduler_raw.get("notes_max_entries")
        notes_chosen = notes_env if notes_env is not None else notes_toml
        if notes_chosen is None:
            notes_max_entries = 0
        else:
            try:
                notes_max_entries = int(notes_chosen)
            except (ValueError, TypeError):
                notes_max_entries = 0
        if notes_max_entries < 0:
            notes_max_entries = 0

        # ---- C4 DB retention ----
        # [scheduler] retention_days = N; env `LYRE_RETENTION_DAYS` wins.
        # 0 / absent / garbage / negative → disabled.
        ret_env = os.environ.get("LYRE_RETENTION_DAYS")
        ret_chosen = (
            ret_env if ret_env is not None else scheduler_raw.get("retention_days")
        )
        try:
            retention_days = int(ret_chosen) if ret_chosen is not None else 0
        except (ValueError, TypeError):
            retention_days = 0
        if retention_days < 0:
            retention_days = 0
        # [scheduler] maintenance_interval_s = N; env wins. Floor 60s; default 6h.
        mi_env = os.environ.get("LYRE_MAINTENANCE_INTERVAL_S")
        mi_chosen = (
            mi_env if mi_env is not None
            else scheduler_raw.get("maintenance_interval_s")
        )
        try:
            maintenance_interval_s = (
                int(mi_chosen) if mi_chosen is not None else 21600
            )
        except (ValueError, TypeError):
            maintenance_interval_s = 21600
        if maintenance_interval_s < 60:
            maintenance_interval_s = 60

        # ---- logging ----
        # [logging] section; env wins per the env-beats-toml convention.
        # Defensive parsing throughout — a garbage logging config must not
        # crash every CLI command inside Config.from_env().
        logging_raw = raw.get("logging", {}) or {}
        log_level = str(
            os.environ.get("LYRE_LOG_LEVEL")
            or logging_raw.get("level")
            or "INFO"
        ).upper()
        if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            log_level = "INFO"
        log_to_file_raw = os.environ.get("LYRE_LOG_TO_FILE")
        if log_to_file_raw is not None:
            log_to_file = log_to_file_raw.lower() in ("1", "true", "yes", "on")
        else:
            log_to_file = bool(logging_raw.get("to_file", True))
        log_dir_raw = os.environ.get("LYRE_LOG_DIR") or logging_raw.get("dir")
        log_dir = (
            Path(log_dir_raw).expanduser() if log_dir_raw else home / "logs"
        )
        # Explicit `is not None` (not an `or` chain): a toml 0 is falsy and
        # would silently fall through to the default instead of being
        # floored like the env string "0" — same convention as
        # max_concurrent_tasks above.
        mb_raw = os.environ.get("LYRE_LOG_MAX_BYTES")
        if mb_raw is None:
            mb_raw = logging_raw.get("max_bytes")
        try:
            log_max_bytes = (
                int(mb_raw) if mb_raw is not None else 10 * 1024 * 1024
            )
        except (ValueError, TypeError):
            log_max_bytes = 10 * 1024 * 1024
        if log_max_bytes < 1024:
            log_max_bytes = 1024
        bk_raw = os.environ.get("LYRE_LOG_BACKUPS")
        if bk_raw is None:
            bk_raw = logging_raw.get("backup_count")
        try:
            log_backup_count = int(bk_raw) if bk_raw is not None else 5
        except (ValueError, TypeError):
            log_backup_count = 5
        # Floor 1: RotatingFileHandler(maxBytes>0, backupCount=0) never
        # renames — the file would grow unbounded while paying a
        # close/reopen on every emit past the threshold. Disabling file
        # logging is LYRE_LOG_TO_FILE=0's job, not backup_count=0's.
        if log_backup_count < 1:
            log_backup_count = 1

        # ---- coding-agent credential bundles ----
        # [coding_backends.<name>] auth_env = "..." [allowed_personas = [...]].
        # Each entry needs an auth_env; entries missing it are skipped with a
        # warning rather than crashing startup.
        coding_backends: dict[str, CodingBackend] = {}
        for name, spec in (raw.get("coding_backends") or {}).items():
            if not isinstance(spec, dict) or not spec.get("auth_env"):
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "config.toml [coding_backends.%s] missing 'auth_env'; skipped",
                    name,
                )
                continue
            ap = spec.get("allowed_personas")
            coding_backends[name] = CodingBackend(
                auth_env=str(spec["auth_env"]),
                allowed_personas=tuple(ap) if ap else None,
            )

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
            loop_repeat_threshold=loop_repeat_threshold,
            wakeup_wall_budget_s=wakeup_wall_budget_s,
            max_tokens=max_tokens,
            max_turns=max_turns,
            llm_max_retries=llm_max_retries,
            singleton_recovery_max=singleton_recovery_max,
            midstream_max_retries=midstream_max_retries,
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
            notes_max_entries=notes_max_entries,
            retention_days=retention_days,
            maintenance_interval_s=maintenance_interval_s,
            coding_backends=coding_backends,
            log_level=log_level,
            log_to_file=log_to_file,
            log_dir=log_dir,
            log_max_bytes=log_max_bytes,
            log_backup_count=log_backup_count,
        )

    @property
    def config_toml_path(self) -> Path:
        """Convenience pointer for ``lyre onboard`` and diagnostics."""
        return _config_path(self.lyre_home)

    def is_onboarded(self) -> bool:
        """True iff ``~/.lyre/config.toml`` exists. ``lyre onboard`` writes it."""
        return self.config_toml_path.is_file()
