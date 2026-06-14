"""Repository Protocols (DAO abstraction layer).

These Protocols describe the surface used by Lyre business code. SQLite implementations
live in sqlite_impl.py; future Postgres implementation would live in postgres_impl.py.

See PERSISTENCE_SCHEMA.md §4 for design rationale.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import aiosqlite

from .models import (
    Agent,
    AgentIdle,
    Blob,
    FanInGroup,
    FanInMember,
    FanInResult,
    MailboxMessage,
    MailReaction,
    OutboxRow,
    Persona,
    ScheduledMail,
    SupervisionState,
    Task,
    TaskSpec,
    Wakeup,
)


class AgentRepository(Protocol):
    """Agent = running instance of a persona.

    See models.Agent docstring for the persona/agent distinction.
    """

    async def create(
        self,
        agent_id: str,
        persona_name: str,
        parent_agent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Insert a new agent. Errors if id already exists.

        `parent_agent_id` records who spawned this agent — NULL for
        bootstrap roots, "owner" when the human created via CLI/dashboard,
        else another agent_id (spawned via `create_agent` tool).
        """
        ...

    async def get(self, agent_id: str) -> Agent | None: ...

    async def list_all(
        self, include_archived: bool = False
    ) -> list[Agent]: ...

    async def list_by_persona(
        self, persona_name: str, include_archived: bool = False
    ) -> list[Agent]:
        """Used by auto-naming (`<persona>-<n>`) and persona-broadcast."""
        ...

    async def archive(self, agent_id: str, reason: str | None = None) -> bool:
        """Soft delete. Returns True if the agent was active and got archived.
        ``reason`` (reaped / storm_halted / idle_reclaimed / manual) is recorded
        atomically for observability (list_agents / dashboard)."""
        ...

    async def unarchive(self, agent_id: str) -> bool:
        """Bring an archived agent back to ``status='idle'``. Returns True
        if a row actually flipped (i.e. it was archived); False if the
        row was already active or doesn't exist.

        The original ``created_at`` is preserved; ``archived_at`` is
        cleared. Mail / task history attached to this id is untouched
        — the unarchive is purely a revive of the addressable slot.
        """
        ...

    async def exists(self, agent_id: str) -> bool: ...

    async def find_reapable_ephemerals(self, limit: int = 20) -> list[Agent]:
        """Ephemeral, non-archived, previously-dispatched agents with no
        in-flight task — the reaper's reclaim candidates. See the SQLite
        implementation for the race/orphan handling."""
        ...

    async def list_bootstrap_singleton_ids(self) -> set[str]:
        """Ids of live bootstrap singletons (parent_agent_id IS NULL). Phase 3
        prioritises their tasks and reserves a slot for them."""
        ...

    async def idle_report(
        self, now: datetime, idle_threshold_s: int
    ) -> dict[str, AgentIdle]:
        """``{agent_id: AgentIdle(idle_seconds, stale)}`` for every non-archived
        agent — the pull-side primitive behind ``list_agents``' idle-reclaim
        hint. The Dispatcher reads ``stale`` and decides whether to
        ``archive_agent``; the runtime never acts on it automatically.

        ``stale`` mirrors ``find_reapable_ephemerals`` but for the NON-ephemeral
        class (idle past ``idle_threshold_s``, spawned, no in-flight task) plus
        an open-fan-in-leg guard, and is never set when ``idle_threshold_s <= 0``.
        See the SQLite implementation for the exact predicate."""
        ...

    async def update_metadata(
        self, agent_id: str, metadata: dict[str, Any]
    ) -> None: ...


class PersonaRepository(Protocol):
    async def get(self, name: str) -> Persona | None: ...
    async def list_active(self, status: str = "approved") -> list[Persona]: ...
    async def upsert(self, persona: Persona) -> None: ...
    async def propose(
        self,
        name: str,
        role_description: str,
        system_prompt: str,
        allowed_lyre_tools: list[str],
        source_task_id: str,
        **kwargs: Any,
    ) -> None: ...
    async def approve(
        self,
        persona_name: str,
        reviewer: str,
        status: str,
        comment: str | None = None,
    ) -> None: ...


class TaskRepository(Protocol):
    async def create(self, spec: TaskSpec) -> str: ...
    async def get(self, task_id: str) -> Task | None: ...
    async def claim_lease(
        self, task_id: str, holder_wakeup_id: str, duration_sec: int
    ) -> bool: ...
    async def renew_lease(
        self, task_id: str, holder_wakeup_id: str, duration_sec: int
    ) -> bool: ...
    async def release_lease(self, task_id: str, holder_wakeup_id: str) -> None: ...
    async def update_checkpoint(
        self, task_id: str, checkpoint: dict[str, Any], holder_wakeup_id: str
    ) -> None: ...
    async def thread_activity_since(self, thread_id: str, since_iso: str) -> bool:
        """H2 progress: a task on this thread was created, reached terminal,
        or had its checkpoint advanced since ``since_iso``. NOT keyed on
        updated_at (lease churn pollutes it)."""
        ...
    async def update_status(
        self, task_id: str, status: str, holder_wakeup_id: str | None = None
    ) -> bool: ...
    async def request_cancel(
        self, task_id: str, reason: str | None = None
    ) -> bool: ...
    async def get_cancel_request(self, task_id: str) -> str | None: ...
    async def bump_recovery_attempt(self, task_id: str) -> int: ...
    async def find_pending(self, limit: int = 10) -> list[Task]: ...
    async def find_expired_leases(self, limit: int = 10) -> list[Task]: ...
    async def find_children(self, parent_task_id: str) -> list[Task]: ...
    async def find_latest_task_for_agent(self, agent_id: str) -> Task | None:
        """The most-recently-created task for this agent (supervisor reaper)."""
        ...

    # --- Park / resume (scheduler-driven barrier seam) -------------------
    async def park(self, task_id: str) -> bool:
        """Park a live (pending/in_progress) task in 'needs_input'. Returns
        True iff a row flipped. A parked task is invisible to find_pending
        and find_expired_leases until resume() runs."""
        ...

    async def request_resume(self, task_id: str) -> bool:
        """Flag a parked task ready to resume (idempotent). The canonical
        transition is done by resume()."""
        ...

    async def find_resumable(self, limit: int = 20) -> list[Task]:
        """Parked tasks with resume_ready set — Phase 0.7 resumes these."""
        ...

    async def resume(self, task_id: str) -> bool:
        """needs_input -> pending, guarded + idempotent (the sole writer of
        this transition; Phase 0.7 only)."""
        ...

    # Dashboard helpers (Sprint D1)
    async def find_recent(
        self, limit: int = 50, status_filter: str | None = None
    ) -> list[Task]: ...
    async def search(
        self,
        persona_name: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> list[Task]:
        """Filter tasks by persona and/or status, newest first.

        Distinct from find_recent which only takes status. Used by the
        list_tasks introspection tool so dispatcher can see who's busy.
        """
        ...
    async def count_in_progress(self) -> int: ...
    async def count_completed_since(self, since_iso: str) -> int: ...
    async def find_recently_changed(
        self, since_iso: str, limit: int = 100
    ) -> list[Task]:
        """Tasks whose updated_at ≥ since_iso. Used to surface status
        transitions in the audit timeline."""
        ...

    async def find_active_for_persona(self, persona_name: str) -> list[Task]:
        """Tasks owned by this persona that are NOT in terminal state
        (i.e. pending / in_progress / needs_input). Used by the auto-wake-
        on-mail scheduler phase to avoid double-dispatching when the
        persona is already busy."""
        ...

    async def active_owner_agent_ids(self) -> set[str]:
        """agent_ids that currently own a non-terminal (pending/in_progress/
        needs_input) task. NULL agent_id rows are excluded. Used by Phase-0
        auto-wake to skip agents already busy without an N+1 per-agent
        query."""
        ...


class WakeupRepository(Protocol):
    async def start(
        self,
        task_id: str,
        persona_name: str,
        agent_id: str | None = None,
    ) -> str: ...
    async def end(
        self,
        wakeup_id: str,
        end_status: str,
        metering: dict[str, Any] | None = None,
        failure_report: dict[str, Any] | None = None,
    ) -> None: ...
    async def set_transcript_uri(self, wakeup_id: str, uri: str) -> None: ...

    # Dashboard helpers (Sprint D1)
    async def list_recent(self, limit: int = 50) -> list[Wakeup]: ...
    async def list_for_task(self, task_id: str, limit: int = 5) -> list[Wakeup]:
        """Wakeups for one task, newest first. Powers the is_running /
        latest-wakeup views (query_task_status, `lyre status`): an open row
        (ended_at IS NULL) is the authoritative "this task is running now"."""
        ...
    async def sum_tokens_since(self, since_iso: str) -> tuple[int, int]:
        """Return (input_tokens, output_tokens) summed across wakeups with
        started_at >= since_iso."""
        ...

    async def list_since(self, since_iso: str, limit: int = 100) -> list[Wakeup]:
        """Wakeups whose started_at OR ended_at ≥ since_iso. Used to surface
        agent wake/end events in the audit timeline."""
        ...

    async def list_active(self) -> list[Wakeup]:
        """Wakeups still in flight (ended_at IS NULL)."""
        ...

    async def thread_work_since(
        self, thread_id: str, since_iso: str
    ) -> tuple[int, int]:
        """Largest single-wakeup (tool calls, output tokens) among this
        thread's wakeups started since ``since_iso`` — the H2 work-floor
        input. MAX, not SUM: several light waiter peeks must never sum
        past the floor."""
        ...

    async def has_active_for_agent(self, agent_id: str) -> bool:
        """True iff a wakeup is currently running for this agent
        (``ended_at IS NULL``). The scheduler consults this before
        claiming a pending task so two wakeups of the same agent
        never run concurrently. Parallelism within a persona uses
        multiple AGENT INSTANCES (e.g. ``analyst/topic-A`` +
        ``analyst/topic-B``), not multiple wakeups of one agent.

        Only wakeups whose task is still in flight count — a wakeup
        row left with ``ended_at IS NULL`` for a task that has since
        reached a terminal state (completed/failed/cancelled) is
        stale metadata, not a real running process, and must not
        latch dispatch shut for the whole agent.
        """
        ...

    async def close_orphans_for_task(
        self, task_id: str, end_status: str = "abandoned"
    ) -> int:
        """Mark every wakeup of ``task_id`` still flagged active
        (``ended_at IS NULL``) as ended right now, using ``end_status``.

        Called at the top of ``Scheduler._run_task_inline`` so the
        kill-test recovery path (where a previous wakeup's process
        died without writing ``ended_at``, leaving an orphan row that
        permanently trips ``has_active_for_agent``) cleans itself up
        before opening a fresh wakeup. Returns the number of rows
        touched so the caller can surface a warning when recovery
        actually fired.
        """
        ...

    async def find_terminal_task_orphans(
        self, limit: int = 10
    ) -> list[dict[str, str]]:
        """Wakeups still flagged active (``ended_at IS NULL``) whose
        task already reached a terminal state. Each dict carries
        ``wakeup_id`` / ``task_id`` / ``agent_id`` / ``task_status``.

        Called once at scheduler startup as an audit. The pairing is
        impossible in steady-state — a terminal task can't have a
        running wakeup — so any rows surfaced here indicate prior
        runtime metadata corruption (typically a crash window between
        ``tasks.update_status`` and ``wakeups.end``). The scheduler
        logs but does NOT auto-close: the JOIN in
        ``has_active_for_agent`` already prevents these from blocking
        dispatch, so leaving the row visible is intentional — it
        documents the corruption for the operator.
        """
        ...


class MailboxRepository(Protocol):
    async def ensure_mailbox(self, recipient: str) -> None: ...

    # --- Read flow (per-message read state) ------------------------------
    async def read_unread(
        self,
        recipient: str,
        *,
        min_urgency: str | None = None,
        limit: int = 50,
    ) -> list[MailboxMessage]:
        """Mail with `read_at IS NULL`, ordered by urgency
        (blocker → high → normal → low) then id ascending. Used by the
        `mailbox_read` tool to give the agent its current inbox."""
        ...

    async def read_all_by_recipient(
        self,
        recipient: str,
        *,
        limit: int = 50,
    ) -> list[MailboxMessage]:
        """Both read and unread, id ascending. Used by
        `mailbox_read(include_read=True)` for archive browsing — does
        NOT mark anything (read state is set by `mark_messages_read`)."""
        ...

    async def mark_messages_read(
        self, recipient: str, msg_ids: list[int]
    ) -> None:
        """Set `read_at = now()` on every (recipient, id) in the list.
        Idempotent — re-marking an already-read row is a no-op (we don't
        overwrite the original read time). Called by the `mailbox_read`
        tool after the rows are returned and by the explicit `mark_read`
        tool."""
        ...

    async def count_unread(
        self, recipient: str, *, min_urgency: str | None = None
    ) -> int: ...

    async def get_max_msg_id(self, recipient: str) -> int:
        """Highest mailbox_messages.id for this recipient (0 if empty).
        Used by MailWatcher as its 'before wakeup started' baseline so
        it only fires on mail that arrives AFTER the agent started."""
        ...

    async def list_sent_by(
        self,
        sender: str,
        *,
        recipient: str | None = None,
        limit: int = 50,
    ) -> list[MailboxMessage]:
        """Mail this agent sent, newest-first. Optional `recipient` filter
        for "mail I sent to X". Powers `mailbox_read(box="sent")` so an
        agent can self-recall its commitments across wakeups (Lyre is
        stateless across wakeups; agents that promised "I'll look at X"
        rely on this to remember what they said)."""
        ...

    async def list_by_thread(
        self,
        thread_id: str,
        *,
        participant: str | None = None,
        limit: int = 20,
    ) -> list[MailboxMessage]:
        """Mail on one 主线 (metadata.thread_id), newest-first. Optional
        `participant` narrows to mail this agent sent or received. Powers
        thread-scoped context injection (T3): a wakeup sees its main-line's
        back-and-forth without the (stateless) agent hunting for it."""
        ...

    async def loop_progress_mail_since(
        self, thread_id: str, since_iso: str, self_actor: str
    ) -> bool:
        """H2 progress: thread mail that isn't self↔self, OR any send by
        the loop agent to a non-self recipient (replies inherit the parent
        thread, so off-thread output must still count as alive)."""
        ...

    # --- Internal listing helpers (system-side, not agent-facing) --------
    async def read_messages(
        self, recipient: str, since_id: int = 0, limit: int = 100
    ) -> list[MailboxMessage]:
        """ID-ascending range read. Used by tests and system helpers
        that don't care about read state. Does NOT mark anything."""
        ...

    async def read_blockers(
        self, recipient: str, since_id: int = 0
    ) -> list[MailboxMessage]:
        """Urgency='blocker' messages with id > since_id, ID-ascending.
        Used by MailWatcher for blocker mid-stream interrupts. Does NOT
        mark anything; the agent decides via mailbox_read."""
        ...

    async def insert_message(self, msg: MailboxMessage) -> int:
        """Insert a delivered message (used by dispatcher; idempotent on external_id)."""
        ...

    async def count_fan_in_results(self, recipient: str, group_id: str) -> int:
        """COUNT(DISTINCT leg_key) of fan-in result-mails delivered to
        ``recipient`` for ``group_id``. The barrier predicate input — counts
        the delivery event, not child-task completion."""
        ...

    async def read_fan_in_results(
        self, recipient: str, group_id: str
    ) -> list[FanInResult]:
        """The delivered fan-in leg results for ``group_id`` — one
        ``FanInResult`` per distinct ``leg_key`` (latest mail wins on
        redelivery), ordered by ``leg_key``. The aggregation primitive the
        coordinator's ``fan_in_results`` tool reads on resume, so it doesn't
        have to scan its inbox and re-parse low-urgency result-mails by hand."""
        ...

    async def get_message(self, msg_id: int) -> MailboxMessage | None:
        """Fetch ANY mailbox message by primary id, regardless of recipient.
        Used by `mailbox_get_message` for thread/reply/forward context.

        Implementations should hydrate the message's ``reactions`` field
        from `mail_reactions` so callers see acks inline."""
        ...

    async def add_reaction(
        self, msg_id: int, reactor: str, kind: str,
    ) -> bool:
        """Idempotently record a reaction. Returns True if a new row was
        inserted, False if the (msg_id, reactor, kind) already existed.

        Crucially does NOT touch mailbox_messages or any unread state —
        reactions are an out-of-band channel by design (see
        migrations/0005_mail_reactions.sql for the rationale)."""
        ...

    async def list_reactions(self, msg_id: int) -> list[MailReaction]:
        """All reactions on one message, oldest-first."""
        ...

    async def set_channel_external_id(
        self, msg_id: int, channel_name: str, external_id: str,
    ) -> None:
        """Persist the external-system message id under
        ``metadata.channels.<channel_name>.message_id``. Used by the
        outbox `channel_publish` handler after the channel post
        succeeds so later replies can resolve threading.

        Implementations must update ONLY that key — other channels'
        sub-trees and unrelated metadata fields must survive.
        """
        ...

    async def find_by_channel_external_id(
        self, channel_name: str, external_id: str,
    ) -> MailboxMessage | None:
        """Reverse lookup: find the mail whose metadata records
        ``channels.<channel_name>.message_id == external_id``.

        Used by external-channel inbound handlers to resolve thread
        replies (e.g. Lark / Slack message-id → original Lyre mail).
        Returns None when no row matches.
        """
        ...

    async def find_id_by_external_id(
        self, recipient: str, external_id: str,
    ) -> int | None:
        """Resolve ``(recipient, external_id)`` → mailbox row id, or
        None. Used by recovery paths after a UNIQUE-constraint collision
        on insert when we need the existing row's id to link FKs.
        """
        ...

    async def list_pending_channel_publish(
        self, *, recipient: str, channel_name: str, limit: int = 500,
    ) -> list[MailboxMessage]:
        """Owner-bound mails for which no ``channel_publish`` outbox
        row exists yet (for the given channel). Used by the owner-mail
        enqueuer to catch up after restarts / channel additions.
        """
        ...

    async def get_last_auto_triggered_id(self, recipient: str) -> int:
        """Scheduler-side cursor: highest msg_id we've already auto-dispatched
        a 'check inbox' task for. Independent of per-message read_at
        so the auto-wake loop doesn't pile on if the agent forgets to mark."""
        ...

    async def set_last_auto_triggered_id(
        self, recipient: str, msg_id: int
    ) -> None:
        """Advance the auto-trigger cursor. Monotonic — never moves backward."""
        ...

    # Dashboard helpers (Sprint D1)
    async def read_messages_paged(
        self,
        recipient: str,
        before_id: int | None = None,
        limit: int = 50,
        min_urgency: str | None = None,
    ) -> list[MailboxMessage]:
        """Time-desc paging. `before_id=None` → latest. `min_urgency`
        restricts to that urgency and "higher" (blocker > high > normal > low)."""
        ...

    async def count_unread_blockers(self, recipient: str) -> int: ...

    async def read_recent_for_audit(
        self, since_iso: str, limit: int = 200
    ) -> list[MailboxMessage]:
        """All mailbox messages (any recipient) delivered ≥ since_iso. Used by
        the audit timeline to show inter-agent communication, not just
        owner-facing inbox."""
        ...


class OutboxRepository(Protocol):
    async def enqueue(self, rows: list[OutboxRow]) -> None: ...
    async def dequeue_batch(self, limit: int = 100) -> list[OutboxRow]: ...
    async def mark_dispatched(self, row_id: int) -> None: ...
    async def mark_failed(self, row_id: int, error: str) -> None: ...
    async def has_pending_fan_in_result(
        self, task_id: str, group_id: str, leg_key: int
    ) -> bool: ...


class ScheduledMailRepository(Protocol):
    """Future mail. See models.ScheduledMail."""

    async def create(self, spec: ScheduledMail) -> int:
        """Insert a new scheduled-mail row; return its id."""
        ...

    async def get(self, mail_id: int) -> ScheduledMail | None: ...

    async def find_ready(
        self, now_iso: str, limit: int = 50
    ) -> list[ScheduledMail]:
        """All pending rows with scheduled_for <= now_iso."""
        ...

    async def list_filtered(
        self,
        recipient: str | None = None,
        sender: str | None = None,
        status: str | None = "pending",
        limit: int = 50,
    ) -> list[ScheduledMail]: ...

    async def mark_delivered(
        self,
        mail_id: int,
        delivered_msg_id: int,
        next_scheduled_for: str | None,
        completed: bool,
        no_progress_count: int | None = None,
    ) -> None:
        """One-shot: completed=True, next_scheduled_for=None.
        Recurring with more occurrences: completed=False, next_scheduled_for=iso.
        Recurring past recur_until: completed=True, next_scheduled_for=None.
        ``no_progress_count``: H2 gate value computed for this delivery
        (None = leave the stored counter unchanged; 0 is a deliberate reset).
        """
        ...

    async def mark_cancelled(
        self,
        mail_id: int,
        cancelled_by: str | None = None,
        reason: str | None = None,
    ) -> bool:
        """Returns True if the row was pending and got cancelled."""
        ...

    async def mark_bounced(
        self, mail_id: int, reason: str
    ) -> None: ...

    async def record_delivery_failure(
        self, mail_id: int, error: str, quarantine_after: int
    ) -> bool:
        """Count a Phase −1 delivery attempt that raised; at
        ``quarantine_after`` consecutive failures flip the row to the
        terminal 'quarantined' status (find_ready stops returning it).
        Returns True when this call quarantined the row."""
        ...


class FanInRepository(Protocol):
    """Workflow fan-in barrier: the coordination contract + lineage roster.
    Payload-free — results ride mailbox_messages, not these rows."""

    async def create_group(self, group: FanInGroup) -> str: ...
    async def get(self, group_id: str) -> FanInGroup | None: ...
    async def add_member(self, member: FanInMember) -> None: ...
    async def get_member(self, group_id: str, leg_key: int) -> FanInMember | None: ...
    async def members(self, group_id: str) -> list[FanInMember]: ...
    async def any_open(self) -> bool: ...
    async def find_open(
        self, limit: int = 20, ttl_cutoff: datetime | None = None
    ) -> list[FanInGroup]:
        """The ``limit`` soonest-deadline open groups, PLUS — when
        ``ttl_cutoff`` is given — every open group created before it. The
        union guarantees groups past the global TTL surface even when more
        than ``limit`` younger groups with earlier deadlines fill the
        deadline-sorted page."""
        ...
    async def set_status(
        self, group_id: str, status: str, *, guard: str | None = None
    ) -> bool:
        """With ``guard`` set, flip only when current status == guard (the
        single-winner idiom). Returns True iff a row flipped."""
        ...


class SupervisionRepository(Protocol):
    """Per-agent restart-intensity window (Erlang/OTP MaxR/MaxT)."""

    async def get(self, agent_id: str) -> SupervisionState | None: ...
    async def bump_and_check_intensity(
        self,
        agent_id: str,
        max_restarts: int,
        max_seconds: int,
        now: datetime,
        reason: str | None = None,
        max_total: int | None = None,
        count_total: bool = True,
    ) -> bool:
        """Record one restart; return True iff within ``max_restarts`` per a
        sliding ``max_seconds`` window AND (when ``max_total`` is set) within
        the agent's lifetime total. The window alone cannot bound
        wakeup-paced failure loops — each retry opens a fresh window. The
        over-count is fail-safe (earlier escalation, never a missed bound)."""
        ...
    async def mark_escalated(self, agent_id: str, now: datetime) -> None: ...


class BlobRepository(Protocol):
    """Content-addressed binary blobs (images, documents).

    The DB row carries metadata only — bytes live on disk under
    ``${object_store}/blobs/<sha256>.<ext>``. ``upsert`` is a no-op when
    the blob id already exists, which is the normal case for any retry
    or duplicate upload (content-addressed → same bytes → same id).
    """

    async def upsert(self, blob: Blob) -> None: ...
    async def get(self, blob_id: str) -> Blob | None: ...
    async def exists(self, blob_id: str) -> bool: ...
    async def list_ids(self, blob_ids: list[str]) -> list[Blob]:
        """Bulk-fetch metadata for a list of ids. Used by mailbox tooling
        to translate ``attachments=[...]`` into image content blocks
        without N+1 round-trips."""
        ...


class Repositories(Protocol):
    """Aggregate facade — what business code receives."""

    personas: PersonaRepository
    agents: AgentRepository
    tasks: TaskRepository
    wakeups: WakeupRepository
    mailbox: MailboxRepository
    scheduled_mail: ScheduledMailRepository
    outbox: OutboxRepository
    blobs: BlobRepository
    fan_in: FanInRepository
    supervision: SupervisionRepository
    # Raw connection for queries that span multiple tables or need SQL
    # features beyond a single repo's API surface (cross-table joins,
    # JSON1 path filters, dashboard snapshot aggregates). SQLite-typed by
    # nature — Lyre is single-DB by design (PERSISTENCE_SCHEMA.md §4).
    # Most call sites should prefer the per-repo methods; reach for
    # ``conn`` only when a typed method would be a one-off helper.
    conn: aiosqlite.Connection

    def transaction(self) -> AbstractAsyncContextManager[None]:
        """Async context manager that commits all DAO writes inside the block
        as ONE unit (or rolls them back on error). Mutators called inside
        auto-suppress their own commit — no per-call flag to pass. See the
        concrete implementation for the rationale."""
        ...
