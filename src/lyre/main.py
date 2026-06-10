"""Lyre CLI entrypoint."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from datetime import UTC
from typing import TYPE_CHECKING, Any

import click

if TYPE_CHECKING:
    import aiosqlite
import structlog

from .config import Config, load_dotenv_chain, lyre_home
from .outbox.dispatcher import OutboxDispatcher
from .persistence.db import init_db
from .persistence.models import MailboxMessage, TaskSpec
from .persistence.sqlite_impl import SqliteRepositories
from .runtime.adapter_factory import AdapterFactory
from .runtime.blob_store import BlobStore
from .scheduler.scheduler import Scheduler

log = structlog.get_logger()


def _setup_logging() -> None:
    """Console-only baseline for every CLI command — runs in the group
    callback BEFORE Config is loaded, so it can't know the log dir yet.
    Long-lived entry points (serve / dashboard / run-task) upgrade to
    file logging via _configure_process_logging once Config is in hand;
    one-shot commands (audit, tail, …) stay console-only."""
    from .logging_setup import configure_logging

    configure_logging(log_dir=None)


def _configure_process_logging(cfg: Config, *, rotate: bool) -> None:
    """File + console logging for a long-lived process. ``rotate=False``
    for wakeup subprocesses — only the serve/dashboard process may
    rotate the shared file (see logging_setup module docstring)."""
    from .logging_setup import configure_logging

    path = configure_logging(
        level=cfg.log_level,
        log_dir=cfg.log_dir if cfg.log_to_file else None,
        max_bytes=cfg.log_max_bytes,
        backup_count=cfg.log_backup_count,
        rotate=rotate,
    )
    if path is not None and rotate:
        # Only the rotating owner announces — one line per serve start,
        # not one per wakeup subprocess.
        click.echo(f"logging to {path} (level={cfg.log_level})", err=True)


@click.group()
def cli() -> None:
    """Lyre — long-running personal multi-agent team."""
    _setup_logging()
    loaded = load_dotenv_chain()
    for path in loaded:
        click.echo(f"loaded .env from {path}", err=True)


# ----------------------------------------------------------------------
# lyre onboard
# ----------------------------------------------------------------------

@cli.command("onboard")
def onboard_cmd() -> None:
    """Interactive first-run / reconfigure wizard.

    Writes ``~/.lyre/config.toml``, ``~/.lyre/.env``, ``~/.lyre/user.md``,
    copies shipped personas into ``~/.lyre/personas/``, initializes the
    database, and seeds default agents. Safe to re-run — every overwrite
    is gated by a confirmation prompt.
    """
    from .onboard import bootstrap_runtime, run_wizard

    cfg = Config.from_env()
    plan = run_wizard(lyre_home=cfg.lyre_home, current_cfg=cfg)

    # Wizard wrote config.toml + env + user.md + dirs; reload Config so the
    # bootstrap sees fresh owner_name / paths.
    bootstrap_cfg = Config.from_env()
    click.echo("")
    click.echo(f"Initializing DB at {bootstrap_cfg.db_path}")
    created_agents = asyncio.run(bootstrap_runtime(bootstrap_cfg))
    click.echo(f"  ✓ DB ready, {bootstrap_cfg.memory_path} dirs ensured")
    if created_agents:
        click.echo(f"  ✓ created default agents: {', '.join(created_agents)}")

    click.echo("")
    click.echo(click.style("All set.", bold=True))
    click.echo(f"  Owner: {plan.owner_name}" + (
        f" <{plan.owner_email}>" if plan.owner_email else ""
    ))
    if plan.models:
        from .onboard import _model_summary_line
        click.echo(f"  Models configured ({len(plan.models)}):")
        for m in plan.models:
            mark = " (default)" if m.id == plan.default_model else ""
            click.echo(f"    • {_model_summary_line(m)}{mark}")
    click.echo("")
    click.echo("Next:")
    click.echo("  lyre serve                 # start the runtime + dashboard")
    click.echo('  lyre send dispatcher "hi"  # send a test message')


# ----------------------------------------------------------------------
# lyre serve
# ----------------------------------------------------------------------

# Backward-compat alias — the original test added in the serve-crash fix
# imports `_model_entry_reachable` from this module. The implementation
# now lives in runtime/adapter_factory.py so the router can use it too.
from .runtime.adapter_factory import entry_reachable as _model_entry_reachable  # noqa: E402


@cli.command("serve")
@click.option("--poll-interval", default=1.0, type=float, help="Seconds between polls")
@click.option(
    "--dashboard/--no-dashboard", default=True,
    help="Run the web dashboard alongside scheduler + dispatcher (default on)",
)
@click.option(
    "--dashboard-host", default="127.0.0.1", show_default=True,
)
@click.option(
    "--dashboard-port", default=8765, show_default=True, type=int,
)
@click.option(
    "--subprocess/--no-subprocess", "use_subprocess", default=True,
    help="Run each task in a fresh `lyre run-task` subprocess (OS-level "
    "isolation per 铁律 2). Default on — Lyre is a long-running daemon "
    "and subprocess mode is what unlocks `max_concurrent_tasks` "
    "parallelism. Pass --no-subprocess for inline debugging.",
)
def serve_cmd(
    poll_interval: float,
    dashboard: bool,
    dashboard_host: str,
    dashboard_port: int,
    use_subprocess: bool,
) -> None:
    """Start scheduler + outbox dispatcher (+ dashboard) in one process."""

    async def _run() -> None:
        from .runtime.model_registry import load_registry_for_config

        cfg = Config.from_env()
        _configure_process_logging(cfg, rotate=True)
        # Each model entry has its own auth config — either an `auth_env`
        # holding an API key, custom HTTP headers (proxy / gateway mode),
        # or both stacked. Validate that at least one enabled entry can
        # actually authenticate before starting, so we fail loudly here
        # instead of silently on the first dispatched task.
        registry = load_registry_for_config(cfg)
        # Make registry provenance visible at startup. The most common
        # config-not-loading symptom (file at wrong path, [[models]]
        # under wrong section, missing config.toml) used to show up as
        # a confusing NoEligibleModelError listing only SHIPPED entries
        # — this log line explains why before that error fires.
        if cfg.models:
            click.echo(
                f"Loaded {len(cfg.models)} model entr"
                f"{'y' if len(cfg.models) == 1 else 'ies'} from "
                f"{lyre_home() / 'config.toml'}; shipped defaults "
                "ignored."
            )
            for m in cfg.models:
                ep = m.endpoint or {}
                auth_summary = (
                    "$" + ep["auth_env"] if ep.get("auth_env")
                    else (
                        f"{len(ep.get('headers', {}))} header(s)"
                        if ep.get("headers") else "(no auth!)"
                    )
                )
                click.echo(f"  • {m.id} [{m.tier}, {auth_summary}]")
        else:
            click.echo(
                f"No [[models]] in {lyre_home() / 'config.toml'} "
                "(file missing or empty) — using shipped registry "
                "defaults. Run `lyre onboard` to configure your own."
            )
        candidates = registry.enabled()
        if cfg.model_override:
            candidates = [e for e in candidates if e.id == cfg.model_override]
            if not candidates:
                click.echo(
                    f"ERROR: LYRE_MODEL_OVERRIDE={cfg.model_override!r} "
                    f"but no enabled entry with that id in model_registry.yaml.",
                    err=True,
                )
                sys.exit(1)

        reachable = [e for e in candidates if _model_entry_reachable(e)]
        if not reachable:
            # Build a human-actionable hint listing what's missing.
            needed_env_vars = sorted({
                e.endpoint.auth_env for e in candidates if e.endpoint.auth_env
            })
            header_entries = [
                e.id for e in candidates if not e.endpoint.auth_env
            ]
            msg_parts = [
                "ERROR: no enabled model entry can authenticate.",
            ]
            if needed_env_vars:
                msg_parts.append(
                    "API-key entries need one of these env vars set: "
                    + ", ".join(needed_env_vars)
                )
            if header_entries:
                msg_parts.append(
                    "Header-only entries with no headers configured: "
                    + ", ".join(header_entries)
                    + " (edit ~/.lyre/config.toml [models.endpoint.headers]"
                    + " sub-table)"
                )
            msg_parts.append(
                "Or set LYRE_MODEL_OVERRIDE=<id> to pin to a model whose "
                "auth you have."
            )
            click.echo("\n".join(msg_parts), err=True)
            sys.exit(1)
        click.echo(
            f"LLM reachable for {len(reachable)}/{len(candidates)} model "
            f"entr{'y' if len(candidates) == 1 else 'ies'}: "
            + ", ".join(e.id for e in reachable)
        )

        # Bootstrap the runtime if needed — `lyre serve` after `lyre onboard`
        # works in one step. Phase 0 needs at least owner+dispatcher present.
        from .onboard import bootstrap_runtime
        await bootstrap_runtime(cfg)

        conn = await init_db(cfg.db_path)
        try:
            repos = SqliteRepositories(
                conn,
                personas_dir=cfg.user_personas_dir,
                persona_overrides=cfg.persona_overrides,
            )

            # One BlobStore per process — adapter factory resolves
            # image/document blocks through it at send-time; dashboard
            # /send route writes uploaded bytes through it and /blobs/
            # <id> serves them back.
            blob_store = BlobStore(cfg.object_store_path)

            scheduler = Scheduler(
                repos, cfg,
                poll_interval_s=poll_interval,
                spawn_subprocess=use_subprocess,
                adapter_factory=AdapterFactory(
                    blob_store=blob_store, max_retries=cfg.llm_max_retries
                ),
            )
            # Build the external-channel registry from cfg.integrations.
            # Channels register themselves into the registry; the outbox
            # dispatcher routes `channel_publish` rows by name. Empty
            # registry = integrations disabled (today's default) and
            # the rest of the runtime is unaffected.
            from .integrations import ChannelRegistry
            channel_registry = ChannelRegistry()
            if cfg.integrations.lark.enabled:
                from .integrations.lark import LarkChannel
                # The default recipient for unaddressed inbound messages
                # is the dispatcher persona's current bootstrap-seeded
                # agent id (display_name from identity.md). Resolved at
                # startup; restart picks up renames.
                dispatcher_persona = await repos.personas.get("dispatcher")
                dispatcher_id = (
                    (dispatcher_persona.display_name
                     or dispatcher_persona.name)
                    if dispatcher_persona is not None else "dispatcher"
                )
                try:
                    channel_registry.register(LarkChannel(
                        cfg.integrations.lark,
                        repos,
                        blob_store,
                        dispatcher_id=dispatcher_id,
                    ))
                except ValueError as exc:
                    click.echo(
                        f"WARNING: Lark integration enabled but disabled "
                        f"at startup: {exc}",
                        err=True,
                    )

            dispatcher = OutboxDispatcher(
                repos,
                poll_interval_s=poll_interval,
                channel_registry=channel_registry,
            )

            # Shared stop_event so SIGINT halts all services together.
            stop_event = asyncio.Event()
            loop = asyncio.get_running_loop()

            # Owner-mail enqueuer + one broadcaster per channel run.
            # Broadcaster lives for the whole serve lifetime so the
            # enqueuer can subscribe.
            owner_broadcaster = None
            owner_enqueuer = None
            if channel_registry:
                from .dashboard.sse import MailboxBroadcaster
                from .integrations.owner_mail_enqueuer import (
                    OwnerMailEnqueuer,
                )
                owner_broadcaster = MailboxBroadcaster(
                    repos=repos, recipient="owner",
                    poll_interval_s=poll_interval,
                )
                await owner_broadcaster.prime()
                await owner_broadcaster.start()
                owner_enqueuer = OwnerMailEnqueuer(repos, channel_registry)

            def _stop_all() -> None:
                stop_event.set()
                scheduler.request_stop()
                dispatcher.request_stop()
                if owner_enqueuer is not None:
                    owner_enqueuer.request_stop()

            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, _stop_all)

            services: list[asyncio.Task[None]] = [
                asyncio.create_task(scheduler.run(), name="scheduler"),
                asyncio.create_task(dispatcher.run(), name="outbox_dispatcher"),
            ]
            # Spawn each registered channel + the owner-mail enqueuer.
            for channel in channel_registry.values():
                services.append(asyncio.create_task(
                    channel.run(stop_event),
                    name=f"channel:{channel.name}",
                ))
                click.echo(f"  channel {channel.name} enabled")
            if owner_enqueuer is not None and owner_broadcaster is not None:
                services.append(asyncio.create_task(
                    owner_enqueuer.run(owner_broadcaster),
                    name="owner_mail_enqueuer",
                ))
            mode = "subprocess-isolated" if use_subprocess else "inline"
            click.echo(
                f"Lyre scheduler ({mode}) + outbox dispatcher started."
            )
            if dashboard:
                from .dashboard.runner import run_dashboard as _run_dash

                def _ready(url: str) -> None:
                    click.echo(f"Lyre dashboard at {url}")

                # Pass per-model context_window into the dashboard so the
                # activity feed can show "peak / window" percentages on
                # wakeup_end events.
                ctx_windows = {
                    e.id: e.context_window
                    for e in registry.entries
                    if e.context_window
                }
                services.append(
                    asyncio.create_task(
                        _run_dash(
                            repos,
                            host=dashboard_host,
                            port=dashboard_port,
                            stop_event=stop_event,
                            on_ready=_ready,
                            model_context_windows=ctx_windows,
                            owner_name=cfg.owner.name,
                            blob_store=blob_store,
                            object_store_root=cfg.object_store_path,
                        ),
                        name="dashboard",
                    )
                )
            else:
                click.echo("  dashboard disabled (--no-dashboard)")
            click.echo("Ctrl-C to stop.")

            try:
                await asyncio.gather(*services)
            except asyncio.CancelledError:
                pass
            finally:
                # If gather returned via a peer crash (real exception)
                # rather than cooperative Ctrl-C, the surviving service
                # tasks are still running on the shared aiosqlite conn.
                # Stop them cooperatively, then cancel+await as a backstop,
                # so none issue queries on the connection we close below
                # (avoids 'operation on a closed database' and 'Task
                # exception was never retrieved' noise on the crash path).
                # On clean Ctrl-C every task is already done -> no-op.
                _stop_all()
                for t in services:
                    if not t.done():
                        t.cancel()
                await asyncio.gather(*services, return_exceptions=True)
                # Owner-mail broadcaster runs alongside the enqueuer;
                # stop it explicitly so its poll task doesn't hold
                # the loop open after services drain.
                if owner_broadcaster is not None:
                    await owner_broadcaster.stop()
        finally:
            await conn.close()

    asyncio.run(_run())


# ----------------------------------------------------------------------
# lyre dispatch
# ----------------------------------------------------------------------

@cli.command("dispatch")
@click.argument("persona")
@click.argument("goal")
@click.option("--acceptance", default="任务跑通即可（Sprint 0 smoke test）", help="验收标准")
def dispatch_cmd(persona: str, goal: str, acceptance: str) -> None:
    """Write a task to the queue. Scheduler will pick it up."""

    async def _run() -> None:
        cfg = Config.from_env()
        conn = await init_db(cfg.db_path)
        try:
            repos = SqliteRepositories(
                conn,
                personas_dir=cfg.user_personas_dir,
                persona_overrides=cfg.persona_overrides,
            )
            spec = TaskSpec(persona_name=persona, goal=goal, acceptance=acceptance)
            task_id = await repos.tasks.create(spec)
            click.echo(f"Task dispatched: {task_id} (persona={persona})")
        finally:
            await conn.close()

    asyncio.run(_run())


# ----------------------------------------------------------------------
# lyre maintenance  (C4)
# ----------------------------------------------------------------------

@cli.command("maintenance")
@click.option(
    "--retention-days", type=int, default=None,
    help="Override [scheduler] retention_days for this run.",
)
@click.option(
    "--vacuum/--no-vacuum", default=True,
    help="Run VACUUM to reclaim file space (default on for manual runs).",
)
def maintenance_cmd(retention_days: int | None, vacuum: bool) -> None:
    """Prune terminal/delivered DB rows past the retention window + reclaim space.

    Deletes delivered outbox, ended wakeups (keeping the most-recent per agent),
    terminal scheduled_mail, and resolved/expired fan_in groups older than the
    window; checkpoints the WAL and (by default) VACUUMs. NEVER touches
    mailbox_messages, blobs, or artifacts.

    The scheduler runs this automatically (without VACUUM) when retention_days>0;
    this command is for on-demand / full-reclaim runs.
    """

    async def _run() -> None:
        from .persistence.maintenance import run_maintenance

        cfg = Config.from_env()
        rd = retention_days if retention_days is not None else cfg.retention_days
        if rd <= 0:
            click.echo(
                "retention_days is 0 (disabled). Pass --retention-days N to prune.",
                err=True,
            )
            sys.exit(1)
        conn = await init_db(cfg.db_path)
        try:
            counts = await run_maintenance(conn, retention_days=rd, vacuum=vacuum)
        finally:
            await conn.close()
        click.echo(f"Maintenance done (retention_days={rd}, vacuum={vacuum}):")
        for table, n in counts.items():
            click.echo(f"  {table:16s} {n} rows pruned")

    asyncio.run(_run())


# ----------------------------------------------------------------------
# lyre status <task_id>
# ----------------------------------------------------------------------

@cli.command("status")
@click.argument("task_id")
def status_cmd(task_id: str) -> None:
    """Show a task's status + checkpoint + latest wakeup transcript URI."""

    async def _run() -> None:
        cfg = Config.from_env()
        conn = await init_db(cfg.db_path)
        try:
            repos = SqliteRepositories(
                conn,
                personas_dir=cfg.user_personas_dir,
                persona_overrides=cfg.persona_overrides,
            )
            task = await repos.tasks.get(task_id)
            if task is None:
                click.echo(f"No such task: {task_id}", err=True)
                sys.exit(1)
            # The docstring promised wakeups + transcript; deliver them.
            # Run-state is an open wakeup (ended_at IS NULL), not task.status.
            wakeups = await repos.wakeups.list_for_task(task_id, limit=10)
            children = await repos.tasks.find_children(task_id)
            active = next((w for w in wakeups if w.ended_at is None), None)
            out = {
                "task": task.model_dump(mode="json"),
                "is_running": active is not None,
                "active_wakeup_id": active.id if active else None,
                "children": [
                    {
                        "id": c.id, "persona": c.persona_name,
                        "agent_id": c.agent_id, "status": c.status,
                    }
                    for c in children
                ],
                "wakeups": [w.model_dump(mode="json") for w in wakeups],
            }
            click.echo(json.dumps(out, indent=2, ensure_ascii=False))
        finally:
            await conn.close()

    asyncio.run(_run())


# ----------------------------------------------------------------------
# lyre persona-refresh <name> — pull a shipped persona update into ~/.lyre
# ----------------------------------------------------------------------

@cli.command("persona-refresh")
@click.argument("name", required=False)
@click.option("--all", "all_personas", is_flag=True, help="Refresh every shipped persona.")
@click.option(
    "--backup/--no-backup", default=True, show_default=True,
    help="Back up the current identity.md before overwriting.",
)
def persona_refresh_cmd(name: str | None, all_personas: bool, backup: bool) -> None:
    """Pull a shipped persona update into ~/.lyre/personas/<name>/identity.md.

    Shipped persona EDITS don't reach an already-onboarded install — identity.md
    is the user SSOT and onboarding never overwrites it. This re-copies the
    shipped version on demand, backing up your current identity.md first.
    Personas are read from the filesystem, so it's live on the next wakeup.
    """
    from .personas.seed import refresh_user_persona, shipped_persona_names

    cfg = Config.from_env()
    known = shipped_persona_names()
    if all_personas:
        targets = known
    elif name:
        if name not in known:
            click.echo(
                f"Unknown shipped persona: {name}. Known: {', '.join(known)}", err=True
            )
            sys.exit(1)
        targets = [name]
    else:
        click.echo(f"Pass a persona name or --all. Known: {', '.join(known)}", err=True)
        sys.exit(1)

    for n in targets:
        identity, bak = refresh_user_persona(cfg.user_personas_dir, n, backup=backup)
        if bak is not None:
            click.echo(f"  backed up: {bak}")
        click.echo(f"refreshed persona '{n}' -> {identity}")


# ----------------------------------------------------------------------
# lyre run-task <task_id> — subprocess entry point
# ----------------------------------------------------------------------

@cli.command("run-task", hidden=True)
@click.argument("task_id")
def run_task_cmd(task_id: str) -> None:
    """Run a single task to completion in THIS process. Intended to be
    invoked as a subprocess by the Scheduler when `spawn_subprocess` is on
    (per 铁律 2). Not normally invoked by hand.

    Env variables it reads:
      LYRE_DB_PATH / LYRE_OBJECT_STORE / LYRE_MEMORY_PATH (Config)
      ANTHROPIC_API_KEY / DEEPSEEK_API_KEY (LLM)
      LYRE_MOCK_ADAPTER_SCRIPT (testing only — path to a JSONL stream script)
    """

    async def _run() -> None:
        cfg = Config.from_env()
        # rotate=False: this subprocess appends to the shared log file
        # but must never rotate it — that's the serve process's job.
        _configure_process_logging(cfg, rotate=False)
        conn = await init_db(cfg.db_path)
        try:
            repos = SqliteRepositories(
                conn,
                personas_dir=cfg.user_personas_dir,
                persona_overrides=cfg.persona_overrides,
            )
            adapter_for_test = None
            mock_script = os.getenv("LYRE_MOCK_ADAPTER_SCRIPT")
            if mock_script:
                from pathlib import Path as _P

                from .adapter.mock_jsonl import MockJsonlAdapter

                # One adapter PER persona/wakeup is fine because the subprocess
                # runs exactly one task. We reuse the same instance across
                # turn calls — MockJsonlAdapter holds its turn queue inside.
                _shared = MockJsonlAdapter(_P(mock_script))
                adapter_for_test = lambda _entry: _shared  # noqa: E731

            blob_store = BlobStore(cfg.object_store_path)
            scheduler = Scheduler(
                repos, cfg,
                poll_interval_s=1.0,
                spawn_subprocess=False,  # we ARE the subprocess
                adapter_for_test=adapter_for_test,
                adapter_factory=AdapterFactory(
                    blob_store=blob_store, max_retries=cfg.llm_max_retries
                ),
            )
            await scheduler._run_task_inline(task_id)
        finally:
            await conn.close()

    asyncio.run(_run())


# ----------------------------------------------------------------------
# lyre dashboard (Sprint D1) — standalone web UI; no scheduler
# ----------------------------------------------------------------------

@cli.command("dashboard")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8765, show_default=True, type=int)
def dashboard_cmd(host: str, port: int) -> None:
    """Start the Lyre dashboard standalone (no scheduler / dispatcher).

    For the all-in-one experience use `lyre serve` (default includes the
    dashboard; pass `--no-dashboard` to opt out)."""
    from .dashboard.runner import run_dashboard

    async def _run() -> None:
        from .runtime.model_registry import load_registry_for_config

        cfg = Config.from_env()
        _configure_process_logging(cfg, rotate=True)
        registry = load_registry_for_config(cfg)
        ctx_windows = {
            e.id: e.context_window
            for e in registry.entries
            if e.context_window
        }
        conn = await init_db(cfg.db_path)
        try:
            repos = SqliteRepositories(
                conn,
                personas_dir=cfg.user_personas_dir,
                persona_overrides=cfg.persona_overrides,
            )
            stop_event = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop_event.set)
            await run_dashboard(
                repos,
                host=host,
                port=port,
                stop_event=stop_event,
                on_ready=lambda url: click.echo(f"Lyre dashboard at {url}"),
                model_context_windows=ctx_windows,
                owner_name=cfg.owner.name,
                blob_store=BlobStore(cfg.object_store_path),
                object_store_root=cfg.object_store_path,
            )
        finally:
            await conn.close()

    asyncio.run(_run())


# ----------------------------------------------------------------------
# lyre send <to> <body>
# ----------------------------------------------------------------------

@cli.command("send")
@click.argument("recipient")
@click.argument("body")
@click.option(
    "--title",
    default=None,
    help="Subject line (≤140 char). Readers see only the title in their "
         "inbox listing. Omitted → derived from body's first line.",
)
@click.option(
    "--urgency",
    type=click.Choice(["blocker", "high", "normal", "low"]),
    default="normal",
    help="urgency tier (default normal)",
)
@click.option(
    "--from", "sender",
    default="owner",
    help="sender persona name (default owner)",
)
@click.option(
    "--task-id", default=None,
    help="optional task_id this message relates to",
)
@click.option(
    "--at", "deliver_at", default=None,
    help="ISO 8601 UTC. Future-mail: deliver at this absolute time (e.g. 2026-06-01T09:00:00Z).",
)
@click.option(
    "--in", "deliver_in", default=None,
    help="Future-mail: deliver in <N>m/h/d/w (e.g. 2h, 1d, 1w).",
)
@click.option(
    "--recur-every", default=None,
    help="Recurrence interval: <N>m/h/d/w. Mutually exclusive with --recur-cron.",
)
@click.option(
    "--recur-cron", default=None,
    help='5-field POSIX cron, e.g. "0 9 * * 1-5". Mutually exclusive with --recur-every.',
)
@click.option(
    "--until", "recur_until", default=None,
    help="ISO 8601 UTC. Stop recurrence after this. Default: first_fire + 1 year.",
)
@click.option(
    "--no-spawn", is_flag=True, default=False,
    help="Reject unknown `persona/name` recipients instead of "
         "spawning the agent on the fly. Use when you want a strict "
         "'this agent must already exist' check.",
)
@click.option(
    "--thread-id", "thread_id", default=None,
    help="主线 (thread) id to attach / continue. Omitted → a new thread is "
         "minted. The runtime then carries it through replies, dispatched "
         "tasks, and result-mail so an agent's wakeups stay on-thread.",
)
def send_cmd(
    recipient: str, body: str, title: str | None,
    urgency: str, sender: str, task_id: str | None,
    deliver_at: str | None, deliver_in: str | None,
    recur_every: str | None, recur_cron: str | None, recur_until: str | None,
    no_spawn: bool, thread_id: str | None,
) -> None:
    """Send a mailbox message to an agent.

    `recipient` is an AGENT ID. Bare ids (`owner`, `dispatcher`,
    `analyst-1`, `reviewer-1` — or whatever display_names the owner set
    in identity.md) reach bootstrap-seeded agents directly. Spawned
    agents use `persona/name` (e.g. `worker-maintainer/refactor-auth`);
    if that id doesn't exist yet the CLI auto-creates it (use
    `--no-spawn` to disable). `lyre agent list` shows live agent ids.
    """

    async def _run() -> None:
        from .runtime.identity import (
            is_valid_agent_id,
            split_id,
        )
        cfg = Config.from_env()
        conn = await init_db(cfg.db_path)
        try:
            import uuid as _uuid

            repos = SqliteRepositories(
                conn,
                personas_dir=cfg.user_personas_dir,
                persona_overrides=cfg.persona_overrides,
            )
            # Owner seeds a 主线: continue an explicit one, else mint a fresh
            # thread for this concern. Propagation downstream is the runtime's.
            thread = thread_id or f"thread-{_uuid.uuid4().hex[:16]}"
            # Resolve / auto-spawn:
            #   - "owner": always valid (human mailbox at the edge)
            #   - bare bootstrap id: must exist (no spawning bootstrap agents)
            #   - bare unknown id: error (anti-hallucination — `leader-scheduler`)
            #   - persona/name: spawn if missing AND --no-spawn not set
            if recipient == "owner":
                pass
            elif not is_valid_agent_id(recipient):
                click.echo(
                    f"invalid agent id {recipient!r}: must be a bare id "
                    f"(owner / dispatcher / analyst-1 / reviewer-1, or your "
                    f"custom equivalents) or `<persona>/<name>`. See "
                    f"`lyre agent list` for live agents.",
                    err=True,
                )
                sys.exit(1)
            elif not await repos.agents.exists(recipient):
                persona, name = split_id(recipient)
                # Bare names (no `/`) must already exist — they can't be
                # spawned because there's no persona side to validate.
                if name is None:
                    live = sorted({a.id for a in await repos.agents.list_all()} | {"owner"})
                    click.echo(
                        f"unknown agent {recipient!r}. Known: {live}. "
                        f"Pass an existing agent id, or use `persona/name` "
                        f"to auto-spawn.",
                        err=True,
                    )
                    sys.exit(1)
                if no_spawn:
                    click.echo(
                        f"agent {recipient!r} doesn't exist and "
                        f"--no-spawn was passed; refusing to create it.",
                        err=True,
                    )
                    sys.exit(1)
                persona_row = await repos.personas.get(persona)
                if persona_row is None or persona_row.status != "approved":
                    click.echo(
                        f"can't spawn agent {recipient!r}: persona "
                        f"{persona!r} is not an approved persona. "
                        f"`lyre persona list` for the valid set.",
                        err=True,
                    )
                    sys.exit(1)
                await repos.agents.create(
                    agent_id=recipient,
                    persona_name=persona,
                    parent_agent_id=sender or "owner",
                )
                click.echo(f"spawned agent {recipient} (persona={persona})")
            await repos.mailbox.ensure_mailbox(recipient)

            # Future-mail branch — any of --at/--in/--recur-* flips this on.
            if any(
                v is not None
                for v in (deliver_at, deliver_in, recur_every, recur_cron, recur_until)
            ):
                from datetime import datetime

                from .persistence.models import ScheduledMail
                from .runtime.future_mail import (
                    PastDeliveryError,
                    default_recur_until,
                    iso,
                    now_utc,
                    parse_duration,
                    resolve_first_fire,
                    validate_cron,
                )

                if recur_every is not None and recur_cron is not None:
                    click.echo("--recur-every and --recur-cron are mutually exclusive", err=True)
                    sys.exit(2)
                try:
                    if recur_cron is not None:
                        validate_cron(recur_cron)
                    if recur_every is not None:
                        parse_duration(recur_every)
                    first_fire = resolve_first_fire(
                        deliver_at=deliver_at,
                        deliver_in=deliver_in,
                        recur_cron=recur_cron,
                        now=now_utc(),
                    )
                except PastDeliveryError as exc:
                    click.echo(str(exc), err=True)
                    sys.exit(1)
                except ValueError as exc:
                    click.echo(str(exc), err=True)
                    sys.exit(1)

                recur_kind: str | None = None
                recur_value: str | None = None
                if recur_every is not None:
                    recur_kind, recur_value = "interval", recur_every
                elif recur_cron is not None:
                    recur_kind, recur_value = "cron", recur_cron

                ru = None
                if recur_kind is not None:
                    if recur_until is not None:
                        try:
                            ru = datetime.fromisoformat(
                                recur_until.replace("Z", "+00:00")
                            )
                        except ValueError:
                            click.echo(
                                f"--until must be ISO 8601 UTC; got {recur_until!r}",
                                err=True,
                            )
                            sys.exit(1)
                        if ru <= first_fire:
                            click.echo(
                                f"--until must be after first delivery "
                                f"({iso(first_fire)})",
                                err=True,
                            )
                            sys.exit(1)
                    else:
                        ru = default_recur_until(first_fire)

                sid = await repos.scheduled_mail.create(
                    ScheduledMail(
                        recipient=recipient,
                        sender=sender,
                        urgency=urgency,  # type: ignore[arg-type]
                        title=title,
                        body=body,
                        task_id=task_id,
                        scheduled_for=first_fire,
                        recur_kind=recur_kind,  # type: ignore[arg-type]
                        recur_value=recur_value,
                        recur_until=ru,
                        created_by_agent=sender,
                        metadata={"thread_id": thread},
                    )
                )
                click.echo(
                    f"scheduled [{sid}] {urgency} {sender} → {recipient} at "
                    f"{iso(first_fire)}"
                )
                if recur_kind:
                    click.echo(
                        f"  recurring: {recur_kind}={recur_value}"
                        + (f" until {iso(ru)}" if ru else "")
                    )
                click.echo(f"  thread: {thread}  (reuse with --thread-id to continue)")
                return

            msg = MailboxMessage(
                recipient=recipient,
                external_id=f"cli:{_uuid.uuid4()}",
                sender=sender,
                urgency=urgency,  # type: ignore[arg-type]
                title=title,
                body=body,
                task_id=task_id,
                metadata={"thread_id": thread},
            )
            msg_id = await repos.mailbox.insert_message(msg)
            if msg_id < 0:
                click.echo("(duplicate external_id, no row written)", err=True)
                sys.exit(1)
            click.echo(
                f"sent [{msg_id}] {urgency} from {sender} → {recipient}: "
                f"{body[:80]}{'…' if len(body) > 80 else ''}"
            )
            click.echo(f"  thread: {thread}  (reuse with --thread-id to continue)")

            # Visibility hint: how this delivery actually reaches the agent.
            if recipient == "owner":
                pass  # owner has no agent; mailbox is a passive human inbox
            elif urgency == "low":
                click.echo(
                    f"  → urgency=low is pure archive; {recipient} is NOT "
                    "auto-woken. Agent only sees it if it explicitly reads."
                )
            else:
                # normal, high, blocker — all reach the agent
                active = await repos.tasks.find_active_for_persona(recipient)
                if active:
                    if urgency == "blocker":
                        click.echo(
                            f"  → {recipient} has {len(active)} active task(s); "
                            "MailWatcher will inject mid-stream + next turn "
                            "boundary (blocker = system waiting)."
                        )
                    elif urgency == "high":
                        click.echo(
                            f"  → {recipient} has {len(active)} active task(s); "
                            "MailWatcher will inject at next turn boundary "
                            "(high = please reply, not mid-thought)."
                        )
                    else:  # normal
                        click.echo(
                            f"  → {recipient} has {len(active)} active task(s); "
                            "FYI won't interrupt running work — picked up by "
                            "Phase 0 once current tasks complete."
                        )
                else:
                    click.echo(
                        f"  → scheduler will auto-dispatch a 'check inbox' task "
                        f"to {recipient} on its next tick "
                        f"(typically <1s if `lyre serve` is running)."
                    )
        finally:
            await conn.close()

    asyncio.run(_run())


# ----------------------------------------------------------------------
# lyre mailbox <recipient>
# ----------------------------------------------------------------------

@cli.command("mailbox")
@click.argument("recipient", default="owner")
@click.option("--since", default=0, type=int, help="只显示 id > since 的消息")
@click.option(
    "--unread-only", is_flag=True,
    help="Only show unread mail (read_at IS NULL).",
)
def mailbox_cmd(recipient: str, since: int, unread_only: bool) -> None:
    """List messages in a mailbox."""

    async def _run() -> None:
        cfg = Config.from_env()
        conn = await init_db(cfg.db_path)
        try:
            repos = SqliteRepositories(
                conn,
                personas_dir=cfg.user_personas_dir,
                persona_overrides=cfg.persona_overrides,
            )
            await repos.mailbox.ensure_mailbox(recipient)
            if unread_only:
                msgs = await repos.mailbox.read_unread(recipient, limit=200)
            else:
                msgs = await repos.mailbox.read_messages(
                    recipient, since_id=since
                )
            for m in msgs:
                state = " " if m.read_at else "•"  # bullet = unread
                title = m.title or "(no title)"
                click.echo(
                    f"{state} [{m.id}] {m.urgency:>7} from {m.sender} → "
                    f"{m.recipient}: {title}"
                )
            if not msgs:
                kind = "unread " if unread_only else ""
                click.echo(
                    f"(no {kind}messages in {recipient}'s mailbox)"
                )
        finally:
            await conn.close()

    asyncio.run(_run())


@cli.command("audit")
@click.argument("target", required=False)
@click.option(
    "--persona", default=None, help="过滤 persona（与 --latest 配合使用）"
)
@click.option(
    "--latest",
    is_flag=True,
    help="取最近一次 wakeup（可配合 --persona 过滤）",
)
@click.option("--system/--no-system", default=True, help="是否打印 system prompt")
@click.option(
    "--full-result",
    is_flag=True,
    help="完整打印 tool_result（默认只截前 400 char）",
)
@click.option(
    "--json", "as_json", is_flag=True,
    help="Emit raw transcript JSONL (one event per line, untouched) "
         "so you can pipe to jq. Skips the pretty-printed summary.",
)
def audit_cmd(
    target: str | None,
    persona: str | None,
    latest: bool,
    system: bool,
    full_result: bool,
    as_json: bool,
) -> None:
    """Pretty-print one wakeup's transcript so you can see exactly what the LLM saw and did.

    Examples:
        lyre audit 019e36a9-e1f8-...          # specific wakeup by id (prefix ok)
        lyre audit --latest                   # most-recent wakeup, any persona
        lyre audit --latest --persona dispatcher  # most-recent dispatcher wakeup
    """
    import textwrap

    async def _run() -> None:
        cfg = Config.from_env()
        conn = await init_db(cfg.db_path)
        try:
            wakeup_id: str | None = None
            if latest:
                if persona:
                    sql = (
                        "SELECT id FROM wakeups WHERE persona_name = ? "
                        "ORDER BY started_at DESC LIMIT 1"
                    )
                    params: tuple[Any, ...] = (persona,)
                else:
                    sql = "SELECT id FROM wakeups ORDER BY started_at DESC LIMIT 1"
                    params = ()
                async with conn.execute(sql, params) as cur:
                    row = await cur.fetchone()
                if row is None:
                    click.echo("No wakeups found.", err=True)
                    sys.exit(1)
                wakeup_id = row["id"]
            elif target:
                async with conn.execute(
                    "SELECT id FROM wakeups WHERE id LIKE ? LIMIT 2",
                    (target + "%",),
                ) as cur:
                    rows = list(await cur.fetchall())
                if not rows:
                    click.echo(f"No wakeup matches '{target}'", err=True)
                    sys.exit(1)
                if len(rows) > 1:
                    click.echo(f"Ambiguous prefix '{target}' (multiple matches)", err=True)
                    sys.exit(1)
                wakeup_id = rows[0]["id"]
            else:
                click.echo("Pass a wakeup id, or --latest [--persona X].", err=True)
                sys.exit(2)

            async with conn.execute(
                "SELECT id, persona_name, task_id, started_at, ended_at, "
                "end_status, provider, model, token_input, token_output, "
                "tool_call_count, transcript_uri FROM wakeups WHERE id = ?",
                (wakeup_id,),
            ) as cur:
                w = await cur.fetchone()
            if w is None:
                click.echo(f"Wakeup not found: {wakeup_id}", err=True)
                sys.exit(1)

            uri = w["transcript_uri"]
            if not uri:
                click.echo("(transcript_uri empty)", err=True)
                return
            from pathlib import Path as _P

            path = uri.removeprefix("file://")

            # --json: dump raw JSONL untouched. The wakeup metadata row
            # comes first so a consumer with `jq` has both the wakeup
            # context and the transcript events in one stream.
            if as_json:
                # aiosqlite.Row supports dict() conversion via keys() — use
                # an explicit comprehension over its keys() since the row
                # isn't a real dict.
                meta = {"type": "_meta"}
                for k in w.keys():  # noqa: SIM118 — aiosqlite.Row API
                    meta[k] = w[k]
                click.echo(json.dumps(meta, default=str))
                content = await asyncio.to_thread(
                    _P(path).read_text, encoding="utf-8"
                )
                for line in content.splitlines():
                    if line.strip():
                        click.echo(line)
                return

            click.echo("=" * 72)
            click.echo(f"WAKEUP   {w['id']}")
            click.echo(f"persona  {w['persona_name']}    task {w['task_id']}")
            click.echo(f"model    {w['provider']} / {w['model']}")
            click.echo(
                f"tokens   in={w['token_input']}  out={w['token_output']}    "
                f"tool_calls={w['tool_call_count']}    end={w['end_status']}"
            )
            click.echo(f"time     {w['started_at']}  →  {w['ended_at']}")
            click.echo(f"file     {w['transcript_uri']}")
            click.echo("=" * 72)

            text_chars = 0
            tool_calls = 0
            text_buf: list[str] = []
            content = await asyncio.to_thread(
                _P(path).read_text, encoding="utf-8"
            )
            for line in content.splitlines():
                if not line.strip():
                    continue
                # Append-only transcripts are individually fsync'd, but a wakeup
                # SIGKILLed mid-write can leave a partial trailing JSONL line.
                # Skip it rather than aborting the whole audit (matches `tail`).
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = evt.get("type")
                if t == "system":
                    if system:
                        click.echo("\n--- SYSTEM PROMPT ---")
                        click.echo(evt["system_prompt"])
                        click.echo(
                            f"\ntools advertised ({len(evt['tool_names'])}): "
                            + ", ".join(evt["tool_names"])
                        )
                        click.echo(f"persona allowlist: {evt['allowed_tools']}")
                        click.echo("--- END SYSTEM ---\n")
                elif t == "note":
                    click.echo(f"[NOTE] {evt['text']}")
                elif t == "content_delta":
                    text_buf.append(evt["text"])
                    text_chars += len(evt["text"])
                elif t == "tool_use":
                    if text_buf:
                        click.echo("[TEXT] " + "".join(text_buf).strip())
                        text_buf = []
                    tool_calls += 1
                    inp_preview = json.dumps(evt["input"], ensure_ascii=False)
                    if len(inp_preview) > 200:
                        inp_preview = inp_preview[:200] + "…"
                    click.echo(
                        f"[TOOL_USE] {evt['name']}  id={evt['id'][:18]}  "
                        f"input={inp_preview}"
                    )
                elif t == "tool_result":
                    res = evt.get("result")
                    if not isinstance(res, str):
                        res = json.dumps(res, ensure_ascii=False)
                    if not full_result and len(res) > 400:
                        res = res[:400] + f"… (+{len(res) - 400} chars)"
                    marker = "ERR" if evt.get("is_error") else "OK "
                    click.echo(
                        f"[TOOL_RES {marker}] id={evt['id'][:18]}\n"
                        + textwrap.indent(res, "    ")
                    )
                elif t == "turn_end":
                    if text_buf:
                        click.echo("[TEXT] " + "".join(text_buf).strip())
                        text_buf = []
                    silent = evt["text_len"] == 0 and evt["tool_count"] == 0
                    marker = "  SILENT TURN ← no text, no tool" if silent else ""
                    click.echo(
                        f"[TURN_END] #{evt['turn']}  stop={evt['stop_reason']}  "
                        f"text={evt['text_len']}  tools={evt['tool_count']}{marker}"
                    )
            if text_buf:
                click.echo("[TEXT] " + "".join(text_buf).strip())
            click.echo("=" * 72)
            click.echo(
                f"SUMMARY  text_chars={text_chars}  tool_calls={tool_calls}"
            )
            if text_chars == 0 and tool_calls == 0:
                click.echo(
                    "⚠  Model produced no text and no tool calls — likely "
                    "prompt-compliance or model-quality issue."
                )
            elif text_chars == 0:
                click.echo(
                    "⚠  Model produced no text — called tools but never spoke "
                    "(no mailbox_send / no user-facing reply)."
                )
        finally:
            await conn.close()

    asyncio.run(_run())


@cli.command("tail")
@click.option(
    "--persona", default=None, help="只跟 persona 名匹配的 wakeup"
)
@click.option(
    "--active-only/--include-completed",
    default=True,
    help="--active-only：只在有正在跑的 wakeup 时才接；否则后退到最近一次结束的（默认 --active-only）",
)
@click.option(
    "--poll", default=0.5, type=float, help="文件轮询间隔（秒）"
)
@click.option(
    "--system/--no-system",
    default=False,
    help="也打 system prompt（默认不打，跑起来太长）",
)
@click.option(
    "--follow/--no-follow",
    default=True,
    help="end_status 写入后是否继续监听后续 wakeup（默认 follow）",
)
@click.option(
    "--json", "as_json", is_flag=True,
    help="Emit raw transcript JSONL (every event as one JSON line) instead "
         "of the pretty-printed form. Pipe to jq for filtering.",
)
def tail_cmd(
    persona: str | None,
    active_only: bool,
    poll: float,
    system: bool,
    follow: bool,
    as_json: bool,
) -> None:
    """Live monitor: follow agent transcript events as they're written.

    类似 `tail -F` 但是是结构化输出。看到 tool_use/tool_result/turn_end 都会实时
    打出来，每个 turn 末尾会标 SILENT TURN（如果模型没说话也没派活）。
    Ctrl-C 退出。
    """
    import textwrap
    import time as _time

    from .runtime.transcript import transcript_path
    from .transcript_tail import TranscriptTailer

    async def _find_target(
        conn: aiosqlite.Connection,
    ) -> tuple[str, bool] | None:
        """Return (wakeup_id, is_active) or None. The transcript path is
        derived from the id — wakeups.transcript_uri is only written at
        end-of-wakeup, so for the active wakeups this command exists to
        follow, the column is still NULL."""
        clauses = []
        params: list[Any] = []
        if active_only:
            clauses.append("ended_at IS NULL")
        if persona:
            clauses.append("persona_name = ?")
            params.append(persona)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        sql = (
            f"SELECT id, transcript_uri, ended_at FROM wakeups {where} "
            "ORDER BY started_at DESC LIMIT 1"
        )
        async with conn.execute(sql, tuple(params)) as cur:
            row = await cur.fetchone()
        if row is None:
            if active_only:
                # fall back: report no-active
                clauses2 = ["persona_name = ?"] if persona else []
                params2 = [persona] if persona else []
                where2 = "WHERE " + " AND ".join(clauses2) if clauses2 else ""
                sql2 = (
                    f"SELECT id, transcript_uri, ended_at FROM wakeups "
                    f"{where2} ORDER BY started_at DESC LIMIT 1"
                )
                async with conn.execute(sql2, tuple(params2)) as cur:
                    row = await cur.fetchone()
                if row is None:
                    return None
            else:
                return None
        is_active = row["ended_at"] is None
        return row["id"], is_active

    def _render(evt: dict[str, Any]) -> str | None:
        t = evt.get("type")
        ts = _time.strftime("%H:%M:%S", _time.localtime(evt.get("ts", 0) / 1000))
        if t == "system":
            if not system:
                return f"[{ts}] [SYSTEM] tools={evt['tool_names']}"
            return (
                f"[{ts}] [SYSTEM]\n"
                + textwrap.indent(evt["system_prompt"], "  | ")
                + f"\n  tools={evt['tool_names']}"
            )
        if t == "note":
            return f"[{ts}] [NOTE] {evt['text']}"
        if t == "content_delta":
            return f"[{ts}] [TEXT] {evt['text'].rstrip()}"
        if t == "tool_use":
            inp = json.dumps(evt["input"], ensure_ascii=False)
            if len(inp) > 200:
                inp = inp[:200] + "…"
            return f"[{ts}] [TOOL_USE] {evt['name']}  input={inp}"
        if t == "tool_result":
            res = evt.get("result")
            if not isinstance(res, str):
                res = json.dumps(res, ensure_ascii=False)
            if len(res) > 400:
                res = res[:400] + f"… (+{len(res) - 400} chars)"
            tag = "ERR" if evt.get("is_error") else "OK "
            return f"[{ts}] [TOOL_RES {tag}]\n" + textwrap.indent(res, "  | ")
        if t == "turn_end":
            silent = evt["text_len"] == 0 and evt["tool_count"] == 0
            warn = "  ⚠ SILENT TURN" if silent else ""
            return (
                f"[{ts}] [TURN_END #{evt['turn']}] stop={evt['stop_reason']}  "
                f"text={evt['text_len']}  tools={evt['tool_count']}{warn}"
            )
        return None

    async def _run() -> None:
        cfg = Config.from_env()
        conn = await init_db(cfg.db_path)
        try:
            current_id: str | None = None
            tailer: TranscriptTailer | None = None
            click.echo(
                "lyre tail — Ctrl-C to stop. "
                f"persona={persona or 'any'} active_only={active_only}"
            )
            while True:
                target = await _find_target(conn)
                if target is None:
                    click.echo("(no matching wakeup yet, waiting…)", err=True)
                    await asyncio.sleep(poll * 4)
                    continue
                wid, is_active = target
                if wid != current_id:
                    if current_id is not None:
                        click.echo(
                            f"\n--- switched to new wakeup {wid[:18]} "
                            f"(active={is_active}) ---\n"
                        )
                    else:
                        click.echo(
                            f"--- following wakeup {wid[:18]} "
                            f"(active={is_active}) ---"
                        )
                    current_id = wid
                    tailer = TranscriptTailer(
                        transcript_path(cfg.object_store_path, wid)
                    )
                assert tailer is not None
                for evt in await asyncio.to_thread(tailer.poll):
                    if as_json:
                        # Verbatim-equivalent pass-through (re-serialized).
                        # Operator can jq it / save / replay.
                        click.echo(json.dumps(evt, ensure_ascii=False))
                        continue
                    rendered = _render(evt)
                    if rendered:
                        click.echo(rendered)
                if not is_active and not follow:
                    click.echo("--- wakeup ended, --no-follow specified, exiting ---")
                    break
                await asyncio.sleep(poll)
        finally:
            await conn.close()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        click.echo("\n(stopped)", err=True)


# ----------------------------------------------------------------------
# lyre agent — create / list / archive
# ----------------------------------------------------------------------


@cli.group("agent")
def agent_group() -> None:
    """Manage agent instances (each is one running entity of a persona)."""


@agent_group.command("create")
@click.argument("persona")
@click.option("--name", default=None, help="Optional agent id; auto-generated as <persona>-<n> if omitted.")
@click.option("--model", default=None, help="Optional model_id override (see `lyre model list`).")
@click.option("--description", default=None, help="Optional purpose note.")
def agent_create_cmd(
    persona: str, name: str | None, model: str | None, description: str | None
) -> None:
    """Register a new agent instance of an existing persona."""

    async def _run() -> None:
        cfg = Config.from_env()
        conn = await init_db(cfg.db_path)
        try:
            repos = SqliteRepositories(
                conn,
                personas_dir=cfg.user_personas_dir,
                persona_overrides=cfg.persona_overrides,
            )
            p = await repos.personas.get(persona)
            if p is None or p.status != "approved":
                click.echo(
                    f"persona {persona!r} not found or not approved", err=True
                )
                sys.exit(1)

            if name is None:
                from .runtime.tools.introspect import _next_auto_name

                class _Stub:
                    repos = None
                _stub = _Stub()
                _stub.repos = repos  # type: ignore[assignment]
                agent_id = await _next_auto_name(_stub, persona)  # type: ignore[arg-type]
            else:
                agent_id = name

            metadata: dict[str, Any] = {}
            if description:
                metadata["description"] = description
            if model:
                metadata["model_id"] = model

            await repos.agents.create(
                agent_id=agent_id,
                persona_name=persona,
                parent_agent_id="owner",
                metadata=metadata or None,
            )
            click.echo(f"created agent {agent_id} (persona={persona})")
        finally:
            await conn.close()

    asyncio.run(_run())


@agent_group.command("list")
@click.option("--all", "include_archived", is_flag=True, help="Include archived agents.")
def agent_list_cmd(include_archived: bool) -> None:
    """List agents."""

    async def _run() -> None:
        cfg = Config.from_env()
        conn = await init_db(cfg.db_path)
        try:
            repos = SqliteRepositories(
                conn,
                personas_dir=cfg.user_personas_dir,
                persona_overrides=cfg.persona_overrides,
            )
            agents = await repos.agents.list_all(include_archived=include_archived)
            if not agents:
                click.echo("(no agents)")
                return
            for a in agents:
                model = a.model_id or "(persona default)"
                desc = f"  — {a.description}" if a.description else ""
                click.echo(
                    f"{a.id:30s} persona={a.persona_name:20s} "
                    f"status={a.status:8s} model={model}{desc}"
                )
        finally:
            await conn.close()

    asyncio.run(_run())


@agent_group.command("archive")
@click.argument("agent_id")
def agent_archive_cmd(agent_id: str) -> None:
    """Soft-archive an agent. In-flight tasks finish.

    Unlike the agent-facing ``archive_agent`` tool (which refuses
    parentless / seeded agents to prevent self-foot-shooting from a
    persona), the CLI lets the owner archive any agent. If you
    accidentally zero out every live agent of a singleton / seeded
    persona, restart ``lyre serve`` — ``seed_default_agents`` will
    bring back the persona's current ``display_name`` agent (and
    unarchive it if a matching archived row exists).
    """

    async def _run() -> None:
        cfg = Config.from_env()
        conn = await init_db(cfg.db_path)
        try:
            repos = SqliteRepositories(
                conn,
                personas_dir=cfg.user_personas_dir,
                persona_overrides=cfg.persona_overrides,
            )
            ok = await repos.agents.archive(agent_id, reason="manual")
            if not ok:
                click.echo(f"no active agent {agent_id!r} to archive", err=True)
                sys.exit(1)
            click.echo(f"archived {agent_id}")
        finally:
            await conn.close()

    asyncio.run(_run())


@agent_group.command("unarchive")
@click.argument("agent_id")
def agent_unarchive_cmd(agent_id: str) -> None:
    """Bring an archived agent back to ``idle``.

    Recovery path when a typo'd ``display_name`` edit (or a previous
    overzealous auto-archive) silently retired a working agent. Mail
    history attached to the id is preserved either way; this just
    flips ``status='archived' → 'idle'`` and clears ``archived_at``.

    Idempotent: re-running on an already-active agent is a no-op
    with an explanatory message, not an error.
    """

    async def _run() -> None:
        cfg = Config.from_env()
        conn = await init_db(cfg.db_path)
        try:
            repos = SqliteRepositories(
                conn,
                personas_dir=cfg.user_personas_dir,
                persona_overrides=cfg.persona_overrides,
            )
            target = await repos.agents.get(agent_id)
            if target is None:
                click.echo(f"no such agent {agent_id!r}", err=True)
                sys.exit(1)
            if target.status != "archived":
                click.echo(
                    f"agent {agent_id!r} is already {target.status} — nothing to do"
                )
                return
            ok = await repos.agents.unarchive(agent_id)
            if not ok:
                click.echo(
                    f"unarchive of {agent_id!r} did not change anything; "
                    f"check status with `lyre agent list`",
                    err=True,
                )
                sys.exit(1)
            click.echo(f"unarchived {agent_id} (status: idle)")
        finally:
            await conn.close()

    asyncio.run(_run())


# ----------------------------------------------------------------------
# lyre model list
# ----------------------------------------------------------------------


@cli.group("model")
def model_group() -> None:
    """Inspect the model registry."""


@model_group.command("list")
def model_list_cmd() -> None:
    """Show every model + auth/health status."""

    from .runtime.model_registry import load_registry_for_config

    cfg = Config.from_env()
    registry = load_registry_for_config(cfg)
    click.echo(
        f"{'ID':40s} {'TIER':10s} {'PROVIDER':12s} {'AUTH':6s} {'CAPS'}"
    )
    for e in registry.entries:
        # Auth column reflects what's actually configured for the entry:
        #   ✓     API-key env var set (or in stacked mode, key + headers)
        #   hdr   header-only mode, headers configured
        #   ✗     misconfigured (neither auth path resolves)
        if e.endpoint.auth_env:
            auth = "✓" if os.environ.get(e.endpoint.auth_env) else "✗"
        elif e.endpoint.headers:
            auth = "hdr"
        else:
            auth = "✗"
        click.echo(
            f"{e.id:40s} {e.tier:10s} {e.provider:12s} {auth:6s} "
            f"{','.join(e.capabilities)}"
        )


# ----------------------------------------------------------------------
# `lyre wakeups list` / `lyre tasks list` — debug entry points
# ----------------------------------------------------------------------

_SINCE_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def _parse_since_to_iso(since: str | None) -> str | None:
    """Parse `1h` / `30m` / `2d` / `1w` into an ISO-8601 UTC cutoff.
    Returns None if `since` is None. Raises click.BadParameter on bad input.
    """
    if not since:
        return None
    import re
    from datetime import datetime, timedelta

    m = re.match(r"^(\d+)([smhdw])$", since.strip())
    if not m:
        raise click.BadParameter(
            f"--since {since!r}: expected like '1h' / '30m' / '2d' / '1w'"
        )
    n = int(m.group(1))
    unit = m.group(2)
    cutoff = datetime.now(UTC) - timedelta(seconds=n * _SINCE_UNITS[unit])
    return cutoff.strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


def _fmt_tokens_short(n: int | None) -> str:
    """12345 → 12.3K, 1234567 → 1.2M (CLI table-friendly)."""
    if not n:
        return "0"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _fmt_ts_short(ts: str | None) -> str:
    """ISO ts → 'YYYY-MM-DD HH:MM:SS' (drop sub-second + Z for CLI table)."""
    if not ts:
        return "-"
    return ts[:19].replace("T", " ")


@cli.group("wakeups")
def wakeups_group() -> None:
    """Inspect wakeups."""


@wakeups_group.command("list")
@click.option("--limit", default=20, type=int, show_default=True)
@click.option("--persona", default=None, help="Filter by persona_name.")
@click.option(
    "--status", default=None,
    help="Filter by end_status (completed / failed / silent_close / "
         "needs_continuation / etc.).",
)
@click.option(
    "--since", default=None,
    help="Recency window: '1h' / '30m' / '2d' / '1w'. Omit for "
         "no time filter (just the most recent N by --limit).",
)
@click.option(
    "--has-compaction", is_flag=True,
    help="Only wakeups that auto-compacted at least once.",
)
@click.option(
    "--summary-degraded", "summary_degraded", is_flag=True,
    help="Only wakeups where a compaction's work-summary LLM call failed "
         "and fell back to the raw trace (lossy compaction).",
)
@click.option(
    "--json", "as_json", is_flag=True,
    help="Emit JSON Lines (one object per wakeup) for piping to jq.",
)
def wakeups_list_cmd(
    limit: int,
    persona: str | None,
    status: str | None,
    since: str | None,
    has_compaction: bool,
    summary_degraded: bool,
    as_json: bool,
) -> None:
    """List recent wakeups with status / tokens / context-usage / compactions.

    Examples:
        lyre wakeups list                                  # last 20
        lyre wakeups list --since 1h --status silent_close
        lyre wakeups list --persona dispatcher --json | jq '.id'
    """
    from .runtime.model_registry import load_registry_for_config

    cutoff = _parse_since_to_iso(since)

    async def _run() -> None:
        cfg = Config.from_env()
        # context_window per model — for ctx_peak_pct column.
        try:
            registry = load_registry_for_config(cfg)
            ctx_windows = {
                e.id: e.context_window for e in registry.entries if e.context_window
            }
        except Exception:  # noqa: BLE001 — debug command must be tolerant
            ctx_windows = {}

        clauses: list[str] = []
        params: list[Any] = []
        if persona:
            clauses.append("persona_name = ?")
            params.append(persona)
        if status:
            clauses.append("end_status = ?")
            params.append(status)
        if cutoff:
            clauses.append("started_at >= ?")
            params.append(cutoff)
        if has_compaction:
            clauses.append("compaction_count > 0")
        if summary_degraded:
            clauses.append("compaction_summary_degraded > 0")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            f"SELECT id, persona_name, agent_id, task_id, started_at, "
            f"ended_at, end_status, token_input, token_output, "
            f"wall_clock_ms, tool_call_count, model, "
            f"context_peak_tokens, compaction_count, "
            f"compaction_summary_degraded "
            f"FROM wakeups {where} "
            f"ORDER BY started_at DESC LIMIT ?"
        )
        params.append(limit)
        conn = await init_db(cfg.db_path)
        try:
            async with conn.execute(sql, tuple(params)) as cur:
                rows = await cur.fetchall()
        finally:
            await conn.close()

        if as_json:
            for r in rows:
                d = dict(r)
                model_id = d.get("model")
                window = (
                    ctx_windows.get(model_id) if isinstance(model_id, str)
                    else None
                )
                peak = d.get("context_peak_tokens") or 0
                d["context_window"] = window
                d["context_peak_pct"] = (
                    round(peak / window * 100, 1) if (peak and window) else None
                )
                click.echo(json.dumps(d, default=str))
            return

        if not rows:
            click.echo("(no wakeups match)")
            return

        click.echo(
            f"{'ID':10s} {'PERSONA':18s} {'STARTED':19s} {'WALL':>7s} "
            f"{'IN':>7s} {'OUT':>7s} {'CTX%':>5s} {'CMPCT':>5s} {'DEGR':>4s} "
            f"{'TOOLS':>5s} {'STATUS':12s} MODEL"
        )
        for r in rows:
            wall = r["wall_clock_ms"]
            wall_s = f"{wall / 1000:.1f}s" if wall else "-"
            peak = r["context_peak_tokens"] or 0
            window = ctx_windows.get(r["model"])
            ctx_pct = (
                f"{peak / window * 100:.0f}%"
                if (peak and window) else "-"
            )
            click.echo(
                f"{(r['id'] or '')[:10]:10s} "
                f"{(r['persona_name'] or '')[:18]:18s} "
                f"{_fmt_ts_short(r['started_at']):19s} "
                f"{wall_s:>7s} "
                f"{_fmt_tokens_short(r['token_input']):>7s} "
                f"{_fmt_tokens_short(r['token_output']):>7s} "
                f"{ctx_pct:>5s} "
                f"{r['compaction_count'] or 0:>5d} "
                f"{r['compaction_summary_degraded'] or 0:>4d} "
                f"{r['tool_call_count'] or 0:>5d} "
                f"{(r['end_status'] or '-')[:12]:12s} "
                f"{r['model'] or '-'}"
            )

    asyncio.run(_run())


@cli.group("tasks")
def tasks_group() -> None:
    """Inspect tasks."""


@tasks_group.command("list")
@click.option("--limit", default=20, type=int, show_default=True)
@click.option("--persona", default=None, help="Filter by persona_name.")
@click.option(
    "--agent", "agent_id", default=None,
    help="Filter by agent_id (post-A3).",
)
@click.option(
    "--status", default=None,
    help="Filter by task status "
         "(pending / in_progress / needs_input / completed / failed / cancelled).",
)
@click.option(
    "--since", default=None,
    help="Recency window: '1h' / '30m' / '2d' / '1w'.",
)
@click.option(
    "--json", "as_json", is_flag=True,
    help="Emit JSON Lines for piping to jq.",
)
def tasks_list_cmd(
    limit: int,
    persona: str | None,
    agent_id: str | None,
    status: str | None,
    since: str | None,
    as_json: bool,
) -> None:
    """List recent tasks with status + goal preview.

    Examples:
        lyre tasks list --since 1h
        lyre tasks list --status in_progress --json | jq '.id'
    """
    cutoff = _parse_since_to_iso(since)

    async def _run() -> None:
        cfg = Config.from_env()
        clauses: list[str] = []
        params: list[Any] = []
        if persona:
            clauses.append("persona_name = ?")
            params.append(persona)
        if agent_id:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if cutoff:
            clauses.append("updated_at >= ?")
            params.append(cutoff)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            f"SELECT id, persona_name, agent_id, status, updated_at, "
            f"goal, parent_task_id "
            f"FROM tasks {where} "
            f"ORDER BY updated_at DESC LIMIT ?"
        )
        params.append(limit)
        conn = await init_db(cfg.db_path)
        try:
            async with conn.execute(sql, tuple(params)) as cur:
                rows = await cur.fetchall()
        finally:
            await conn.close()

        if as_json:
            for r in rows:
                click.echo(json.dumps(dict(r), default=str))
            return

        if not rows:
            click.echo("(no tasks match)")
            return

        click.echo(
            f"{'ID':10s} {'PERSONA':18s} {'AGENT':18s} {'STATUS':14s} "
            f"{'UPDATED':19s} GOAL"
        )
        for r in rows:
            goal = (r["goal"] or "").replace("\n", " ")[:80]
            click.echo(
                f"{(r['id'] or '')[:10]:10s} "
                f"{(r['persona_name'] or '')[:18]:18s} "
                f"{(r['agent_id'] or '-')[:18]:18s} "
                f"{(r['status'] or '-')[:14]:14s} "
                f"{_fmt_ts_short(r['updated_at']):19s} "
                f"{goal}"
            )

    asyncio.run(_run())


@tasks_group.command("cancel")
@click.argument("task_id")
@click.option("--reason", default=None, help="Why you're cancelling (recorded).")
def tasks_cancel_cmd(task_id: str, reason: str | None) -> None:
    """Request cooperative cancel of a running / pending task.

    The running wakeup observes the request at its next turn boundary, finishes
    the current turn cleanly, then stops with status 'cancelled' (a
    task_terminated mail is sent to the supervisor). A not-yet-running task
    cancels on its next wakeup's first turn. This cancels the TASK, not the
    agent — the agent stays alive for other work.

    Examples:
        lyre tasks cancel task_ab12 --reason "wrong approach, will re-dispatch"
    """

    async def _run() -> None:
        cfg = Config.from_env()
        conn = await init_db(cfg.db_path)
        try:
            repos = SqliteRepositories(
                conn,
                personas_dir=cfg.user_personas_dir,
                persona_overrides=cfg.persona_overrides,
            )
            ok = await repos.tasks.request_cancel(task_id, reason)
        finally:
            await conn.close()
        if ok:
            click.echo(
                f"Cancel requested for {task_id}. It will stop at the next "
                f"turn boundary."
            )
        else:
            click.echo(
                f"Task {task_id} not found or already terminal — nothing to "
                f"cancel.",
                err=True,
            )
            sys.exit(1)

    asyncio.run(_run())


@cli.group("mail")
def mail_group() -> None:
    """Manage scheduled (future / recurring) mail."""


@mail_group.command("list-scheduled")
@click.option("--recipient", default=None, help="Filter by recipient agent.")
@click.option("--sender", default=None, help="Filter by sender.")
@click.option(
    "--status",
    type=click.Choice(["pending", "completed", "cancelled", "bounced", "all"]),
    default="pending",
)
@click.option("--limit", default=50, type=int)
def mail_list_scheduled_cmd(
    recipient: str | None, sender: str | None, status: str, limit: int
) -> None:
    """List scheduled mail entries."""

    async def _run() -> None:
        cfg = Config.from_env()
        conn = await init_db(cfg.db_path)
        try:
            repos = SqliteRepositories(
                conn,
                personas_dir=cfg.user_personas_dir,
                persona_overrides=cfg.persona_overrides,
            )
            rows = await repos.scheduled_mail.list_filtered(
                recipient=recipient, sender=sender, status=status, limit=limit
            )
            if not rows:
                click.echo("(no scheduled mail matching filters)")
                return
            click.echo(
                f"{'ID':>5} {'STATUS':10s} {'NEXT FIRE':22s} {'RECIPIENT':20s} "
                f"{'RECUR':14s} OCC  PREVIEW"
            )
            for r in rows:
                recur = ""
                if r.recur_kind == "interval":
                    recur = f"every {r.recur_value}"
                elif r.recur_kind == "cron":
                    recur = f"cron {r.recur_value}"[:14]
                preview = (r.body or "").replace("\n", " ")[:50]
                click.echo(
                    f"{r.id!s:>5} {r.status:10s} "
                    f"{str(r.scheduled_for)[:22]:22s} "
                    f"{r.recipient:20s} {recur:14s} "
                    f"{r.occurrence_count!s:3s}  {preview}"
                )
        finally:
            await conn.close()

    asyncio.run(_run())


@mail_group.command("cancel")
@click.argument("mail_id", type=int)
@click.option("--reason", default=None)
def mail_cancel_cmd(mail_id: int, reason: str | None) -> None:
    """Cancel a pending scheduled mail (stops recurrence for recurring ones)."""

    async def _run() -> None:
        cfg = Config.from_env()
        conn = await init_db(cfg.db_path)
        try:
            repos = SqliteRepositories(
                conn,
                personas_dir=cfg.user_personas_dir,
                persona_overrides=cfg.persona_overrides,
            )
            existing = await repos.scheduled_mail.get(mail_id)
            if existing is None:
                click.echo(f"scheduled_mail id={mail_id} not found", err=True)
                sys.exit(1)
            if existing.status != "pending":
                click.echo(
                    f"scheduled_mail id={mail_id} is already {existing.status}",
                    err=True,
                )
                sys.exit(1)
            ok = await repos.scheduled_mail.mark_cancelled(
                mail_id=mail_id, cancelled_by="owner", reason=reason
            )
            if not ok:
                click.echo("(cancel raced; nothing changed)", err=True)
                sys.exit(1)
            click.echo(f"cancelled scheduled_mail {mail_id}")
        finally:
            await conn.close()

    asyncio.run(_run())


if __name__ == "__main__":
    cli()
