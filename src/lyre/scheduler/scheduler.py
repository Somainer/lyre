"""Scheduler.

Polls pending tasks and runs them on the multi-turn AgentLoop. The Scheduler
owns the long-lived ModelRegistry / Router / HealthTracker / AdapterFactory —
each wakeup pulls a ranked candidate list from the router and hands it to a
fresh AgentLoop instance.

Per Q9 (2026-05-17):
- Persona declares `model_preference` (tier + requires + prefer);
- Router resolves that against `model_registry.yaml` and the in-memory
  HealthTracker;
- AgentLoop walks the ranked list, falling back per-turn on pre-stream errors.

Subprocess isolation (per AGENT_RUNTIME §3.5 + 铁律 2): when
`spawn_subprocess=True`, each task runs in a fresh Python subprocess via
`lyre run-task <id>`. Subprocess opens its own DB connection (SQLite WAL
handles concurrent processes), executes the same `_run_task_inline`
pipeline, and writes results back to the DB. Parent only observes the exit
code; abnormal exit leaves the lease held and the standard recovery path
takes over.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable
from typing import Any

import structlog

from ..adapter.llm_adapter import LLMAdapter
from ..config import Config
from ..persistence.models import Persona, ScheduledMail, TaskSpec
from ..persistence.repositories import Repositories
from ..runtime.adapter_factory import AdapterFactory, model_name_for_provider
from ..runtime.agent_loop import AgentLoop
from ..runtime.context import assemble_initial_user_message, assemble_system_prompt
from ..runtime.health_tracker import HealthTracker
from ..runtime.kill_switch import KillSwitch, is_simulated_kill_in_flight
from ..runtime.mail_watcher import MailWatcher
from ..runtime.model_registry import (
    ModelEntry,
    ModelRegistry,
    load_registry_for_config,
)
from ..runtime.model_router import ModelPreference, ModelRouter
from ..runtime.tools import ToolContext, ToolRegistry
from ..runtime.tools.builtin import build_default_registry
from ..runtime.transcript import TranscriptWriter
from ..runtime.wakeup_summary import summarize_and_append
from ..runtime.worktree import WorktreeHandle, WorktreeManager

log = structlog.get_logger()


# Wakeup-level statuses produced by AgentLoopResult are richer than
# the task-level TaskStatus enum that tasks.status is CHECK-constrained
# to. Translate them at the boundary:
#
#   completed          → completed  (passthrough)
#   failed             → failed     (passthrough)
#   cancelled          → cancelled  (passthrough)
#   silent_close       → completed
#       The wakeup ran but composed no user-facing reply. The task
#       itself terminated normally — the silent-close detail lives
#       on wakeups.end_status, not on the task.
#   needs_continuation → failed
#       The loop bailed because it hit max_turns or max_tokens —
#       usually the model is stuck repeating a malformed tool call
#       (e.g. truncated args). Marking the task failed is honest
#       and prevents the scheduler from blindly re-enqueueing the
#       same wedged state. Owner / dispatcher can re-dispatch with
#       a fresh plan if recovery is desired.
#
# Without this mapping, writing the raw wakeup status to tasks.status
# trips the DB CHECK constraint and the scheduler's post-loop write
# crashes mid-tick, leaving the lease orphaned.
_WAKEUP_TO_TASK_STATUS: dict[str, str] = {
    "silent_close": "completed",
    "needs_continuation": "failed",
}


def _wakeup_status_to_task_status(wakeup_status: str) -> str:
    return _WAKEUP_TO_TASK_STATUS.get(wakeup_status, wakeup_status)


class Scheduler:
    def __init__(
        self,
        repos: Repositories,
        config: Config,
        poll_interval_s: float = 1.0,
        tool_registry: ToolRegistry | None = None,
        registry: ModelRegistry | None = None,
        health: HealthTracker | None = None,
        adapter_factory: AdapterFactory | None = None,
        worktree_manager: WorktreeManager | None = None,
        kill_switch: KillSwitch | None = None,
        spawn_subprocess: bool = False,
        subprocess_argv: list[str] | None = None,
        auto_wake_on_mail: bool = True,
        # Test hook: when set, every wakeup uses this adapter for every
        # candidate, ignoring AdapterFactory. Callable receives ModelEntry.
        adapter_for_test: Callable[[ModelEntry], LLMAdapter] | None = None,
    ):
        self.repos = repos
        self.config = config
        self.poll_interval_s = poll_interval_s
        self.tool_registry = tool_registry or build_default_registry()
        # The default MUST honor user [[models]] from config.toml, not
        # only the shipped registry — otherwise a user with their own
        # endpoints sees the router pick shipped Anthropic / DeepSeek
        # entries they never asked for. load_registry_for_config does
        # the merge (and now-replace: user entries replace shipped
        # when any are present). Tests + the subprocess runner can
        # still pass `registry=` explicitly to bypass.
        self.registry = (
            registry if registry is not None
            else load_registry_for_config(self.config)
        )
        self.health = health or HealthTracker()
        self.adapter_factory = adapter_factory or AdapterFactory()
        self.router = ModelRouter(
            registry=self.registry,
            health=self.health,
            override_id=self.config.model_override,
        )
        self.worktree_manager = worktree_manager or WorktreeManager(
            root=self.config.object_store_path / "worktrees"
        )
        self.kill_switch = kill_switch or KillSwitch()
        self.spawn_subprocess = spawn_subprocess
        self.subprocess_argv = subprocess_argv  # if None: ["python", "-m", "lyre.main", "run-task"]
        self.auto_wake_on_mail = auto_wake_on_mail
        self.adapter_for_test = adapter_for_test
        self._stop_event = asyncio.Event()
        # Concurrency state (subprocess mode only). Maps task_id →
        # (Process, reaper_task). The reaper is a background asyncio
        # task that awaits proc.communicate(), logs the result, and
        # pops itself off this dict. Inline mode ignores this — it
        # stays strictly serial.
        self._active_subprocesses: dict[
            str, tuple[asyncio.subprocess.Process, asyncio.Task[None]]
        ] = {}
        # Cap from config; only consulted in subprocess mode. inline
        # is single-threaded by design so the cap doesn't apply.
        self._max_concurrent = max(1, self.config.max_concurrent_tasks)

    def request_stop(self) -> None:
        self._stop_event.set()

    async def _drain_subprocesses(self, timeout_s: float = 30.0) -> None:
        """Wait for all in-flight subprocesses to finish.

        Called from ``run`` after the main tick loop exits so a
        graceful shutdown lets running tasks complete rather than
        SIGKILL'ing them mid-wakeup. If a subprocess wedges past
        ``timeout_s`` we cancel its reaper, which terminates the
        process and its lease will be recovered after expiry on the
        next process' boot.
        """
        if not self._active_subprocesses:
            return
        log.info(
            "scheduler_draining_subprocesses",
            count=len(self._active_subprocesses),
            timeout_s=timeout_s,
        )
        reapers = [t for _, t in self._active_subprocesses.values()]
        try:
            await asyncio.wait_for(
                asyncio.gather(*reapers, return_exceptions=True),
                timeout=timeout_s,
            )
        except TimeoutError:
            log.warning(
                "scheduler_drain_timeout",
                stuck=list(self._active_subprocesses.keys()),
            )
            # Cancel hung reapers — each handles CancelledError by
            # terminating + killing its child.
            for _, t in self._active_subprocesses.values():
                if not t.done():
                    t.cancel()
            # Best-effort: wait once more for the cancellations to
            # propagate. Anything still stuck after this falls to the
            # OS to reap when the parent exits.
            await asyncio.gather(*reapers, return_exceptions=True)

    async def run(self) -> None:
        log.info(
            "scheduler_started",
            poll_interval_s=self.poll_interval_s,
            registry_entries=len(self.registry.entries),
            override=self.config.model_override,
            max_concurrent_tasks=self._max_concurrent,
            spawn_subprocess=self.spawn_subprocess,
        )
        while not self._stop_event.is_set():
            try:
                await self._tick()
            except Exception as e:  # noqa: BLE001
                log.exception("scheduler_tick_error", error=str(e))
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.poll_interval_s)
            except TimeoutError:
                pass
        # Let in-flight subprocess tasks finish (or time out) before
        # returning. Without this, `lyre serve` shutdown would orphan
        # children that the OS later reaps with no logging.
        if self.spawn_subprocess:
            await self._drain_subprocesses()
        log.info("scheduler_stopped")

    async def _tick(self) -> None:
        # Phase -1 (future mail): deliver any scheduled_mail whose
        # scheduled_for has arrived. Runs BEFORE Phase 0 so a just-delivered
        # mail can immediately trigger an auto-dispatch in the same tick —
        # otherwise agents would wait a full poll interval for follow-up.
        await self._deliver_scheduled_mail()

        # Phase 0 (mail-triggered wakeup): if any agent has unread mail and
        # has no in-flight task, create an auto-"check inbox" task so the
        # message gets read.
        if self.auto_wake_on_mail:
            await self._auto_dispatch_for_unread_mail()

        # Phase 2 (chaos recovery): pick up tasks whose lease has expired
        # (process died, SIGKILL, etc.) BEFORE looking for new pending work.
        # Skip any task already covered by an in-flight subprocess —
        # avoids a transient double-spawn during the window between
        # subprocess crash and DB lease expiry (the crashed proc is
        # gone but the new reaper hasn't fired yet because we just
        # started the recovery one).
        slots = self._available_slots()
        if slots > 0:
            expired = await self.repos.tasks.find_expired_leases(limit=slots)
            expired = [t for t in expired if t.id not in self._active_subprocesses]
            for t in expired[:slots]:
                log.info("scheduler_recovering_expired_lease", task_id=t.id)
                await self._run_task(t.id)
                slots = self._available_slots()
                if slots <= 0:
                    break

        # Phase 3: new pending work — up to the remaining slot budget.
        slots = self._available_slots()
        if slots <= 0:
            return
        # Over-fetch: ``has_active_for_agent`` may force us to skip some
        # candidates (an agent already running blocks any second
        # wakeup of itself). Fetching slots*4 gives the scan room to
        # find tasks for OTHER agents without exhausting the pool too
        # eagerly. The hard cap stays at ``slots`` actual dispatches.
        candidate_limit = max(slots * 4, slots)
        pending = await self.repos.tasks.find_pending(limit=candidate_limit)
        pending = [t for t in pending if t.id not in self._active_subprocesses]

        # Agents are sequential actors: a second pending task for the
        # same agent must wait until that agent's current wakeup ends.
        # Without this guard two subprocesses for the same agent_id
        # race on shared filesystem state (scratchpad, notes,
        # auto-summary log) — last writer wins, lost updates,
        # interleaved log entries. Parallelism within a persona =
        # multiple agent INSTANCES, not multiple wakeups of one
        # agent. See docs/design/AGENT_RUNTIME.md.
        dispatched = 0
        # Track agents we've already claimed work for in this tick so
        # we don't dispatch two pending tasks of the same agent in
        # the same tick (the DB has_active check wouldn't notice yet).
        claimed_in_this_tick: set[str] = set()
        for t in pending:
            if dispatched >= slots:
                break
            agent_id = t.agent_id
            if agent_id is not None:
                if agent_id in claimed_in_this_tick:
                    continue
                if await self.repos.wakeups.has_active_for_agent(agent_id):
                    log.debug(
                        "scheduler_skip_task_agent_busy",
                        task_id=t.id, agent_id=agent_id,
                    )
                    continue
                claimed_in_this_tick.add(agent_id)
            await self._run_task(t.id)
            dispatched += 1
            if self._available_slots() <= 0:
                break

    def _available_slots(self) -> int:
        """How many fresh tasks we can take on right now.

        Inline mode: serial-by-design, so 0 if any inline task is
        currently running (we naturally enforce this because
        ``_run_task_inline`` is awaited synchronously inside ``_tick``,
        but expose the count as 1 here — the gate is the caller's own
        single-threadedness). Subprocess mode: ``max_concurrent_tasks
        - len(active)``.
        """
        if not self.spawn_subprocess:
            # Inline: ``_run_task_inline`` blocks the tick; ``_tick``
            # only spawns one before returning, so returning 1 is
            # fine — the loop body itself is the gate.
            return 1
        return max(0, self._max_concurrent - len(self._active_subprocesses))

    # Phase 0 task goal/acceptance: cache-friendly split.
    # The first 2 strings are byte-identical across all wakeups (cached
    # prefix in the initial user message). The volatile mail-id/sender
    # hint goes LAST so it only invalidates the tail of the prompt.
    _AUTO_INBOX_GOAL = (
        "Check your inbox: call `mailbox_read()` to see your unread mail "
        "(listing only — titles + sizes; use `mailbox_get_message(msg_id=N)` "
        "for any full body you actually need). Handle each item per its "
        "urgency: blocker/high → respond now via `mailbox_send`; normal → "
        "acknowledge or note; FYIs you don't want to reply to are still "
        "auto-marked read by `mailbox_read`."
    )
    _AUTO_INBOX_ACCEPTANCE = (
        "Inbox is processed: replies sent for items needing a response; "
        "no-action items got `mark_read` if you skipped mailbox_read."
    )

    async def _auto_dispatch_for_unread_mail(self) -> None:
        """Scan every live (non-archived) non-owner AGENT; if they have
        unread mail of urgency ≥ normal AND no active task for THIS agent,
        dispatch a 'check inbox' task.

        Read state is per-message (`mailbox_messages.read_at`) — set by
        the agent's `mailbox_read` call. We still keep an in-mailbox
        `last_auto_triggered_msg_id` cursor as a Phase 0-local anti-loop
        knob: if the agent wakes up but never calls mailbox_read (model
        bug / silent turn), mail stays unread and we'd otherwise re-fire
        every tick.

        Urgency hierarchy:
          - blocker / high   → also surface during running tasks (MailWatcher)
          - normal           → trigger ONLY at idle (this Phase 0)
          - low              → pure archive: never auto-triggers
        """
        agents = await self.repos.agents.list_all()
        for agent in agents:
            if agent.id == "owner":
                continue  # owner has no LLM, never wakeable
            if agent.status == "archived":
                continue

            # Anti-loop cursor: pick the highest-id unread that's strictly
            # above what we already dispatched for. Avoids re-firing for a
            # mail whose dispatch's task already completed without the
            # agent advancing read state (silent turn / model bug).
            last_auto_triggered = (
                await self.repos.mailbox.get_last_auto_triggered_id(agent.id)
            )
            unread = await self.repos.mailbox.read_unread(
                agent.id, min_urgency="normal", limit=50,
            )
            unread_new = [
                m for m in unread
                if m.id is not None and m.id > last_auto_triggered
            ]
            if not unread_new:
                continue
            # Among the not-yet-dispatched-for unread, pick the highest-
            # urgency one (read_unread already sorted urgency-desc).
            top = unread_new[0]

            # Skip if this agent already has an in-flight task.
            active = await self.repos.tasks.find_active_for_persona(
                agent.persona_name
            )
            if any(t.agent_id == agent.id for t in active):
                continue

            # Volatile hint kept at the TAIL of the goal so the cached
            # prefix (the boilerplate above) hits across wakeups.
            volatile_hint = (
                f"\n\nHint: scheduler woke you because mail id={top.id} "
                f"from {top.sender} (urgency={top.urgency}, "
                f"title={top.title!r}) is unread."
            )
            task_id = await self.repos.tasks.create(
                TaskSpec(
                    agent_id=agent.id,
                    goal=self._AUTO_INBOX_GOAL + volatile_hint,
                    acceptance=self._AUTO_INBOX_ACCEPTANCE,
                    metadata={
                        "auto_dispatched": True,
                        "triggered_by_mail_id": top.id,
                        "triggered_by_urgency": top.urgency,
                    },
                )
            )
            # top.id is non-None because read_unread only returns
            # persisted rows (the watermark column itself is NOT NULL).
            assert top.id is not None  # noqa: S101 — narrows for mypy
            await self.repos.mailbox.set_last_auto_triggered_id(
                agent.id, top.id
            )
            log.info(
                "scheduler_auto_dispatched_for_mail",
                agent_id=agent.id,
                persona=agent.persona_name,
                triggered_by_mail_id=top.id,
                new_task_id=task_id,
            )

    async def _deliver_scheduled_mail(self) -> None:
        """Phase -1: deliver any due scheduled_mail row.

        For each due row:
          - if recipient is `owner` or a live agent: insert a real
            mailbox_message and advance the schedule (mutate scheduled_for
            for recurring, mark completed for one-shot or expired recurring)
          - if recipient is archived / missing: send a bounce mail to the
            creator and mark the schedule bounced

        Idempotency note: if the process dies between the mailbox insert
        and mark_delivered, on restart we'd re-deliver. We accept this rare
        double-delivery for MVP (mailbox UNIQUE(recipient, external_id)
        catches it since we mint a deterministic external_id from
        scheduled_id + occurrence_count).
        """
        from ..persistence.models import MailboxMessage as _Msg
        from ..runtime.future_mail import compute_next_fire, iso, now_utc

        now = now_utc()
        due = await self.repos.scheduled_mail.find_ready(iso(now), limit=20)
        if not due:
            return

        for sched in due:
            recipient = sched.recipient
            # Is the recipient eligible? `owner` is always-valid; otherwise
            # the agent must exist and not be archived.
            archived = False
            missing = False
            if recipient != "owner":
                agent = await self.repos.agents.get(recipient)
                if agent is None:
                    missing = True
                elif agent.status == "archived":
                    archived = True

            if archived or missing:
                reason = (
                    "recipient agent archived"
                    if archived
                    else f"recipient agent {recipient!r} not found"
                )
                await self._bounce_scheduled_mail(sched, reason)
                continue

            # Deliver: insert a regular mailbox_message. Deterministic
            # external_id covers the rare crash-between-insert-and-mark
            # case.
            external_id = f"sched:{sched.id}:{sched.occurrence_count}"
            await self.repos.mailbox.ensure_mailbox(recipient)
            msg_id = await self.repos.mailbox.insert_message(
                _Msg(
                    recipient=recipient,
                    external_id=external_id,
                    sender=sched.sender,
                    urgency=sched.urgency,
                    title=sched.title,
                    body=sched.body,
                    task_id=sched.task_id,
                    parent_msg_id=sched.parent_msg_id,
                    metadata=sched.metadata,
                )
            )
            if msg_id < 0:
                # UNIQUE collision = already delivered before the previous
                # run crashed mid-update. Look up the existing row's id so
                # we can still set last_delivery_id correctly (FK protected).
                existing_id = await self.repos.mailbox.find_id_by_external_id(
                    recipient, external_id,
                )
                delivered_msg_id = existing_id or 0
            else:
                delivered_msg_id = msg_id

            # Compute next fire (None = no more occurrences).
            next_fire = compute_next_fire(
                sched.recur_kind,
                sched.recur_value,
                after=now,
                recur_until=sched.recur_until,
            )
            # sched was loaded from a persisted row — its id is always set
            # by the time it lands in the delivery loop.
            assert sched.id is not None  # noqa: S101 — narrows for mypy
            await self.repos.scheduled_mail.mark_delivered(
                mail_id=sched.id,
                delivered_msg_id=delivered_msg_id,
                next_scheduled_for=iso(next_fire) if next_fire else None,
                completed=(next_fire is None),
            )
            log.info(
                "scheduled_mail_delivered",
                scheduled_id=sched.id,
                recipient=recipient,
                msg_id=delivered_msg_id,
                next_fire=iso(next_fire) if next_fire else None,
                occurrence=sched.occurrence_count + 1,
            )

    async def _bounce_scheduled_mail(
        self, sched: ScheduledMail, reason: str
    ) -> None:
        """Deliver a bounce notice back to the creator and mark the
        schedule bounced. If the creator is also gone (e.g. archived in
        the meantime), we just log and drop."""
        creator = sched.created_by_agent
        if creator and (
            creator == "owner"
            or (await self.repos.agents.get(creator)) is not None
        ):
            from ..persistence.models import MailboxMessage as _Msg

            body = (
                f"[BOUNCE] Your scheduled mail to `{sched.recipient}` at "
                f"{sched.scheduled_for} could not be delivered: {reason}.\n"
                f"Original urgency: {sched.urgency}\n"
                f"Original body:\n─────\n{sched.body}"
            )
            await self.repos.mailbox.ensure_mailbox(creator)
            await self.repos.mailbox.insert_message(
                _Msg(
                    recipient=creator,
                    external_id=f"sched-bounce:{sched.id}",
                    sender="system:scheduled-mail",
                    urgency="normal",
                    body=body,
                    metadata={
                        "bounce": True,
                        "original_scheduled_id": sched.id,
                        "reason": reason,
                    },
                )
            )
        else:
            log.warning(
                "scheduled_mail_bounce_undeliverable",
                scheduled_id=sched.id,
                reason=reason,
                creator=creator,
            )
        assert sched.id is not None  # noqa: S101 — narrows for mypy
        await self.repos.scheduled_mail.mark_bounced(sched.id, reason)

    async def _run_task(self, task_id: str) -> None:
        """Dispatch a task to either inline (in-process asyncio task) or
        subprocess execution. Subprocess mode spawns a fresh Python process
        running `lyre run-task <task_id>` against the same DB — true OS
        isolation per 铁律 2; the chaos test path stays valid because the
        subprocess holds the lease and its death leaves the same recoverable
        state shape an in-process SimulatedKill would."""
        if self.spawn_subprocess:
            await self._run_task_in_subprocess(task_id)
        else:
            await self._run_task_inline(task_id)

    async def _run_task_in_subprocess(self, task_id: str) -> None:
        """Spawn `lyre run-task <task_id>` as a subprocess and return
        IMMEDIATELY without waiting for exit.

        Why non-blocking: per `config.max_concurrent_tasks` we may have
        N tasks in flight at once. A background reaper task (see
        ``_reap_subprocess``) drains stdout/stderr and logs the exit
        code asynchronously so the OS pipe buffers don't fill and
        block the child. The scheduler tick reads
        ``self._active_subprocesses`` to decide whether to pick more
        pending work this round.

        Each subprocess opens its OWN aiosqlite connection — SQLite
        WAL + 10s busy_timeout on every connection (see
        persistence/db.py) cover cross-process write contention. The
        lease's atomic UPDATE is the final dedup guard: even if the
        scheduler accidentally spawned two subprocesses for the same
        task, only one's claim_lease succeeds; the other no-ops.
        """
        argv = self.subprocess_argv or [
            sys.executable, "-m", "lyre.main", "run-task"
        ]
        full_argv = [*argv, task_id]

        # Forward only the env vars Lyre actually reads; subprocess does its
        # own Config.from_env(). PATH must propagate so shell_exec tools find
        # binaries; the existing env allowlist in runtime/shell.py kicks in
        # inside the subprocess itself.
        env = dict(os.environ)

        log.info(
            "scheduler_spawning_subprocess",
            task_id=task_id,
            argv=full_argv,
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *full_argv,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            log.exception("scheduler_subprocess_spawn_failed", error=str(exc))
            await self.repos.tasks.update_status(task_id, "failed")
            return

        reaper = asyncio.create_task(
            self._reap_subprocess(task_id, proc),
            name=f"reaper:{task_id}",
        )
        self._active_subprocesses[task_id] = (proc, reaper)

    async def _reap_subprocess(
        self, task_id: str, proc: asyncio.subprocess.Process,
    ) -> None:
        """Wait for ``proc`` to exit, drain its pipes, log the result,
        and remove from ``_active_subprocesses``. One reaper per
        spawned subprocess; fires concurrently with the scheduler
        tick so multiple subprocesses can be in flight."""
        try:
            stdout, stderr = await proc.communicate()
        except asyncio.CancelledError:
            # Shutdown path — the parent is going down. Kill the
            # child rather than leave it orphaned; its lease will
            # expire after lease_duration_s and the next process'
            # Phase 2 recovery picks it up.
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except TimeoutError:
                proc.kill()
            raise

        rc = proc.returncode if proc.returncode is not None else -1
        log.info(
            "scheduler_subprocess_exited",
            task_id=task_id,
            returncode=rc,
            stdout_bytes=len(stdout or b""),
            stderr_bytes=len(stderr or b""),
        )
        if rc != 0:
            # Subprocess crashed / SIGKILL'd. Task may be left
            # in_progress with lease held — the next
            # find_expired_leases tick will recover. Don't touch DB
            # here.
            log.warning(
                "scheduler_subprocess_abnormal_exit",
                task_id=task_id,
                returncode=rc,
                stderr_tail=(stderr or b"").decode("utf-8", "replace")[-512:],
            )
        # Pop AFTER logging so a fast follow-up tick that sees the
        # task still in `_active_subprocesses` won't double-spawn.
        self._active_subprocesses.pop(task_id, None)

    async def _run_task_inline(self, task_id: str) -> None:
        task = await self.repos.tasks.get(task_id)
        if task is None:
            return
        # Resolve the running agent. After A3 every task has agent_id; legacy
        # callers that only set persona_name still work — we treat the
        # persona name as a degenerate agent_id (which is exactly how the
        # bootstrap-seeded agents are set up anyway).
        agent_id = task.agent_id or task.persona_name
        agent = await self.repos.agents.get(agent_id)
        persona_name = agent.persona_name if agent else task.persona_name
        persona = await self.repos.personas.get(persona_name)
        if persona is None:
            log.error("missing_persona", task_id=task_id, persona=persona_name)
            await self.repos.tasks.update_status(task_id, "failed")
            return

        # Only write agent_id to the wakeup row when the FK actually
        # resolves — tasks created with a bare persona name (legacy
        # tests, pre-A3 callers) leave agent=None and the FK
        # `wakeups.agent_id REFERENCES agents(id)` would fail.
        wakeup_id = await self.repos.wakeups.start(
            task_id, persona.name,
            agent_id=agent.id if agent is not None else None,
        )
        claimed = await self.repos.tasks.claim_lease(
            task_id, wakeup_id, duration_sec=task.lease_duration_s
        )
        if not claimed:
            log.warning("lease_unclaimed", task_id=task_id)
            return

        transcript = TranscriptWriter(self.config.object_store_path, wakeup_id)
        worktree_handle: WorktreeHandle | None = None
        if persona.needs_worktree:
            worktree_handle = await self.worktree_manager.prepare(task_id)

        # Start blocker watcher. Baseline = whatever's already been processed
        # by this persona at wakeup-start time, so the agent sees ALL blockers
        # that haven't been handled yet (not just ones that arrive during this
        # wakeup).
        # Mailbox is keyed by agent_id (not persona name) post-A3. For
        # workers like "worker-maintainer-1", agent_id != persona name.
        await self.repos.mailbox.ensure_mailbox(agent_id)
        # Baseline = highest existing mail id at wakeup start. MailWatcher
        # only signals for mail that arrives AFTER that — so the agent
        # gets the pre-existing inbox via the normal Phase 0 task goal,
        # and mid-wakeup interrupts are reserved for genuinely new mail.
        baseline = await self.repos.mailbox.get_max_msg_id(agent_id)
        blocker_watcher = MailWatcher(
            repos=self.repos,
            recipient=agent_id,
            baseline_msg_id=baseline,
            min_urgency="high",  # high also surfaces, but only at turn boundaries
            poll_interval_s=self.poll_interval_s,
        )
        await blocker_watcher.start()

        succeeded = False
        try:
            task = await self.repos.tasks.get(task_id)
            if task is None:
                raise RuntimeError("task disappeared")

            preference = self._preference_for(persona)
            candidates = self.router.select(preference)
            log.info(
                "task_running",
                task_id=task_id,
                persona=persona.name,
                wakeup_id=wakeup_id,
                tools=persona.allowed_lyre_tools,
                candidates=[c.id for c in candidates],
                worktree=str(worktree_handle.dir) if worktree_handle else None,
            )

            other_agents = await self.repos.agents.list_all()
            system_prompt = assemble_system_prompt(
                persona,
                agent_id=agent_id,
                memory_root=self.config.memory_path,
                worktree_cwd=(
                    worktree_handle.dir if worktree_handle else None
                ),
                other_agents=other_agents,
            )
            initial_user_msg = await assemble_initial_user_message(
                task,
                tasks_repo=self.repos.tasks,
            )

            extras: dict[str, Any] = {}
            if worktree_handle:
                extras["worktree"] = str(worktree_handle.dir)
                extras["env_overlay"] = worktree_handle.env_overlay()
            if self.config.memory_path is not None:
                extras["memory_root"] = str(self.config.memory_path)
            # list_models / future router-aware tools read these.
            extras["model_registry"] = self.registry
            extras["health_tracker"] = self.health
            # archive_agent (tool) consults the agents table at call time
            # for "is this a bootstrap-seeded singleton" via
            # parent_agent_id IS NULL. No need to pre-compute a snapshot
            # here — DB is the SSOT.
            tool_ctx = ToolContext(
                repos=self.repos,
                task_id=task_id,
                wakeup_id=wakeup_id,
                persona_name=persona.name,
                agent_id=agent_id,
                extras=extras,
            )
            agent_loop = AgentLoop(
                candidates=candidates,
                adapter_for=self._adapter_for_entry,
                model_name_for=model_name_for_provider,
                transcript=transcript,
                tool_registry=self.tool_registry,
                tool_context=tool_ctx,
                allowed_tools=list(persona.allowed_lyre_tools or []),
                max_tokens=self.config.max_tokens,
                health=self.health,
                blocker_watcher=blocker_watcher,
                kill_switch=self.kill_switch,
                compact_threshold=self.config.compact_threshold,
            )

            # Kill point 1: lease claimed, context assembled, before any action
            self.kill_switch.check("before_action")

            result = await agent_loop.run(
                system_prompt=system_prompt,
                initial_messages=[initial_user_msg],
            )

            # Kill point 3 boundary: agent has finished its work (possibly
            # incl. remote-side push/PR via shell_exec) and may or may not
            # have called report_side_effect. If the agent itself simulated
            # death mid-stream, agent_loop has already raised; we don't reach
            # here.
            self.kill_switch.check("post_action_pre_report")

            await self.repos.wakeups.set_transcript_uri(wakeup_id, transcript.uri)
            chosen_entry = (
                self.registry.by_id(result.model_id) if result.model_id else None
            )
            await self.repos.wakeups.end(
                wakeup_id,
                end_status=result.status,  # may be "silent_close" — wakeup-only signal
                metering={
                    "token_input": result.usage.get("input_tokens"),
                    "token_output": result.usage.get("output_tokens"),
                    "wall_clock_ms": result.wall_clock_ms,
                    "tool_call_count": len(result.tool_calls),
                    "provider": chosen_entry.provider if chosen_entry else None,
                    "model": result.model_id,
                    "context_peak_tokens": result.context_peak_tokens,
                    "compaction_count": result.compaction_count,
                },
            )
            task_status = _wakeup_status_to_task_status(result.status)
            await self.repos.tasks.update_status(task_id, task_status)
            succeeded = task_status == "completed"
            log.info(
                "task_done",
                task_id=task_id,
                status=result.status,
                turns=result.turns,
                tool_calls=len(result.tool_calls),
                text_chars=len(result.text),
                model_id=result.model_id,
                fallbacks=len(result.fallback_events),
                interrupts=len(result.interrupt_events),
            )

            # Best-effort post-wakeup summary. Replaces the old summary-agent
            # persona: instead of scheduling a separate agent that reads
            # other agents' transcripts, we do one cheap-model call inline
            # here and append a few bullets to the agent's notes file. The
            # wakeup is already finalized in the DB above; this only adds
            # filesystem context for the agent's NEXT wakeup. Any failure
            # is swallowed inside the helper.
            if not is_simulated_kill_in_flight() and task.agent_id is not None:
                await summarize_and_append(
                    wakeup_id=wakeup_id,
                    agent_id=task.agent_id,
                    persona_name=persona.name,
                    result=result,
                    memory_path=self.config.memory_path,
                    router=self.router,
                    adapter_for_entry=self._adapter_for_entry,
                )
        except Exception as e:  # noqa: BLE001
            log.exception("task_failed", task_id=task_id, error=str(e))
            await self.repos.wakeups.end(
                wakeup_id,
                end_status="failed",
                failure_report={"error": str(e), "type": type(e).__name__},
            )
            await self.repos.tasks.update_status(task_id, "failed")
        finally:
            # Simulated process death (Q5 chaos test): if a SimulatedKill is
            # propagating, real process would already be dead and no `finally`
            # would run. Skip cleanup to match that semantics so the next
            # scheduler tick can recover via find_expired_leases.
            if is_simulated_kill_in_flight():
                log.warning(
                    "scheduler_skipping_cleanup_due_to_simulated_kill",
                    task_id=task_id,
                    wakeup_id=wakeup_id,
                )
            else:
                await blocker_watcher.stop()
                transcript.close()
                await self.repos.tasks.release_lease(task_id, wakeup_id)
                if worktree_handle is not None:
                    # On success: wipe local state, remote-side git/PR is the truth.
                    # On failure: keep dir for postmortem; agent gets killed either way.
                    await self.worktree_manager.cleanup(
                        worktree_handle, remove_dir=succeeded
                    )

    def _preference_for(self, persona: Persona) -> ModelPreference:
        pref = ModelPreference.from_dict(persona.model_preference)
        if pref is None:
            raise RuntimeError(
                f"Persona {persona.name!r} has no model_preference. Every "
                "non-owner persona must declare one (see PERSONAS.md Q9)."
            )
        return pref

    def _adapter_for_entry(self, entry: ModelEntry) -> LLMAdapter:
        if self.adapter_for_test is not None:
            return self.adapter_for_test(entry)
        return self.adapter_factory.make(entry)
