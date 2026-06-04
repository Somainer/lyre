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
from ..persistence.models import (
    Agent,
    MailboxMessage,
    OutboxRow,
    Persona,
    ScheduledMail,
    Task,
    TaskSpec,
    Urgency,
)
from ..persistence.repositories import Repositories
from ..runtime.adapter_factory import AdapterFactory, model_name_for_provider
from ..runtime.agent_loop import AgentLoop
from ..runtime.context import assemble_initial_user_message, assemble_system_prompt
from ..runtime.git_context import GitContextHandle, GitContextProvisioner
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


# task_terminated mail (OTP `monitor`/DOWN analogue): only a TERMINAL task
# warrants notifying its supervisor; pending/in_progress/needs_input are still
# in-flight. Failure rides urgency=high so MailWatcher surfaces it mid-wakeup
# even to a busy supervisor; completion/cancellation are normal.
_TERMINAL_TASK_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "cancelled"}
)
_TASK_OUTCOME_URGENCY: dict[str, str] = {
    "completed": "normal",
    "failed": "high",
    "cancelled": "normal",
}


def _should_restart(policy: str, outcome: str) -> bool:
    """OTP restart-type semantics for an ephemeral child's latest outcome.
    ``permanent`` restarts on any terminal outcome; ``transient`` only on an
    abnormal one (failed); ``temporary`` (the default) never restarts."""
    if policy == "permanent":
        return True
    if policy == "transient":
        return outcome == "failed"
    return False


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
        git_context_provisioner: GitContextProvisioner | None = None,
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
        self.git_context_provisioner = (
            git_context_provisioner or GitContextProvisioner()
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

    async def _log_terminal_task_orphan_wakeups(self) -> None:
        """One-shot startup audit: log any wakeup row left open against
        a task that's already in a terminal state. This combination
        means a previous run died after the task finished but before
        its wakeup row got finalised — runtime metadata corruption
        that ``has_active_for_agent``'s task-status JOIN now masks at
        dispatch time (so it no longer wedges the scheduler), but
        which is still worth surfacing to the operator. We do NOT
        auto-close these here — keeping the row visible until someone
        looks is the whole point of "log, don't repair".
        """
        orphans = await self.repos.wakeups.find_terminal_task_orphans(limit=10)
        if not orphans:
            return
        log.warning(
            "scheduler_terminal_task_orphan_wakeups_detected",
            count=len(orphans),
            samples=orphans,
        )

    async def run(self) -> None:
        log.info(
            "scheduler_started",
            poll_interval_s=self.poll_interval_s,
            registry_entries=len(self.registry.entries),
            override=self.config.model_override,
            max_concurrent_tasks=self._max_concurrent,
            spawn_subprocess=self.spawn_subprocess,
        )
        await self._log_terminal_task_orphan_wakeups()
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

        # Phase 0.5 (fan-in barrier): resolve any workflow barrier whose
        # delivered result-mails reached quorum (or whose deadline passed)
        # and deliver the coordinator a 'ready' mail. Runs BEFORE Phase 0 so
        # that wake mail is picked up by auto-wake on the SAME tick.
        await self._resolve_fan_in_barriers()

        # Phase 0 (mail-triggered wakeup): if any agent has unread mail and
        # has no in-flight task, create an auto-"check inbox" task so the
        # message gets read.
        if self.auto_wake_on_mail:
            await self._auto_dispatch_for_unread_mail()

        # Phase 0.7 (workflow barrier resume): flip any task parked in
        # 'needs_input' whose resume flag is set back to 'pending' so Phase 3
        # claims it on this same tick. Inert today — nothing parks a task
        # yet, so find_resumable() returns []. The scheduler-driven fan-in
        # barrier (a later PR) is what raises the flag; doing the canonical
        # needs_input -> pending transition HERE (and only here) keeps the
        # park/resume state machine single-writer and kill-safe.
        await self._resume_parked_tasks()

        # Phase 0.8 (reaper): reclaim ephemeral agents whose work is done, so
        # spawned workers (fan-in panel members etc.) don't accumulate. Inert
        # until something spawns an ephemeral agent.
        await self._reap_ephemeral_agents()

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
                # An ephemeral child whose lease keeps expiring is in a
                # raw-SIGKILL crash-loop that bypasses the reaper (its task
                # never reaches a terminal status, so Phase 0.8 never sees it).
                # Bound it with the same restart-intensity budget; on exceed it
                # is failed + escalated + reclaimed here instead of re-run.
                if await self._ephemeral_recovery_exceeded(t):
                    continue
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

        # Owner-facing bootstrap singletons (parent_agent_id NULL) must not be
        # starved by a burst of spawned ephemeral children. Two measures:
        #   1. dispatch their pending tasks FIRST (stable sort keeps FIFO within
        #      each group), so a runnable singleton always beats children;
        #   2. (subprocess mode, max_concurrent>1) RESERVE one slot for them —
        #      non-singleton tasks are capped at max_concurrent-1 concurrent, so
        #      children can never occupy every slot and make the owner wait.
        #      Inductively this keeps total non-singleton subprocesses ≤
        #      max_concurrent-1, hence ≥1 slot always reachable by a singleton.
        singleton_ids = await self.repos.agents.list_bootstrap_singleton_ids()
        pending.sort(key=lambda t: 0 if t.agent_id in singleton_ids else 1)
        if self.spawn_subprocess and self._max_concurrent > 1:
            nonsingleton_cap = max(
                0, self._max_concurrent - 1 - len(self._active_subprocesses)
            )
        else:
            nonsingleton_cap = slots

        # Agents are sequential actors: a second pending task for the
        # same agent must wait until that agent's current wakeup ends.
        # Without this guard two subprocesses for the same agent_id
        # race on shared filesystem state (scratchpad, notes,
        # auto-summary log) — last writer wins, lost updates,
        # interleaved log entries. Parallelism within a persona =
        # multiple agent INSTANCES, not multiple wakeups of one
        # agent. See docs/design/AGENT_RUNTIME.md.
        dispatched = 0
        dispatched_nonsingleton = 0
        # Track agents we've already claimed work for in this tick so
        # we don't dispatch two pending tasks of the same agent in
        # the same tick (the DB has_active check wouldn't notice yet).
        claimed_in_this_tick: set[str] = set()
        for t in pending:
            if dispatched >= slots:
                break
            is_singleton = t.agent_id in singleton_ids
            if not is_singleton and dispatched_nonsingleton >= nonsingleton_cap:
                # Reserved slot — only a bootstrap singleton may take it.
                continue
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
            if not is_singleton:
                dispatched_nonsingleton += 1
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
        # One bulk query for "which agents already own an active task" instead
        # of a per-agent find_active_for_persona inside the loop (N+1). Reflects
        # task ownership at scan start, matching the prior per-agent semantics.
        busy_agent_ids = await self.repos.tasks.active_owner_agent_ids()
        for agent in agents:
            if agent.id == "owner":
                continue  # owner has no LLM, never wakeable
            # (list_all() already excludes archived agents — no guard needed.)

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

            # Skip if this agent already has an in-flight task (predicate
            # unchanged: own an active task by exact agent_id; NULL-owner rows
            # never matched and are excluded from the set).
            if agent.id in busy_agent_ids:
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
                        # Carry the 主线 from the triggering mail onto the task,
                        # so the woken wakeup knows its thread (T2 → T3/T4).
                        "thread_id": (
                            top.metadata.get("thread_id") if top.metadata else None
                        ),
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

    async def _resolve_fan_in_barriers(self) -> None:
        """Phase 0.5: resolve mailbox-driven fan-in barriers.

        Counts DELIVERED result-mails (not completed child tasks — that would
        race the outbox: a child can be 'completed' before its result-mail is
        dispatched). When a group reaches ``quorum`` (or its deadline passes),
        deliver a high-urgency 'ready' mail to the coordinator, then flip the
        group to a terminal status with a guarded single-winner UPDATE.

        Mail-BEFORE-flip is deliberate and self-healing: a SIGKILL between the
        two re-delivers the idempotent mail (UNIQUE recipient+external_id) and
        retries the flip next tick, so a resolved group can never end up with
        no wake (the flip-first ordering could strand the coordinator). The
        coordinator is idle (its open-barrier task COMPLETED — never parked),
        so the existing Phase 0 auto-wake picks up the ready mail. No lease, no
        wakeup, no LLM here — this phase only reads + enqueues.
        """
        if not await self.repos.fan_in.any_open():
            return
        from datetime import timedelta

        from ..persistence.models import MailboxMessage as _Msg
        from ..runtime.future_mail import now_utc

        now = now_utc()
        # Global TTL backstop (PR6): force-expire any open group older than
        # LYRE_FANIN_MAX_AGE regardless of its own (coordinator-set, up to 24h)
        # deadline. 0 disables — the per-group deadline is the always-on
        # liveness; this is an operator ceiling. The cutoff is handed to
        # find_open so age-expired groups are pulled into this tick even when
        # >20 younger groups with earlier deadlines fill the deadline-sorted
        # page; otherwise an old group with a far-future deadline sorts to the
        # back and leaks past the ceiling under load.
        max_age = self.config.fanin_max_age_s
        ttl_cutoff = now - timedelta(seconds=max_age) if max_age > 0 else None
        for g in await self.repos.fan_in.find_open(limit=20, ttl_cutoff=ttl_cutoff):
            delivered = await self.repos.mailbox.count_fan_in_results(
                g.coordinator_agent_id, g.id
            )
            ready = delivered >= g.quorum
            timed_out = g.deadline is not None and now >= g.deadline
            ttl_expired = (
                max_age > 0
                and g.created_at is not None
                and (now - g.created_at).total_seconds() > max_age
            )
            if not (ready or timed_out or ttl_expired):
                continue
            new_status = "quorum_met" if ready else "expired"
            trigger = "quorum" if ready else ("ttl" if ttl_expired else "deadline")
            await self.repos.mailbox.ensure_mailbox(g.coordinator_agent_id)
            await self.repos.mailbox.insert_message(
                _Msg(
                    recipient=g.coordinator_agent_id,
                    external_id=f"fanin:{g.id}:resolved",
                    sender="system:fan-in",
                    urgency="high",
                    title=f"fan-in {g.id} ready ({new_status})",
                    body=(
                        f"Fan-in barrier {g.id} is ready to aggregate: "
                        f"{delivered}/{g.expect_replies} legs delivered "
                        f"(quorum {g.quorum}, status {new_status}, "
                        f"trigger {trigger}). Read your result-mails (they carry "
                        f"metadata.fan_in.group_id={g.id}) and synthesize."
                    ),
                    task_id=g.parent_task_id,
                    metadata={
                        "fan_in_resolved": g.id,
                        "delivered": delivered,
                        "resolved_status": new_status,
                        "trigger": trigger,
                    },
                )
            )
            flipped = await self.repos.fan_in.set_status(
                g.id, new_status, guard="open"
            )
            if flipped:
                log.info(
                    "fan_in_resolved",
                    group_id=g.id,
                    status=new_status,
                    trigger=trigger,
                    delivered=delivered,
                    quorum=g.quorum,
                    coordinator=g.coordinator_agent_id,
                )

    async def _resume_parked_tasks(self) -> None:
        """Phase 0.7: resume tasks parked in 'needs_input' once their resume
        flag is set.

        This is the ONLY writer of the needs_input -> pending transition.
        Whatever satisfied the wait (a fan-in barrier predicate, a deadline,
        an escalation) just raises ``resume_ready``; the canonical transition
        happens here so the state machine stays single-writer and kill-safe:
        a SIGKILL after the flag is set but before the flip re-resumes on the
        next tick (``resume`` is guarded + idempotent). Inert until something
        parks a task — ``find_resumable`` returns [] in steady state.
        """
        resumable = await self.repos.tasks.find_resumable(limit=20)
        for t in resumable:
            if await self.repos.tasks.resume(t.id):
                log.info("scheduler_resumed_parked_task", task_id=t.id)

    async def _reap_ephemeral_agents(self) -> None:
        """Phase 0.8: supervise + reclaim ephemeral agents whose work is done.

        For each ephemeral agent with no in-flight task (race/orphan handling
        documented on ``find_reapable_ephemerals``), apply its restart policy
        to the latest task's outcome:

          * should-restart (transient on failure / permanent on any) AND within
            restart intensity → re-dispatch the leg one-for-one (same agent,
            same goal + metadata, so a fan-in member retries its leg). Silent —
            a deterministic routine restart.
          * should-restart but intensity exceeded → escalate (a high-urgency
            mail to the supervisor — the one LLM entry point) and reclaim.
          * no restart, latest FAILED → a failure notice to the supervisor
            (PR3's task_terminated is suppressed for ephemeral agents — the
            reaper owns their lifecycle — so this is where that failure
            surfaces), then reclaim.
          * no restart, clean → reclaim silently.

        Each agent's decision is one transaction (bump+restart, or
        archive+mail), so a SIGKILL leaves either the old state or the new,
        never half. Reclaim is idempotent; a re-dispatched task is in-flight
        and removes the agent from the candidate set until it terminates again,
        which serialises restart vs. re-reap.

        KNOWN GAP (PR4c): a child killed by a raw SIGKILL (no end-of-wakeup
        write) is recovered by Phase 2 lease-expiry, which re-runs it without
        bumping intensity — so a repeated-SIGKILL crash-loop is not yet bounded
        here. Exception/normal failures DO terminate the task and ARE bounded
        by this reaper.
        """
        from ..runtime.future_mail import now_utc

        now = now_utc()
        for agent in await self.repos.agents.find_reapable_ephemerals(limit=20):
            sup = (agent.metadata or {}).get("supervision", {})
            latest = await self.repos.tasks.find_latest_task_for_agent(agent.id)
            outcome = latest.status if latest is not None else "completed"
            policy = sup.get("restart", "temporary")

            if latest is not None and _should_restart(policy, outcome):
                max_r = int(sup.get("max_restarts", 3))
                max_s = int(sup.get("max_seconds", 60))
                restarted = False
                async with self.repos.transaction():
                    within = await self.repos.supervision.bump_and_check_intensity(
                        agent.id, max_r, max_s, now, reason=outcome
                    )
                    if within:
                        await self.repos.tasks.create(
                            TaskSpec(
                                agent_id=agent.id,
                                goal=latest.goal,
                                acceptance=latest.acceptance,
                                parent_task_id=latest.parent_task_id,
                                metadata=latest.metadata,
                            )
                        )
                        restarted = True
                    else:
                        await self.repos.supervision.mark_escalated(agent.id, now)
                        await self.repos.agents.archive(
                            agent.id, reason="storm_halted"
                        )
                        await self._insert_supervision_mail(
                            agent, latest, kind="escalation", urgency="high"
                        )
                log.info(
                    "scheduler_supervised_ephemeral",
                    agent_id=agent.id,
                    action="restarted" if restarted else "escalated",
                    outcome=outcome,
                )
            else:
                async with self.repos.transaction():
                    if latest is not None and latest.status == "failed":
                        await self._insert_supervision_mail(
                            agent, latest, kind="failure", urgency="high"
                        )
                    await self.repos.agents.archive(agent.id, reason="reaped")
                log.info(
                    "scheduler_reaped_ephemeral_agent",
                    agent_id=agent.id,
                    persona=agent.persona_name,
                    outcome=outcome,
                )

    async def _ephemeral_recovery_exceeded(self, task: Task) -> bool:
        """For Phase 2 lease recovery: if ``task`` belongs to an ephemeral
        agent, count this recovery against the agent's restart intensity. While
        within budget, return False (let the normal recovery re-run proceed).
        On exceed, fail the task, escalate to the supervisor, and ARCHIVE the
        agent — so a repeated raw-SIGKILL crash-loop (which never produces a
        terminal task for the reaper to bound) can't recover forever. Returns
        True iff it handled the task (caller must skip the re-run).

        Non-ephemeral tasks are never bounded here — return False so ordinary
        chaos recovery is unchanged.
        """
        if task.agent_id is None:
            return False
        agent = await self.repos.agents.get(task.agent_id)
        if agent is None:
            return False
        sup = (agent.metadata or {}).get("supervision", {})
        if not sup.get("ephemeral"):
            return False

        from ..runtime.future_mail import now_utc

        now = now_utc()
        within = await self.repos.supervision.bump_and_check_intensity(
            task.agent_id,
            int(sup.get("max_restarts", 3)),
            int(sup.get("max_seconds", 60)),
            now,
            reason="lease_expired",
        )
        if within:
            return False
        # Exceeded: terminate the loop. Failing the task makes it terminal (so
        # find_expired_leases stops returning it); archiving the agent keeps the
        # reaper from then restarting the now-failed task. One transaction.
        async with self.repos.transaction():
            await self.repos.supervision.mark_escalated(task.agent_id, now)
            await self.repos.tasks.update_status(task.id, "failed")
            await self.repos.agents.archive(task.agent_id, reason="storm_halted")
            await self._insert_supervision_mail(
                agent, task, kind="escalation", urgency="high"
            )
        log.warning(
            "scheduler_ephemeral_recovery_exceeded",
            task_id=task.id,
            agent_id=task.agent_id,
        )
        return True

    async def _resolve_supervisor_for_agent(self, agent: Agent) -> str | None:
        """The supervisor of an ephemeral ``agent``: its spawner
        (``parent_agent_id``) if live and not archived, else ``owner``."""
        parent = agent.parent_agent_id
        if parent:
            pa = await self.repos.agents.get(parent)
            if pa is not None and pa.status != "archived":
                return parent
        owner = await self.repos.agents.get("owner")
        if owner is not None and owner.status != "archived":
            return "owner"
        return None

    async def _insert_supervision_mail(
        self, agent: Agent, latest: Task | None, *, kind: str, urgency: Urgency
    ) -> None:
        """Direct mailbox insert (the scheduler isn't in a wakeup, so it can't
        use the wakeup-keyed outbox — mirrors Phase -1/0.5). Idempotent via a
        deterministic external_id. Joins the caller's transaction."""
        recipient = await self._resolve_supervisor_for_agent(agent)
        if recipient is None:
            log.warning("supervision_mail_no_recipient", agent_id=agent.id, kind=kind)
            return
        sup = (agent.metadata or {}).get("supervision", {})
        last_status = latest.status if latest is not None else "unknown"
        if kind == "escalation":
            external_id = f"supervision:{agent.id}:escalation"
            tag = f"[escalation] ephemeral {agent.id} exceeded restart intensity"
            detail = (
                f"Agent {agent.id} (persona {agent.persona_name}) hit its restart "
                f"limit (max_restarts={sup.get('max_restarts', 3)} within "
                f"{sup.get('max_seconds', 60)}s); reclaimed without further restart. "
                f"Last outcome: {last_status}. Decide: re-plan, re-spec, or drop "
                f"this leg."
            )
        else:  # failure
            external_id = (
                f"supervision:{agent.id}:failed:"
                f"{latest.id if latest is not None else 'na'}"
            )
            tag = f"[failed] ephemeral {agent.id} did not succeed"
            detail = (
                f"Agent {agent.id} (persona {agent.persona_name}) task "
                f"{latest.id if latest is not None else '?'} ended {last_status!r}; "
                f"its restart policy ({sup.get('restart', 'temporary')}) does not "
                f"retry it. The agent has been reclaimed."
            )
        await self.repos.mailbox.ensure_mailbox(recipient)
        await self.repos.mailbox.insert_message(
            MailboxMessage(
                recipient=recipient,
                external_id=external_id,
                sender="system:supervisor",
                urgency=urgency,
                body=f"{tag}\n\n{detail}",
                metadata={
                    "kind": f"supervision_{kind}",
                    "agent_id": agent.id,
                    "outcome": last_status,
                    "parent_task_id": (
                        latest.parent_task_id if latest is not None else None
                    ),
                },
            )
        )

    async def _resolve_terminated_task_supervisor(self, task: Task) -> str | None:
        """The agent that should receive a ``task_terminated`` mail for
        ``task`` — Lyre's OTP `monitor` analogue, but unidirectional so a
        worker's death never cascade-kills its supervisor.

        Resolution order:
          1. ``parent_task_id`` set + that parent task's agent is live and
             not archived → the parent's agent (dispatch creates the monitor).
          2. Otherwise ``owner`` — top-level tasks root at the human owner.
        An archived parent falls through to owner so the signal isn't lost.
        Returns None only when even the owner record is missing (fresh test
        DBs), in which case the caller skips the enqueue.
        """
        if task.parent_task_id:
            parent = await self.repos.tasks.get(task.parent_task_id)
            if parent is not None and parent.agent_id:
                parent_agent = await self.repos.agents.get(parent.agent_id)
                if parent_agent is not None and parent_agent.status != "archived":
                    return parent.agent_id
        owner = await self.repos.agents.get("owner")
        if owner is not None and owner.status != "archived":
            return "owner"
        return None

    async def _emit_task_terminated_mail(
        self,
        task: Task | None,
        wakeup_id: str,
        task_status: str,
        *,
        summary: str | None,
        failure_reason: str | None,
        transcript_uri: str | None,
    ) -> None:
        """Notify the supervisor that ``task`` reached a terminal state, via an
        ordinary outbox ``mailbox_send`` — same durable, idempotent, Phase-0-
        auto-waking path as any agent-to-agent mail. Supervisors then react to
        child terminations instead of polling ``query_task_status``.

        ``metadata.kind == 'task_terminated'`` lets a supervisor pattern-match
        without parsing the body. external_id is ``task_terminated:<task_id>``
        so a double-fire (e.g. a retried tick) dedupes at the mailbox UNIQUE.

        Deliberately SUPPRESSED in two cases, so this doesn't fight other
        subsystems:
          * fan-in member tasks — the barrier (PR2) already aggregates their
            results as low-urgency mail; a normal-urgency notice here would
            prematurely auto-wake the coordinator on the first child.
          * auto-dispatched 'check inbox' tasks — internal scheduler bookkeeping,
            not work the owner should be pinged about.
        And for TOP-LEVEL tasks we notify the owner only on FAILURE: a
        successful top-level task already replies to the owner through the
        agent's own mail, so a system completion ping would just be noise — but
        a silent failure is exactly the "sudden failed 没人知道" gap we close.
        """
        if task is None or task_status not in _TERMINAL_TASK_STATUSES:
            return
        meta = task.metadata or {}
        if meta.get("fan_in_group") is not None or meta.get("auto_dispatched"):
            return
        if task.parent_task_id is None and task_status != "failed":
            return
        # Ephemeral agents' lifecycle is owned by the reaper (Phase 0.8): it
        # restarts, escalates, or surfaces their failure itself. A second
        # notice here would double-signal and could prod the supervisor into
        # re-handling a failure the reaper is already restarting.
        if task.agent_id is not None:
            agent = await self.repos.agents.get(task.agent_id)
            if agent is not None and (agent.metadata or {}).get(
                "supervision", {}
            ).get("ephemeral"):
                return

        recipient = await self._resolve_terminated_task_supervisor(task)
        if recipient is None:
            log.warning("task_terminated_no_recipient", task_id=task.id)
            return

        urgency = _TASK_OUTCOME_URGENCY.get(task_status, "normal")
        external_id = f"task_terminated:{task.id}"
        # The mail title is derived from the body's first line (the dispatcher
        # does not carry an explicit title), so lead with the outcome tag; the
        # machine signal supervisors match on is metadata.kind, not the title.
        goal_preview = (task.goal or "").strip().splitlines()[0:1]
        tag = f"[{task_status}] task {task.id[:8]}" + (
            f" — {goal_preview[0][:80]}" if goal_preview else ""
        )
        detail = summary or f"reached terminal state: {task_status}."
        payload = {
            "recipient": recipient,
            "sender": "system:supervisor",
            "urgency": urgency,
            "body": f"{tag}\n\n{detail}",
            "task_id": task.id,
            "external_id": external_id,
            "metadata": {
                "kind": "task_terminated",
                "task_id": task.id,
                "outcome": task_status,
                "failure_reason": failure_reason,
                "parent_task_id": task.parent_task_id,
                "transcript_uri": transcript_uri,
            },
        }
        await self.repos.outbox.enqueue(
            [
                OutboxRow(
                    task_id=task.id,
                    wakeup_id=wakeup_id,
                    kind="mailbox_send",
                    payload=payload,
                    external_id=external_id,
                )
            ]
        )
        log.info(
            "task_terminated_mail_enqueued",
            task_id=task.id,
            recipient=recipient,
            outcome=task_status,
            urgency=urgency,
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
            # T4: a recurring self-mail is a bounded loop. This delivery is
            # occurrence #(occurrence_count+1); if it reaches max_occurrences it
            # is the FINAL wake — mark it high-urgency with a wrap-up note and
            # don't re-arm. The scheduler (not the model) enforces the ceiling,
            # so a confused loop can't run forever.
            loop_final = (
                sched.max_occurrences is not None
                and sched.occurrence_count + 1 >= sched.max_occurrences
            )
            urgency = "high" if loop_final else sched.urgency
            body = sched.body
            if loop_final:
                body = (
                    f"[loop budget reached: {sched.occurrence_count + 1}/"
                    f"{sched.max_occurrences} iterations — this is your LAST "
                    f"scheduled wake on this thread. Finish up or escalate; "
                    f"you will NOT be auto-woken again.]\n\n" + sched.body
                )

            external_id = f"sched:{sched.id}:{sched.occurrence_count}"
            await self.repos.mailbox.ensure_mailbox(recipient)
            msg_id = await self.repos.mailbox.insert_message(
                _Msg(
                    recipient=recipient,
                    external_id=external_id,
                    sender=sched.sender,
                    urgency=urgency,
                    title=sched.title,
                    body=body,
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
            if loop_final:
                next_fire = None  # budget exhausted → stop re-arming
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

        # Sweep stale wakeups for this task BEFORE opening a new one.
        # A previous attempt may have died after INSERTing its wakeup
        # row but before any path could write ended_at — kill-test
        # crash, host shutdown without graceful drain, or just the
        # claim-lease-fails return path below from an earlier tick.
        # If we don't close them here, the orphan row keeps tripping
        # has_active_for_agent for this agent (the scheduler dispatch
        # gate), starving every future pending task. Sweeping is by
        # task_id so we never touch wakeups for OTHER live tasks of
        # the same agent (there shouldn't be any — we're about to be
        # the one wakeup — but the narrow filter is the safer one).
        abandoned = await self.repos.wakeups.close_orphans_for_task(task_id)
        if abandoned:
            log.warning(
                "scheduler_closed_orphan_wakeups",
                task_id=task_id,
                count=abandoned,
            )

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
            # The wakeup row got INSERTed before we attempted the claim
            # (wakeups.start commits unconditionally). If we just return
            # here, the row stays at ended_at IS NULL forever — exactly
            # the orphan pattern close_orphans_for_task exists to fix.
            # Close our own freshly-inserted row in the same code path
            # rather than relying on the next _run_task to sweep it.
            log.warning(
                "lease_unclaimed", task_id=task_id, wakeup_id=wakeup_id,
            )
            await self.repos.wakeups.end(wakeup_id, end_status="abandoned")
            return

        transcript = TranscriptWriter(self.config.object_store_path, wakeup_id)
        # The transcript fd is open from here. The setup below (worktree mkdir,
        # git provisioning, mailbox baseline) can raise BEFORE the main
        # try/finally takes ownership of cleanup — guard it so a setup failure
        # releases the fd instead of leaking it. The lease + wakeup row stay
        # dangling on purpose: the next tick recovers them via
        # find_expired_leases (kill-test semantics).
        try:
            # Every wakeup gets a worktree (empty tmpdir). Whether it's
            # a git working copy depends on TaskSpec.git_context — see
            # the git_context provisioning below. The worktree itself is
            # cheap (one mkdir) and uniformly available simplifies
            # downstream tool / prompt logic (no "do I have a sandbox"
            # branching anywhere).
            worktree_handle: WorktreeHandle = (
                await self.worktree_manager.prepare(task_id)
            )

            # Optional git_context overlay: if the task was dispatched
            # with a repo + branch spec, provision an ephemeral SSH key
            # + ssh-agent and clone+checkout into the worktree before
            # the worker arrives. Non-git tasks (skill migration,
            # research, data shaping) skip this entirely.
            git_handle: GitContextHandle | None = None
            if task_for_setup := await self.repos.tasks.get(task_id):
                if task_for_setup.git_context is not None:
                    try:
                        git_handle = await self.git_context_provisioner.prepare(
                            task_id=task_id,
                            worktree_dir=worktree_handle.dir,
                            git_context=task_for_setup.git_context,
                        )
                    except Exception as exc:  # noqa: BLE001
                        # Provisioning failed (bad repo URL, network, etc.).
                        # Release lease, mark task failed, surface error.
                        log.warning(
                            "git_context_provision_failed",
                            task_id=task_id,
                            error=str(exc),
                        )
                        await self.repos.tasks.release_lease(task_id, wakeup_id)
                        await self.repos.tasks.update_status(task_id, "failed")
                        transcript.close()
                        await self.worktree_manager.cleanup(
                            worktree_handle, remove_dir=False,
                        )
                        return

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
        except Exception:
            transcript.close()
            raise

        blocker_watcher = MailWatcher(
            repos=self.repos,
            recipient=agent_id,
            baseline_msg_id=baseline,
            min_urgency="high",  # high also surfaces, but only at turn boundaries
            poll_interval_s=self.poll_interval_s,
        )
        try:
            await blocker_watcher.start()
        except Exception:
            # Watcher started a background poll task before failing (or failed
            # to): stop it and release the transcript fd so neither leaks.
            await blocker_watcher.stop()
            transcript.close()
            raise

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
                worktree=str(worktree_handle.dir),
                git_context_repo=(
                    git_handle.repo_url if git_handle else None
                ),
            )

            other_agents = await self.repos.agents.list_all()
            system_prompt = assemble_system_prompt(
                persona,
                agent_id=agent_id,
                memory_root=self.config.memory_path,
                worktree_cwd=worktree_handle.dir,
                other_agents=other_agents,
            )
            initial_user_msg = await assemble_initial_user_message(
                task,
                tasks_repo=self.repos.tasks,
                mailbox_repo=self.repos.mailbox,
                agent_id=agent_id,
                memory_root=self.config.memory_path,
            )

            extras: dict[str, Any] = {
                "worktree": str(worktree_handle.dir),
                # env_overlay carries SSH_AUTH_SOCK / SSH_AGENT_PID
                # only when a git_context overlay was provisioned.
                # Non-git tasks see no SSH env — no leaking of git
                # credentials into research / skill-migration tasks.
                "env_overlay": (
                    git_handle.env_overlay() if git_handle else {}
                ),
            }
            if git_handle is not None:
                extras["git_context"] = {
                    "repo_url": git_handle.repo_url,
                    "base_branch": git_handle.base_branch,
                    "target_branch": git_handle.target_branch,
                }
            if self.config.memory_path is not None:
                extras["memory_root"] = str(self.config.memory_path)
            # list_models / future router-aware tools read these.
            extras["model_registry"] = self.registry
            extras["health_tracker"] = self.health
            # list_agents' idle-reclaim `stale` hint reads this threshold;
            # 0 (default) disables it.
            extras["idle_reclaim_age_s"] = self.config.idle_reclaim_age_s
            # shell_exec(credentials=<name>) resolves bundles from here to
            # inject an external coding-agent's key into one subprocess.
            extras["coding_backends"] = self.config.coding_backends
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
                # The 主线 this wakeup is on; tools propagate it to sends/dispatches.
                thread_id=(task.metadata.get("thread_id") if task.metadata else None),
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
            task_status = _wakeup_status_to_task_status(result.status)
            # Step 9 COMMIT POINT: the wakeup-end metering, the task-status
            # advance, and the supervisor task_terminated outbox row must land
            # as ONE commit. A SIGKILL between update_status (terminal) and the
            # outbox enqueue would otherwise leave a terminal task with no
            # task_terminated mail — and find_expired_leases (in_progress-only)
            # never re-runs it, reopening the "sudden failed 没人知道" gap.
            async with self.repos.transaction():
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
                        "compaction_summary_degraded": (
                            result.compaction_summary_degraded
                        ),
                    },
                )
                await self.repos.tasks.update_status(task_id, task_status)
                # Notify the supervisor that this task terminated (OTP monitor).
                # needs_continuation→failed carries the wakeup status as a coarse
                # reason until the structured end-contract lands.
                await self._emit_task_terminated_mail(
                    task,
                    wakeup_id,
                    task_status,
                    summary=(result.text or "").strip()[:500] or None,
                    failure_reason=(
                        result.status if task_status == "failed" else None
                    ),
                    transcript_uri=transcript.uri,
                )
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
                    object_store_path=self.config.object_store_path,
                    notes_max_entries=self.config.notes_max_entries,
                )
        except Exception as e:  # noqa: BLE001
            log.exception("task_failed", task_id=task_id, error=str(e))
            # Same Step 9 COMMIT POINT as the success path: the failed wakeup-end,
            # the task->failed advance, and the supervisor notice are one commit
            # so a crash can't strand a failed task with no task_terminated mail.
            async with self.repos.transaction():
                await self.repos.wakeups.end(
                    wakeup_id,
                    end_status="failed",
                    failure_report={"error": str(e), "type": type(e).__name__},
                )
                await self.repos.tasks.update_status(task_id, "failed")
                # The original "sudden failed 没人知道" path: a mid-wakeup crash
                # never produced an end-of-wakeup declaration, so the supervisor
                # must still be told. ``task`` may be stale (pre-wakeup) but its
                # parent/metadata — all this needs — don't change mid-wakeup.
                await self._emit_task_terminated_mail(
                    task,
                    wakeup_id,
                    "failed",
                    summary=f"{type(e).__name__}: {e}"[:500],
                    failure_reason=type(e).__name__,
                    transcript_uri=getattr(transcript, "uri", None),
                )
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
                # git_context teardown first (kill ssh-agent), then
                # worktree teardown (rm -rf the dir). The order
                # matters when ``remove_dir=True``: ssh-agent has to
                # release the key file before we wipe the dir.
                if git_handle is not None:
                    await self.git_context_provisioner.cleanup(git_handle)
                # On success: wipe local state, remote-side git / PR
                # is the truth. On failure: keep dir for postmortem.
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
