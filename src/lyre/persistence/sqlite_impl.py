"""SQLite implementations of Repository Protocols.

MVP Sprint 0 scope: just enough methods to run `lyre dispatch dispatcher "hello"` end-to-end.
Methods not needed in Sprint 0 are still declared (to satisfy Protocol) but raise
NotImplementedError. They get filled in during Sprint 1/2 as needed.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid as _uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite

from .fs_personas import FilesystemPersonaRepository
from .models import (
    Agent,
    Artifact,
    Blob,
    FanInGroup,
    FanInMember,
    GitContext,
    MailboxMessage,
    MailReaction,
    OutboxRow,
    ScheduledMail,
    Skill,
    Task,
    TaskSpec,
    Wakeup,
)
from .repositories import (
    AgentRepository,
    ArtifactRepository,
    BlobRepository,
    FanInRepository,
    LocalHotRepository,
    MailboxRepository,
    OutboxRepository,
    PersonaRepository,
    ScheduledMailRepository,
    SkillRepository,
    TaskRepository,
    WakeupRepository,
)

if TYPE_CHECKING:
    from ..config import PersonaOverride


# Set for the duration of a `repos.transaction()` block. DAO mutators commit
# through `_commit()`, which is a no-op while this is True — so every write in
# the block joins ONE transaction that the block commits (or rolls back) once.
# A ContextVar (not a connection attribute) so it's isolated per async task and
# never leaks into a concurrent flow that shares the same connection. Any DAO
# write composes atomically inside a transaction with no per-call flag — there
# is nothing to forget, hence no silent-half-commit footgun.
_IN_TRANSACTION: ContextVar[bool] = ContextVar("lyre_in_transaction", default=False)


async def _commit(conn: aiosqlite.Connection) -> None:
    """Commit, unless we're inside a ``repos.transaction()`` block (then the
    block owns the single commit)."""
    if not _IN_TRANSACTION.get():
        await conn.commit()


def _uuid7() -> str:
    """Generate a UUIDv7 string (time-ordered, index-friendly; RFC 9562).

    Falls back to stdlib UUIDv4 if the system clock is unavailable.
    Hand-rolled to avoid an extra dependency in Sprint 0.
    """
    try:
        ms = int(time.time() * 1000)
        rand = os.urandom(10)
        b = bytes([
            (ms >> 40) & 0xFF,
            (ms >> 32) & 0xFF,
            (ms >> 24) & 0xFF,
            (ms >> 16) & 0xFF,
            (ms >> 8) & 0xFF,
            ms & 0xFF,
            (0x70 | (rand[0] & 0x0F)),  # version 7
            rand[1],
            (0x80 | (rand[2] & 0x3F)),  # variant 10
            *rand[3:10],
        ])
        h = b.hex()
        return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
    except Exception:
        return str(_uuid.uuid4())


def _now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _iso(dt: datetime | str | None) -> str | None:
    """Coerce a datetime or ISO string to canonical Lyre ISO 8601 UTC.

    Accepts either form because pydantic may hand us back a datetime, while
    direct SQL inserts often have the string already.
    """
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _derive_title_from_body(body: str) -> str:
    """First non-empty line of body, trimmed and truncated to 140 chars.

    Deterministic, no LLM. Used by insert_message when the sender didn't
    pass an explicit title. Empty-body edge case yields "(empty)" so the
    listing UI always has *something* to show.
    """
    if not body:
        return "(empty)"
    for line in body.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:140]
    return "(empty)"


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def _parse_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return json.loads(value)
    return value


# -----------------------------------------------------------------------
# Persona — the SQLite implementation was deleted in migration 0009.
# Personas are filesystem-only (~/.lyre/personas/<name>/identity.md),
# served via ``lyre.persistence.fs_personas.FilesystemPersonaRepository``,
# which ``SqliteRepositories`` instantiates below.
# -----------------------------------------------------------------------


# -----------------------------------------------------------------------
# Agent — running instance of a persona (Q: persona vs agent orthogonality)
# -----------------------------------------------------------------------
class SqliteAgentRepository:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    async def create(
        self,
        agent_id: str,
        persona_name: str,
        parent_agent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO agents (id, persona_name, parent_agent_id, metadata)
            VALUES (?, ?, ?, ?)
            """,
            (agent_id, persona_name, parent_agent_id, _json(metadata)),
        )
        await _commit(self.conn)

    async def get(self, agent_id: str) -> Agent | None:
        async with self.conn.execute(
            "SELECT * FROM agents WHERE id = ?", (agent_id,)
        ) as cur:
            row = await cur.fetchone()
        return self._row_to_agent(row) if row else None

    async def list_all(self, include_archived: bool = False) -> list[Agent]:
        if include_archived:
            sql = "SELECT * FROM agents ORDER BY created_at"
            params: tuple[Any, ...] = ()
        else:
            sql = "SELECT * FROM agents WHERE status != 'archived' ORDER BY created_at"
            params = ()
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [self._row_to_agent(r) for r in rows]

    async def list_by_persona(
        self, persona_name: str, include_archived: bool = False
    ) -> list[Agent]:
        if include_archived:
            sql = (
                "SELECT * FROM agents WHERE persona_name = ? ORDER BY created_at"
            )
        else:
            sql = (
                "SELECT * FROM agents WHERE persona_name = ? "
                "AND status != 'archived' ORDER BY created_at"
            )
        async with self.conn.execute(sql, (persona_name,)) as cur:
            rows = await cur.fetchall()
        return [self._row_to_agent(r) for r in rows]

    async def archive(self, agent_id: str) -> bool:
        async with self.conn.execute(
            """
            UPDATE agents
            SET status = 'archived',
                archived_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE id = ? AND status != 'archived'
            """,
            (agent_id,),
        ) as cur:
            changed = cur.rowcount
        await _commit(self.conn)
        return bool(changed)

    async def unarchive(self, agent_id: str) -> bool:
        async with self.conn.execute(
            """
            UPDATE agents
            SET status = 'idle',
                archived_at = NULL
            WHERE id = ? AND status = 'archived'
            """,
            (agent_id,),
        ) as cur:
            changed = cur.rowcount
        await _commit(self.conn)
        return bool(changed)

    async def find_reapable_ephemerals(self, limit: int = 20) -> list[Agent]:
        """Ephemeral agents whose work is fully discharged — the reaper's
        reclaim candidates.

        An agent is reapable iff it is marked ephemeral
        (``metadata.supervision.ephemeral``), not already archived, was
        actually spawned (``parent_agent_id`` not NULL — bootstrap singletons
        are never ephemeral), has run at least one task, and has NO in-flight
        task (pending/in_progress/needs_input).

        Requiring ``EXISTS(any task)`` closes the create→first-dispatch race: a
        freshly created ephemeral agent (zero tasks) is NOT reaped before the
        coordinator dispatches its first task. Keying liveness on in-flight
        TASK status (not raw wakeup rows) means an orphan open-wakeup row left
        by a crashed child — whose task is already terminal — does not mask the
        agent as live (mirrors has_active_for_agent's JOIN defense).
        """
        async with self.conn.execute(
            """
            SELECT a.* FROM agents a
            WHERE a.status != 'archived'
              AND a.parent_agent_id IS NOT NULL
              AND json_extract(a.metadata, '$.supervision.ephemeral') = 1
              AND EXISTS (SELECT 1 FROM tasks t WHERE t.agent_id = a.id)
              AND NOT EXISTS (
                SELECT 1 FROM tasks t
                WHERE t.agent_id = a.id
                  AND t.status IN ('pending', 'in_progress', 'needs_input')
              )
            ORDER BY a.created_at
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [self._row_to_agent(r) for r in rows]

    async def exists(self, agent_id: str) -> bool:
        async with self.conn.execute(
            "SELECT 1 FROM agents WHERE id = ?", (agent_id,)
        ) as cur:
            return (await cur.fetchone()) is not None

    async def update_metadata(
        self, agent_id: str, metadata: dict[str, Any]
    ) -> None:
        await self.conn.execute(
            "UPDATE agents SET metadata = ? WHERE id = ?",
            (_json(metadata), agent_id),
        )
        await _commit(self.conn)

    @staticmethod
    def _row_to_agent(row: aiosqlite.Row) -> Agent:
        return Agent(
            id=row["id"],
            persona_name=row["persona_name"],
            status=row["status"],
            parent_agent_id=row["parent_agent_id"],
            created_at=row["created_at"],
            archived_at=row["archived_at"],
            metadata=_parse_json(row["metadata"]),
        )


# -----------------------------------------------------------------------
# Task
# -----------------------------------------------------------------------
class SqliteTaskRepository:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    async def create(self, spec: TaskSpec) -> str:
        task_id = _uuid7()

        # Resolve (agent_id, persona_name): the canonical key is agent_id. We
        # also denormalize persona_name into the row so scheduler/router code
        # that still keys off persona name continues to work until a follow-up
        # migration drops the column.
        agent_id = spec.agent_id
        persona_name = spec.persona_name
        if agent_id:
            async with self.conn.execute(
                "SELECT persona_name FROM agents WHERE id = ?", (agent_id,)
            ) as cur:
                arow = await cur.fetchone()
            if arow is None:
                raise ValueError(
                    f"agent_id {agent_id!r} not found; create the agent first"
                )
            persona_name = arow["persona_name"]
        elif persona_name is None:
            raise ValueError(
                "TaskSpec requires either agent_id or persona_name"
            )
        # If only persona_name was supplied, agent_id stays NULL — back-compat
        # for callers that haven't migrated. dispatch_task and Phase 0 will
        # populate agent_id once their plumbing is updated.

        git_ctx_json = (
            spec.git_context.model_dump_json() if spec.git_context else None
        )
        await self.conn.execute(
            """
            INSERT INTO tasks (
              id, parent_task_id, agent_id, persona_name, goal, acceptance,
              status, lease_duration_s, tier_overrides, deadline, metadata,
              git_context
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                spec.parent_task_id,
                agent_id,
                persona_name,
                spec.goal,
                spec.acceptance,
                spec.lease_duration_s,
                _json(spec.tier_overrides),
                spec.deadline.isoformat() if spec.deadline else None,
                _json(spec.metadata),
                git_ctx_json,
            ),
        )
        await _commit(self.conn)
        return task_id

    async def get(self, task_id: str) -> Task | None:
        async with self.conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ) as cur:
            row = await cur.fetchone()
        return self._row_to_task(row) if row else None

    async def claim_lease(
        self, task_id: str, holder_wakeup_id: str, duration_sec: int
    ) -> bool:
        # Optimistic lock: only succeed if no current lease holder or lease expired.
        # We use UPDATE ... RETURNING (SQLite 3.35+).
        async with self.conn.execute(
            """
            UPDATE tasks
            SET lease_holder = ?,
                lease_until  = strftime('%Y-%m-%dT%H:%M:%fZ','now', ?),
                status       = 'in_progress',
                updated_at   = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE id = ?
              AND (lease_until IS NULL
                   OR lease_until < strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            RETURNING id
            """,
            (holder_wakeup_id, f"+{duration_sec} seconds", task_id),
        ) as cur:
            row = await cur.fetchone()
        await _commit(self.conn)
        return row is not None

    async def renew_lease(
        self, task_id: str, holder_wakeup_id: str, duration_sec: int
    ) -> bool:
        async with self.conn.execute(
            """
            UPDATE tasks
            SET lease_until = strftime('%Y-%m-%dT%H:%M:%fZ','now', ?),
                updated_at  = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE id = ? AND lease_holder = ?
            RETURNING id
            """,
            (f"+{duration_sec} seconds", task_id, holder_wakeup_id),
        ) as cur:
            row = await cur.fetchone()
        await _commit(self.conn)
        return row is not None

    async def release_lease(self, task_id: str, holder_wakeup_id: str) -> None:
        await self.conn.execute(
            """
            UPDATE tasks SET lease_holder = NULL, lease_until = NULL,
                             updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE id = ? AND lease_holder = ?
            """,
            (task_id, holder_wakeup_id),
        )
        await _commit(self.conn)

    async def update_checkpoint(
        self, task_id: str, checkpoint: dict[str, Any], holder_wakeup_id: str
    ) -> None:
        await self.conn.execute(
            """
            UPDATE tasks SET checkpoint = ?,
                             updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE id = ? AND lease_holder = ?
            """,
            (json.dumps(checkpoint), task_id, holder_wakeup_id),
        )
        await _commit(self.conn)

    async def update_status(self, task_id: str, status: str) -> None:
        sql = """
            UPDATE tasks SET status = ?,
                             updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
        """
        if status == "completed":
            sql += ", completed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')"
        sql += " WHERE id = ?"
        await self.conn.execute(sql, (status, task_id))
        await _commit(self.conn)

    # --- Park / resume seam (scheduler-driven barriers) ------------------
    # A task parked in 'needs_input' is invisible to BOTH find_pending
    # (status!='pending') and find_expired_leases (status!='in_progress'),
    # so it is neither dispatched nor lease-recovered while it waits on an
    # external event (e.g. a fan-in barrier). The wake-readiness flag
    # (resume_ready) is decoupled from status so multiple resume sources
    # (barrier predicate, deadline, escalation) can independently raise it
    # while a SINGLE writer — Phase 0.7's resume() — performs the canonical
    # needs_input -> pending transition. This is the kill-safe alternative
    # to a blocking "await children" primitive (deliberately absent — see
    # context.py:317/481): the "wait" is the scheduler polling durable rows.

    async def park(self, task_id: str) -> bool:
        """Park a live task in 'needs_input'. Returns True iff a
        pending/in_progress row flipped (a terminal task is left untouched).

        The lease is NOT cleared here — the scheduler releases it in its
        normal end-of-wakeup teardown. resume_ready is reset to 0 so a fresh
        park always starts not-ready.
        """
        async with self.conn.execute(
            """
            UPDATE tasks
            SET status = 'needs_input', resume_ready = 0,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE id = ? AND status IN ('pending', 'in_progress')
            RETURNING id
            """,
            (task_id,),
        ) as cur:
            row = await cur.fetchone()
        await _commit(self.conn)
        return row is not None

    async def request_resume(self, task_id: str) -> bool:
        """Flag a parked task ready to resume. Idempotent. Returns True iff a
        'needs_input' row was flagged (False if it isn't parked — already
        resumed, cancelled, or never parked). The actual transition is done
        once by resume()."""
        async with self.conn.execute(
            """
            UPDATE tasks SET resume_ready = 1,
                             updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE id = ? AND status = 'needs_input'
            RETURNING id
            """,
            (task_id,),
        ) as cur:
            row = await cur.fetchone()
        await _commit(self.conn)
        return row is not None

    async def find_resumable(self, limit: int = 20) -> list[Task]:
        """Parked tasks whose resume flag is set — Phase 0.7 flips these back
        to 'pending'. Oldest-first so a backlog drains FIFO."""
        async with self.conn.execute(
            """
            SELECT * FROM tasks
            WHERE status = 'needs_input' AND resume_ready = 1
            ORDER BY updated_at
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [self._row_to_task(r) for r in rows]

    async def resume(self, task_id: str) -> bool:
        """The canonical needs_input -> pending transition (Phase 0.7 only).
        Guarded on (status='needs_input' AND resume_ready=1) so two
        concurrent processes can't double-resume — the loser's RETURNING is
        empty. Clears resume_ready. Idempotent across a SIGKILL: a kill after
        the flag is set but before this commits simply re-resumes next tick."""
        async with self.conn.execute(
            """
            UPDATE tasks SET status = 'pending', resume_ready = 0,
                             updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE id = ? AND status = 'needs_input' AND resume_ready = 1
            RETURNING id
            """,
            (task_id,),
        ) as cur:
            row = await cur.fetchone()
        await _commit(self.conn)
        return row is not None

    async def find_pending(self, limit: int = 10) -> list[Task]:
        async with self.conn.execute(
            "SELECT * FROM tasks WHERE status = 'pending' ORDER BY created_at LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [self._row_to_task(r) for r in rows]

    async def find_expired_leases(self, limit: int = 10) -> list[Task]:
        async with self.conn.execute(
            """
            SELECT * FROM tasks
            WHERE status = 'in_progress'
              AND lease_until < strftime('%Y-%m-%dT%H:%M:%fZ','now')
            ORDER BY lease_until
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [self._row_to_task(r) for r in rows]

    async def find_children(self, parent_task_id: str) -> list[Task]:
        """Return all direct child tasks (status doesn't matter)."""
        async with self.conn.execute(
            "SELECT * FROM tasks WHERE parent_task_id = ? ORDER BY created_at",
            (parent_task_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [self._row_to_task(r) for r in rows]

    # Dashboard helpers
    async def find_recent(
        self, limit: int = 50, status_filter: str | None = None
    ) -> list[Task]:
        if status_filter:
            sql = (
                "SELECT * FROM tasks WHERE status = ? "
                "ORDER BY created_at DESC LIMIT ?"
            )
            params: tuple[Any, ...] = (status_filter, limit)
        else:
            sql = "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?"
            params = (limit,)
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [self._row_to_task(r) for r in rows]

    async def search(
        self,
        persona_name: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> list[Task]:
        clauses: list[str] = []
        params: list[Any] = []
        if persona_name is not None:
            clauses.append("persona_name = ?")
            params.append(persona_name)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM tasks {where} ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        async with self.conn.execute(sql, tuple(params)) as cur:
            rows = await cur.fetchall()
        return [self._row_to_task(r) for r in rows]

    async def count_in_progress(self) -> int:
        async with self.conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE status = 'in_progress'"
        ) as cur:
            row = await cur.fetchone()
        return int(row["n"]) if row else 0

    async def count_completed_since(self, since_iso: str) -> int:
        async with self.conn.execute(
            """
            SELECT COUNT(*) AS n FROM tasks
            WHERE status = 'completed' AND completed_at >= ?
            """,
            (since_iso,),
        ) as cur:
            row = await cur.fetchone()
        return int(row["n"]) if row else 0

    async def find_recently_changed(
        self, since_iso: str, limit: int = 100
    ) -> list[Task]:
        async with self.conn.execute(
            """
            SELECT * FROM tasks
            WHERE updated_at >= ?
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (since_iso, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [self._row_to_task(r) for r in rows]

    async def find_active_for_persona(self, persona_name: str) -> list[Task]:
        async with self.conn.execute(
            """
            SELECT * FROM tasks
            WHERE persona_name = ?
              AND status IN ('pending', 'in_progress', 'needs_input')
            ORDER BY created_at
            """,
            (persona_name,),
        ) as cur:
            rows = await cur.fetchall()
        return [self._row_to_task(r) for r in rows]

    @staticmethod
    def _row_to_task(row: aiosqlite.Row) -> Task:
        # agent_id is a new (nullable) column from migration 0003.
        # git_context is a new (nullable) column from migration 0008.
        # SQLite's Row.keys() doesn't expose missing columns; trying to read
        # them raises IndexError. Guard so callers on a pre-0003 schema
        # (notably test fixtures that mocked rows) don't crash.
        try:
            agent_id = row["agent_id"]
        except (KeyError, IndexError):
            agent_id = None
        git_ctx_raw: str | None
        try:
            git_ctx_raw = row["git_context"]
        except (KeyError, IndexError):
            git_ctx_raw = None
        git_ctx = (
            GitContext.model_validate_json(git_ctx_raw)
            if git_ctx_raw else None
        )
        return Task(
            id=row["id"],
            parent_task_id=row["parent_task_id"],
            agent_id=agent_id,
            persona_name=row["persona_name"],
            goal=row["goal"],
            acceptance=row["acceptance"],
            status=row["status"],
            lease_duration_s=row["lease_duration_s"],
            lease_holder=row["lease_holder"],
            checkpoint=_parse_json(row["checkpoint"]),
            tier_overrides=_parse_json(row["tier_overrides"]),
            metadata=_parse_json(row["metadata"]),
            git_context=git_ctx,
        )


# -----------------------------------------------------------------------
# Wakeup
# -----------------------------------------------------------------------
class SqliteWakeupRepository:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    async def start(
        self,
        task_id: str,
        persona_name: str,
        agent_id: str | None = None,
    ) -> str:
        # agent_id is the 2-stage `<persona>/<name>` id from the addressing
        # rework. We persist it so the dashboard's "currently running"
        # detection can match an exact agent — without it, occupancy_pill
        # falls back to persona_name and misses agents whose id has a
        # `/<name>` suffix, rendering them as "queued" while a wakeup of
        # theirs is actively running.
        wakeup_id = _uuid7()
        await self.conn.execute(
            "INSERT INTO wakeups (id, task_id, persona_name, agent_id) "
            "VALUES (?, ?, ?, ?)",
            (wakeup_id, task_id, persona_name, agent_id),
        )
        await _commit(self.conn)
        return wakeup_id

    async def end(
        self,
        wakeup_id: str,
        end_status: str,
        metering: dict[str, Any] | None = None,
        failure_report: dict[str, Any] | None = None,
    ) -> None:
        m = metering or {}
        await self.conn.execute(
            """
            UPDATE wakeups
            SET ended_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                end_status = ?,
                token_input = ?,
                token_output = ?,
                wall_clock_ms = ?,
                tool_call_count = ?,
                provider = ?,
                model = ?,
                failure_report = ?,
                context_peak_tokens = ?,
                compaction_count = COALESCE(?, 0)
            WHERE id = ?
            """,
            (
                end_status,
                m.get("token_input"),
                m.get("token_output"),
                m.get("wall_clock_ms"),
                m.get("tool_call_count"),
                m.get("provider"),
                m.get("model"),
                _json(failure_report),
                m.get("context_peak_tokens"),
                m.get("compaction_count"),
                wakeup_id,
            ),
        )
        await _commit(self.conn)

    async def set_transcript_uri(self, wakeup_id: str, uri: str) -> None:
        await self.conn.execute(
            "UPDATE wakeups SET transcript_uri = ? WHERE id = ?", (uri, wakeup_id)
        )
        await _commit(self.conn)

    # Dashboard helpers
    async def list_recent(self, limit: int = 50) -> list[Wakeup]:
        async with self.conn.execute(
            "SELECT * FROM wakeups ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [self._row_to_wakeup(r) for r in rows]

    async def sum_tokens_since(self, since_iso: str) -> tuple[int, int]:
        async with self.conn.execute(
            """
            SELECT COALESCE(SUM(token_input), 0) AS ti,
                   COALESCE(SUM(token_output), 0) AS to_
            FROM wakeups WHERE started_at >= ?
            """,
            (since_iso,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return (0, 0)
        return (int(row["ti"]), int(row["to_"]))

    async def list_since(
        self, since_iso: str, limit: int = 100
    ) -> list[Wakeup]:
        async with self.conn.execute(
            """
            SELECT * FROM wakeups
            WHERE started_at >= ?
               OR (ended_at IS NOT NULL AND ended_at >= ?)
            ORDER BY COALESCE(ended_at, started_at) DESC
            LIMIT ?
            """,
            (since_iso, since_iso, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [self._row_to_wakeup(r) for r in rows]

    async def list_active(self) -> list[Wakeup]:
        async with self.conn.execute(
            "SELECT * FROM wakeups WHERE ended_at IS NULL ORDER BY started_at DESC"
        ) as cur:
            rows = await cur.fetchall()
        return [self._row_to_wakeup(r) for r in rows]

    async def has_active_for_agent(self, agent_id: str) -> bool:
        """True iff some wakeup of ``agent_id`` is currently running
        (``ended_at IS NULL``) AND its task is still in flight. The
        scheduler uses this to enforce the "agents are sequential
        actors" invariant: a second pending task for the same agent
        must wait until the first one's wakeup finishes, otherwise
        concurrent processes can race on the agent's shared
        filesystem state (scratchpad, notes, ## Auto-summary log).

        The JOIN-and-filter against ``tasks.status`` is deliberate.
        Without it, a wakeup row left with ``ended_at IS NULL`` after
        its task reached a terminal state (e.g. kill-test recovery
        re-ran the task to completion under a new wakeup, but the
        first wakeup's process never finalised its own row) would
        permanently latch this check shut for the whole agent — all
        future pending tasks of that agent get skipped on every
        scheduler tick. Pairing this filter with
        ``close_orphans_for_task`` at recovery time covers both the
        symptom (this check) and the source (orphan rows accumulate).
        """
        async with self.conn.execute(
            """
            SELECT 1
            FROM wakeups w
            JOIN tasks t ON t.id = w.task_id
            WHERE w.agent_id = ?
              AND w.ended_at IS NULL
              AND t.status IN ('pending', 'in_progress', 'needs_input')
            LIMIT 1
            """,
            (agent_id,),
        ) as cur:
            return (await cur.fetchone()) is not None

    async def close_orphans_for_task(
        self, task_id: str, end_status: str = "abandoned"
    ) -> int:
        """Force-close wakeups of ``task_id`` still flagged active.

        The recovery path (``find_expired_leases`` → ``_run_task`` for a
        task whose previous wakeup process died) used to leave the
        dead wakeup's row open forever: the surviving process never
        had a handle to that row, and the end-of-wakeup write that
        would have set ``ended_at`` never ran. The orphaned row then
        poisoned ``has_active_for_agent`` for the rest of the agent's
        lifetime. Sweeping by ``task_id`` at the top of every
        ``_run_task_inline`` invocation closes whatever the prior
        attempt left behind, without touching ``tasks.status`` (that's
        the task's own concern — the scheduler will re-claim or skip
        it based on lease + status in the normal way).
        """
        async with self.conn.execute(
            """
            UPDATE wakeups
            SET ended_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                end_status = ?
            WHERE task_id = ? AND ended_at IS NULL
            """,
            (end_status, task_id),
        ) as cur:
            n = cur.rowcount
        await _commit(self.conn)
        return n

    async def find_terminal_task_orphans(
        self, limit: int = 10
    ) -> list[dict[str, str]]:
        async with self.conn.execute(
            """
            SELECT w.id AS wakeup_id,
                   w.task_id,
                   w.agent_id,
                   t.status AS task_status
            FROM wakeups w JOIN tasks t ON t.id = w.task_id
            WHERE w.ended_at IS NULL
              AND t.status IN ('completed', 'failed', 'cancelled')
            ORDER BY w.started_at
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "wakeup_id": r["wakeup_id"],
                "task_id": r["task_id"],
                "agent_id": r["agent_id"] or "",
                "task_status": r["task_status"],
            }
            for r in rows
        ]

    @staticmethod
    def _row_to_wakeup(r: aiosqlite.Row) -> Wakeup:
        try:
            agent_id = r["agent_id"]
        except (KeyError, IndexError):
            agent_id = None
        # context_peak_tokens / compaction_count came with migration 0006;
        # defensive .get-style access via tuple unpack so older test DBs
        # without the column don't blow up.
        try:
            context_peak = r["context_peak_tokens"]
        except (KeyError, IndexError):
            context_peak = None
        try:
            compaction_count = r["compaction_count"] or 0
        except (KeyError, IndexError):
            compaction_count = 0
        return Wakeup(
            id=r["id"],
            task_id=r["task_id"],
            agent_id=agent_id,
            persona_name=r["persona_name"],
            started_at=r["started_at"],
            ended_at=r["ended_at"],
            end_status=r["end_status"],
            token_input=r["token_input"],
            token_output=r["token_output"],
            wall_clock_ms=r["wall_clock_ms"],
            tool_call_count=r["tool_call_count"],
            provider=r["provider"],
            model=r["model"],
            transcript_uri=r["transcript_uri"],
            context_peak_tokens=context_peak,
            compaction_count=compaction_count,
        )


# -----------------------------------------------------------------------
# Mailbox
# -----------------------------------------------------------------------
class SqliteMailboxRepository:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    async def ensure_mailbox(self, recipient: str) -> None:
        await self.conn.execute(
            "INSERT OR IGNORE INTO mailboxes (recipient) VALUES (?)",
            (recipient,),
        )
        await _commit(self.conn)

    # --- Read flow (per-message read state) ---------------------------

    # CASE expression to rank urgency for ORDER BY. Higher urgency = earlier.
    _URGENCY_RANK_SQL = (
        "CASE urgency "
        "WHEN 'blocker' THEN 4 "
        "WHEN 'high'    THEN 3 "
        "WHEN 'normal'  THEN 2 "
        "WHEN 'low'     THEN 1 "
        "ELSE 0 END"
    )

    _URGENCY_ORDER = {"low": 1, "normal": 2, "high": 3, "blocker": 4}

    async def read_unread(
        self,
        recipient: str,
        *,
        min_urgency: str | None = None,
        limit: int = 50,
    ) -> list[MailboxMessage]:
        clauses = ["recipient = ?", "read_at IS NULL"]
        params: list[Any] = [recipient]
        if min_urgency:
            rank = self._URGENCY_ORDER.get(min_urgency)
            if rank is None:
                raise ValueError(f"unknown urgency '{min_urgency}'")
            allowed = [u for u, r in self._URGENCY_ORDER.items() if r >= rank]
            placeholders = ",".join("?" * len(allowed))
            clauses.append(f"urgency IN ({placeholders})")
            params.extend(allowed)
        params.append(limit)
        sql = (
            f"SELECT * FROM mailbox_messages "
            f"WHERE {' AND '.join(clauses)} "
            f"ORDER BY {self._URGENCY_RANK_SQL} DESC, id ASC "
            f"LIMIT ?"
        )
        async with self.conn.execute(sql, tuple(params)) as cur:
            rows = await cur.fetchall()
        return [self._row_to_msg(r) for r in rows]

    async def read_all_by_recipient(
        self,
        recipient: str,
        *,
        limit: int = 50,
    ) -> list[MailboxMessage]:
        async with self.conn.execute(
            """
            SELECT * FROM mailbox_messages
            WHERE recipient = ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (recipient, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [self._row_to_msg(r) for r in rows]

    async def mark_messages_read(
        self, recipient: str, msg_ids: list[int]
    ) -> None:
        if not msg_ids:
            return
        placeholders = ",".join("?" * len(msg_ids))
        await self.conn.execute(
            f"""
            UPDATE mailbox_messages
            SET read_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE recipient = ? AND id IN ({placeholders})
              AND read_at IS NULL
            """,
            (recipient, *msg_ids),
        )
        await _commit(self.conn)

    async def get_max_msg_id(self, recipient: str) -> int:
        async with self.conn.execute(
            "SELECT MAX(id) AS m FROM mailbox_messages WHERE recipient = ?",
            (recipient,),
        ) as cur:
            row = await cur.fetchone()
        return int(row["m"]) if (row and row["m"] is not None) else 0

    async def list_sent_by(
        self,
        sender: str,
        *,
        recipient: str | None = None,
        limit: int = 50,
    ) -> list[MailboxMessage]:
        # Newest-first: the agent calls this to recall recent commitments,
        # most recent is usually most relevant.
        clauses = ["sender = ?"]
        params: list[Any] = [sender]
        if recipient is not None:
            clauses.append("recipient = ?")
            params.append(recipient)
        params.append(limit)
        sql = (
            f"SELECT * FROM mailbox_messages "
            f"WHERE {' AND '.join(clauses)} "
            f"ORDER BY id DESC "
            f"LIMIT ?"
        )
        async with self.conn.execute(sql, tuple(params)) as cur:
            rows = await cur.fetchall()
        return [self._row_to_msg(r) for r in rows]

    async def count_unread(
        self, recipient: str, *, min_urgency: str | None = None
    ) -> int:
        clauses = ["recipient = ?", "read_at IS NULL"]
        params: list[Any] = [recipient]
        if min_urgency:
            rank = self._URGENCY_ORDER.get(min_urgency)
            if rank is None:
                raise ValueError(f"unknown urgency '{min_urgency}'")
            allowed = [u for u, r in self._URGENCY_ORDER.items() if r >= rank]
            placeholders = ",".join("?" * len(allowed))
            clauses.append(f"urgency IN ({placeholders})")
            params.extend(allowed)
        sql = (
            f"SELECT COUNT(*) AS n FROM mailbox_messages "
            f"WHERE {' AND '.join(clauses)}"
        )
        async with self.conn.execute(sql, tuple(params)) as cur:
            row = await cur.fetchone()
        return int(row["n"]) if row else 0

    # --- System-side read helpers (NOT agent-facing; no read_at side effect)

    async def read_messages(
        self, recipient: str, since_id: int = 0, limit: int = 100
    ) -> list[MailboxMessage]:
        async with self.conn.execute(
            """
            SELECT * FROM mailbox_messages
            WHERE recipient = ? AND id > ?
            ORDER BY id
            LIMIT ?
            """,
            (recipient, since_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [self._row_to_msg(r) for r in rows]

    async def read_blockers(
        self, recipient: str, since_id: int = 0
    ) -> list[MailboxMessage]:
        async with self.conn.execute(
            """
            SELECT * FROM mailbox_messages
            WHERE recipient = ? AND id > ? AND urgency = 'blocker'
            ORDER BY id
            """,
            (recipient, since_id),
        ) as cur:
            rows = await cur.fetchall()
        return [self._row_to_msg(r) for r in rows]

    # Dashboard helpers
    async def read_messages_paged(
        self,
        recipient: str,
        before_id: int | None = None,
        limit: int = 50,
        min_urgency: str | None = None,
    ) -> list[MailboxMessage]:
        _URGENCY_ORDER = {"low": 1, "normal": 2, "high": 3, "blocker": 4}
        clauses = ["recipient = ?"]
        params: list[Any] = [recipient]
        if before_id is not None:
            clauses.append("id < ?")
            params.append(before_id)
        if min_urgency:
            rank = _URGENCY_ORDER.get(min_urgency)
            if rank is None:
                raise ValueError(f"unknown urgency '{min_urgency}'")
            allowed = [
                u for u, r in _URGENCY_ORDER.items() if r >= rank
            ]
            placeholders = ",".join("?" * len(allowed))
            clauses.append(f"urgency IN ({placeholders})")
            params.extend(allowed)
        params.append(limit)
        sql = (
            f"SELECT * FROM mailbox_messages WHERE {' AND '.join(clauses)} "
            f"ORDER BY id DESC LIMIT ?"
        )
        async with self.conn.execute(sql, tuple(params)) as cur:
            rows = await cur.fetchall()
        return [self._row_to_msg(r) for r in rows]

    async def read_recent_for_audit(
        self, since_iso: str, limit: int = 200
    ) -> list[MailboxMessage]:
        async with self.conn.execute(
            """
            SELECT * FROM mailbox_messages
            WHERE delivered_at >= ?
            ORDER BY delivered_at DESC, id DESC
            LIMIT ?
            """,
            (since_iso, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [self._row_to_msg(r) for r in rows]

    async def count_unread_blockers(self, recipient: str) -> int:
        async with self.conn.execute(
            """
            SELECT COUNT(*) AS n FROM mailbox_messages
            WHERE recipient = ?
              AND urgency = 'blocker'
              AND read_at IS NULL
            """,
            (recipient,),
        ) as cur:
            row = await cur.fetchone()
        return int(row["n"]) if row else 0

    async def insert_message(self, msg: MailboxMessage) -> int:
        await self.ensure_mailbox(msg.recipient)
        # Derive title from body's first non-empty line if not provided.
        # 140 char cap matches the schema convention. Storing the derived
        # value (vs computing at read time) keeps listings deterministic
        # and cache-friendly.
        title = msg.title or _derive_title_from_body(msg.body)
        async with self.conn.execute(
            """
            INSERT INTO mailbox_messages (
              recipient, external_id, sender, urgency, title, body,
              task_id, parent_msg_id, broadcast_id, recipients_all,
              metadata, attachments
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (recipient, external_id) DO NOTHING
            RETURNING id
            """,
            (
                msg.recipient,
                msg.external_id,
                msg.sender,
                msg.urgency,
                title,
                msg.body,
                msg.task_id,
                msg.parent_msg_id,
                msg.broadcast_id,
                _json(msg.recipients_all),
                _json(msg.metadata),
                _json(msg.attachments) if msg.attachments else None,
            ),
        ) as cur:
            row = await cur.fetchone()
        await _commit(self.conn)
        return row["id"] if row else -1

    async def count_fan_in_results(self, recipient: str, group_id: str) -> int:
        """Distinct fan-in legs whose result-mail has been DELIVERED to
        ``recipient`` for ``group_id``. This is the barrier predicate input
        (Phase 0.5): it counts mailbox rows — the delivery event the
        coordinator actually depends on — not completed child tasks, so it
        can never trip while a result is still an undispatched outbox row.
        COUNT(DISTINCT leg_key) collapses an idempotent redelivery to one.
        """
        async with self.conn.execute(
            """
            SELECT COUNT(DISTINCT json_extract(metadata, '$.fan_in.leg_key')) AS n
            FROM mailbox_messages
            WHERE recipient = ?
              AND json_extract(metadata, '$.fan_in.group_id') = ?
            """,
            (recipient, group_id),
        ) as cur:
            row = await cur.fetchone()
        return int(row["n"]) if row and row["n"] is not None else 0

    async def get_message(self, msg_id: int) -> MailboxMessage | None:
        async with self.conn.execute(
            "SELECT * FROM mailbox_messages WHERE id = ?", (msg_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        msg = self._row_to_msg(row)
        msg.reactions = await self.list_reactions(msg_id)
        return msg

    async def add_reaction(
        self, msg_id: int, reactor: str, kind: str,
    ) -> bool:
        """Idempotent insert into mail_reactions. `INSERT OR IGNORE`
        relies on the (msg_id, reactor, kind) PK to drop duplicates
        cleanly, and `changes()` distinguishes "new row" from "already
        existed" without an extra round-trip."""
        await self.conn.execute(
            """
            INSERT OR IGNORE INTO mail_reactions (msg_id, reactor, kind)
            VALUES (?, ?, ?)
            """,
            (msg_id, reactor, kind),
        )
        async with self.conn.execute("SELECT changes()") as cur:
            row = await cur.fetchone()
        await _commit(self.conn)
        return bool(row and row[0])

    async def list_reactions(self, msg_id: int) -> list[MailReaction]:
        async with self.conn.execute(
            """
            SELECT msg_id, reactor, kind, created_at
            FROM mail_reactions
            WHERE msg_id = ?
            ORDER BY created_at ASC, reactor ASC
            """,
            (msg_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            MailReaction(
                msg_id=r["msg_id"],
                reactor=r["reactor"],
                kind=r["kind"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    async def set_channel_external_id(
        self, msg_id: int, channel_name: str, external_id: str,
    ) -> None:
        # Two nested json_set calls — outer ensures `channels` exists as
        # a JSON object, inner writes the message_id under the named
        # channel's sub-tree. Other channels' metadata is preserved.
        # Path syntax: $.channels.<name>.message_id; channel_name is
        # restricted to [a-z0-9_] by the Protocol convention so it's
        # safe to interpolate into the path string.
        if not channel_name.replace("_", "").isalnum():
            raise ValueError(
                f"channel name {channel_name!r} contains characters "
                "that would corrupt the JSON path"
            )
        path = f"$.channels.{channel_name}.message_id"
        ensure_channels = (
            "json_set(COALESCE(metadata, '{}'), '$.channels', "
            "COALESCE(json_extract(metadata, '$.channels'), json('{}')))"
        )
        ensure_named = (
            f"json_set({ensure_channels}, '$.channels.{channel_name}', "
            f"COALESCE(json_extract(metadata, '$.channels.{channel_name}'), "
            "json('{}')))"
        )
        await self.conn.execute(
            f"UPDATE mailbox_messages "
            f"SET metadata = json_set({ensure_named}, '{path}', ?) "
            f"WHERE id = ?",
            (external_id, msg_id),
        )
        await _commit(self.conn)

    async def find_by_channel_external_id(
        self, channel_name: str, external_id: str,
    ) -> MailboxMessage | None:
        # JSON1 path lookup; mail volumes per user are small, table scan
        # is fine. If it ever gets hot, add an expression index on
        # ``json_extract(metadata, '$.channels.<name>.message_id')``.
        async with self.conn.execute(
            "SELECT * FROM mailbox_messages "
            "WHERE json_extract(metadata, ?) = ? "
            "LIMIT 1",
            (f"$.channels.{channel_name}.message_id", external_id),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return self._row_to_msg(row)

    async def find_id_by_external_id(
        self, recipient: str, external_id: str,
    ) -> int | None:
        async with self.conn.execute(
            "SELECT id FROM mailbox_messages "
            "WHERE recipient = ? AND external_id = ?",
            (recipient, external_id),
        ) as cur:
            row = await cur.fetchone()
        return int(row["id"]) if row is not None else None

    async def list_pending_channel_publish(
        self, *, recipient: str, channel_name: str, limit: int = 500,
    ) -> list[MailboxMessage]:
        prefix = f"channel:{channel_name}:owner-mail:"
        async with self.conn.execute(
            """
            SELECT m.* FROM mailbox_messages m
            WHERE m.recipient = ?
              AND NOT EXISTS (
                SELECT 1 FROM outbox o
                 WHERE o.kind = 'channel_publish'
                   AND o.external_id = ? || m.id
              )
            ORDER BY m.id
            LIMIT ?
            """,
            (recipient, prefix, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [self._row_to_msg(r) for r in rows]

    async def get_last_auto_triggered_id(self, recipient: str) -> int:
        await self.ensure_mailbox(recipient)
        async with self.conn.execute(
            "SELECT metadata FROM mailboxes WHERE recipient = ?", (recipient,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return 0
        meta = _parse_json(row["metadata"]) or {}
        return int(meta.get("last_auto_triggered_msg_id") or 0)

    async def set_last_auto_triggered_id(
        self, recipient: str, msg_id: int
    ) -> None:
        await self.ensure_mailbox(recipient)
        # Use SQLite's json_set to merge; monotonic (don't go backward).
        await self.conn.execute(
            """
            UPDATE mailboxes
            SET metadata = json_set(
                COALESCE(metadata, '{}'),
                '$.last_auto_triggered_msg_id',
                MAX(?, COALESCE(
                    json_extract(metadata, '$.last_auto_triggered_msg_id'), 0
                ))
            )
            WHERE recipient = ?
            """,
            (msg_id, recipient),
        )
        await _commit(self.conn)

    @staticmethod
    def _row_to_msg(row: aiosqlite.Row) -> MailboxMessage:
        keys = set(row.keys())
        return MailboxMessage(
            id=row["id"],
            recipient=row["recipient"],
            external_id=row["external_id"],
            sender=row["sender"],
            urgency=row["urgency"],
            title=row["title"] if "title" in keys else None,
            body=row["body"],
            task_id=row["task_id"],
            parent_msg_id=row["parent_msg_id"],
            broadcast_id=row["broadcast_id"] if "broadcast_id" in keys else None,
            recipients_all=(
                _parse_json(row["recipients_all"])
                if "recipients_all" in keys else None
            ),
            metadata=_parse_json(row["metadata"]),
            # `delivered_at` is the canonical timestamp for "when mail
            # appeared in the recipient's inbox" — the dashboard sorts
            # the activity timeline by it. Forgetting it here meant
            # MailboxMessage.delivered_at was always None on read-back,
            # which made every mail event lex-sort to the start of the
            # timeline (empty string < any ISO timestamp).
            delivered_at=row["delivered_at"] if "delivered_at" in keys else None,
            read_at=row["read_at"] if "read_at" in keys else None,
            attachments=(
                _parse_json(row["attachments"])
                if "attachments" in keys else None
            ),
        )


# -----------------------------------------------------------------------
# Scheduled mail (future / recurring) — see migration 0004
# -----------------------------------------------------------------------
class SqliteScheduledMailRepository:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    async def create(self, spec: ScheduledMail) -> int:
        title = spec.title or _derive_title_from_body(spec.body)
        async with self.conn.execute(
            """
            INSERT INTO scheduled_mail (
              recipient, sender, urgency, title, body, task_id,
              parent_msg_id, metadata, scheduled_for,
              recur_kind, recur_value, recur_until,
              created_by_agent, created_by_task, status
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending')
            RETURNING id
            """,
            (
                spec.recipient,
                spec.sender,
                spec.urgency,
                title,
                spec.body,
                spec.task_id,
                spec.parent_msg_id,
                _json(spec.metadata),
                _iso(spec.scheduled_for),
                spec.recur_kind,
                spec.recur_value,
                _iso(spec.recur_until) if spec.recur_until else None,
                spec.created_by_agent,
                spec.created_by_task,
            ),
        ) as cur:
            row = await cur.fetchone()
        await _commit(self.conn)
        # INSERT ... RETURNING always yields a row when no constraint
        # violation occurs (which would raise instead of returning None).
        assert row is not None  # noqa: S101 — narrows for mypy
        return int(row["id"])

    async def get(self, mail_id: int) -> ScheduledMail | None:
        async with self.conn.execute(
            "SELECT * FROM scheduled_mail WHERE id = ?", (mail_id,)
        ) as cur:
            row = await cur.fetchone()
        return self._row_to_mail(row) if row else None

    async def find_ready(
        self, now_iso: str, limit: int = 50
    ) -> list[ScheduledMail]:
        async with self.conn.execute(
            """
            SELECT * FROM scheduled_mail
            WHERE status='pending' AND scheduled_for <= ?
            ORDER BY scheduled_for ASC
            LIMIT ?
            """,
            (now_iso, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [self._row_to_mail(r) for r in rows]

    async def list_filtered(
        self,
        recipient: str | None = None,
        sender: str | None = None,
        status: str | None = "pending",
        limit: int = 50,
    ) -> list[ScheduledMail]:
        clauses: list[str] = []
        params: list[Any] = []
        if recipient is not None:
            clauses.append("recipient = ?")
            params.append(recipient)
        if sender is not None:
            clauses.append("sender = ?")
            params.append(sender)
        if status is not None and status != "all":
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT * FROM scheduled_mail {where} "
            f"ORDER BY scheduled_for ASC LIMIT ?"
        )
        params.append(limit)
        async with self.conn.execute(sql, tuple(params)) as cur:
            rows = await cur.fetchall()
        return [self._row_to_mail(r) for r in rows]

    async def mark_delivered(
        self,
        mail_id: int,
        delivered_msg_id: int,
        next_scheduled_for: str | None,
        completed: bool,
    ) -> None:
        if completed:
            await self.conn.execute(
                """
                UPDATE scheduled_mail
                SET status='completed',
                    last_delivery_id = ?,
                    last_delivered_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                    occurrence_count = occurrence_count + 1
                WHERE id = ?
                """,
                (delivered_msg_id, mail_id),
            )
        else:
            await self.conn.execute(
                """
                UPDATE scheduled_mail
                SET last_delivery_id = ?,
                    last_delivered_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                    occurrence_count = occurrence_count + 1,
                    scheduled_for = ?
                WHERE id = ?
                """,
                (delivered_msg_id, next_scheduled_for, mail_id),
            )
        await _commit(self.conn)

    async def mark_cancelled(
        self,
        mail_id: int,
        cancelled_by: str | None = None,
        reason: str | None = None,
    ) -> bool:
        # `reason` is stored in metadata.cancel_reason because the
        # schema's bounce_reason is reserved for delivery-failure context.
        # Cheaper than another column for the rare case agents pass it.
        meta_patch = (
            ", metadata = json_set(COALESCE(metadata, '{}'), "
            "'$.cancel_reason', ?)"
            if reason else ""
        )
        params: list[Any] = [cancelled_by]
        if reason:
            params.append(reason)
        params.append(mail_id)
        async with self.conn.execute(
            f"""
            UPDATE scheduled_mail
            SET status='cancelled',
                cancelled_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                cancelled_by = ?
                {meta_patch}
            WHERE id = ? AND status='pending'
            """,
            tuple(params),
        ) as cur:
            changed = cur.rowcount
        await _commit(self.conn)
        return bool(changed)

    async def mark_bounced(self, mail_id: int, reason: str) -> None:
        await self.conn.execute(
            """
            UPDATE scheduled_mail
            SET status='bounced', bounce_reason = ?,
                last_delivered_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE id = ?
            """,
            (reason, mail_id),
        )
        await _commit(self.conn)

    @staticmethod
    def _row_to_mail(row: aiosqlite.Row) -> ScheduledMail:
        keys = set(row.keys())
        return ScheduledMail(
            id=row["id"],
            recipient=row["recipient"],
            sender=row["sender"],
            urgency=row["urgency"],
            title=row["title"] if "title" in keys else None,
            body=row["body"],
            task_id=row["task_id"],
            parent_msg_id=row["parent_msg_id"],
            metadata=_parse_json(row["metadata"]),
            scheduled_for=row["scheduled_for"],
            recur_kind=row["recur_kind"],
            recur_value=row["recur_value"],
            recur_until=row["recur_until"],
            occurrence_count=row["occurrence_count"],
            created_at=row["created_at"],
            created_by_agent=row["created_by_agent"],
            created_by_task=row["created_by_task"],
            status=row["status"],
            last_delivery_id=row["last_delivery_id"],
            last_delivered_at=row["last_delivered_at"],
            cancelled_at=row["cancelled_at"],
            cancelled_by=row["cancelled_by"],
            bounce_reason=row["bounce_reason"],
        )


# -----------------------------------------------------------------------
# Outbox (Sprint 0 stub: dispatch is sync — directly write to mailbox)
# -----------------------------------------------------------------------
class SqliteOutboxRepository:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    async def enqueue(self, rows: list[OutboxRow]) -> None:
        for r in rows:
            await self.conn.execute(
                """
                INSERT INTO outbox (
                  task_id, wakeup_id, kind, payload, external_id
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (kind, external_id) DO NOTHING
                """,
                (r.task_id, r.wakeup_id, r.kind, _json(r.payload), r.external_id),
            )
        await _commit(self.conn)

    async def dequeue_batch(self, limit: int = 100) -> list[OutboxRow]:
        async with self.conn.execute(
            """
            SELECT * FROM outbox
            WHERE dispatched_at IS NULL
            ORDER BY created_at
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            OutboxRow(
                id=r["id"],
                task_id=r["task_id"],
                wakeup_id=r["wakeup_id"],
                kind=r["kind"],
                payload=_parse_json(r["payload"]),
                external_id=r["external_id"],
                dispatch_attempts=r["dispatch_attempts"],
            )
            for r in rows
        ]

    async def mark_dispatched(self, row_id: int) -> None:
        await self.conn.execute(
            """
            UPDATE outbox SET dispatched_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE id = ?
            """,
            (row_id,),
        )
        await _commit(self.conn)

    async def mark_failed(self, row_id: int, error: str) -> None:
        await self.conn.execute(
            """
            UPDATE outbox
            SET dispatch_attempts = dispatch_attempts + 1, last_error = ?
            WHERE id = ?
            """,
            (error, row_id),
        )
        await _commit(self.conn)


# -----------------------------------------------------------------------
# Local-hot
# -----------------------------------------------------------------------
class SqliteLocalHotRepository:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    async def put(self, task_id: str, key: str, value: Any) -> None:
        await self.conn.execute(
            """
            INSERT INTO local_hot (task_id, key, value)
            VALUES (?, ?, ?)
            ON CONFLICT(task_id, key) DO UPDATE SET
              value = excluded.value,
              updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            """,
            (task_id, key, _json(value)),
        )
        await _commit(self.conn)

    async def get(self, task_id: str, key: str) -> Any | None:
        async with self.conn.execute(
            "SELECT value FROM local_hot WHERE task_id = ? AND key = ?",
            (task_id, key),
        ) as cur:
            row = await cur.fetchone()
        return _parse_json(row["value"]) if row else None

    async def clear_task(self, task_id: str) -> None:
        await self.conn.execute(
            "DELETE FROM local_hot WHERE task_id = ?", (task_id,)
        )
        await _commit(self.conn)


# -----------------------------------------------------------------------
# Stubs for Sprint 0: declared to satisfy Protocol, raise NotImplementedError
# -----------------------------------------------------------------------
class SqliteSkillRepository:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    async def get_by_name(self, name: str) -> Skill | None:
        return None

    async def list_active(
        self, scope: str | None = None, status: str = "approved"
    ) -> list[Skill]:
        return []

    async def propose(
        self,
        name: str,
        frontmatter: dict[str, Any],
        body: str,
        source_task_id: str,
        scope: str | None = None,
    ) -> str:
        raise NotImplementedError("Sprint 0 stub")

    async def approve(
        self,
        skill_id: str,
        reviewer: str,
        status: str,
        comment: str | None = None,
    ) -> None:
        raise NotImplementedError("Sprint 0 stub")


class SqliteArtifactRepository:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    async def insert(
        self,
        task_id: str,
        wakeup_id: str,
        kind: str,
        content_hash: str,
        blob_uri: str,
        size_bytes: int | None = None,
    ) -> str:
        artifact_id = _uuid7()
        await self.conn.execute(
            """
            INSERT INTO artifacts (
              id, task_id, wakeup_id, kind, content_hash, blob_uri, size_bytes
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (content_hash) DO NOTHING
            """,
            (artifact_id, task_id, wakeup_id, kind, content_hash, blob_uri, size_bytes),
        )
        await _commit(self.conn)
        return artifact_id

    async def get_by_hash(self, content_hash: str) -> Artifact | None:
        return None

    async def find_by_task(self, task_id: str) -> list[Artifact]:
        return []


class SqliteBlobRepository:
    """Metadata for content-addressed blobs (images, documents).

    Bytes live on disk — this row carries only ``id`` (sha256 hex),
    ``media_type``, ``size_bytes``, optional ``filename`` and
    ``source``. ``upsert`` collapses to a no-op on id conflict so
    re-uploading identical bytes is free.
    """

    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    async def upsert(self, blob: Blob) -> None:
        await self.conn.execute(
            """
            INSERT INTO blobs (id, media_type, size_bytes, filename, source)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (id) DO NOTHING
            """,
            (
                blob.id, blob.media_type, blob.size_bytes,
                blob.filename, blob.source,
            ),
        )
        await _commit(self.conn)

    async def get(self, blob_id: str) -> Blob | None:
        async with self.conn.execute(
            "SELECT * FROM blobs WHERE id = ?", (blob_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return self._row_to_blob(row)

    async def exists(self, blob_id: str) -> bool:
        async with self.conn.execute(
            "SELECT 1 FROM blobs WHERE id = ? LIMIT 1", (blob_id,)
        ) as cur:
            return (await cur.fetchone()) is not None

    async def list_ids(self, blob_ids: list[str]) -> list[Blob]:
        if not blob_ids:
            return []
        # Parameterized IN-list; SQLite handles up to 999 params per
        # query which is well past any realistic mail attachment count.
        placeholders = ",".join("?" * len(blob_ids))
        async with self.conn.execute(
            f"SELECT * FROM blobs WHERE id IN ({placeholders})",
            tuple(blob_ids),
        ) as cur:
            rows = await cur.fetchall()
        by_id = {r["id"]: self._row_to_blob(r) for r in rows}
        # Preserve caller's ordering — the mail-detail view renders
        # attachments in the order they were attached.
        return [by_id[bid] for bid in blob_ids if bid in by_id]

    @staticmethod
    def _row_to_blob(r: aiosqlite.Row) -> Blob:
        return Blob(
            id=r["id"],
            media_type=r["media_type"],
            size_bytes=r["size_bytes"],
            filename=r["filename"],
            source=r["source"],
            created_at=r["created_at"],
        )


# -----------------------------------------------------------------------
# Aggregate facade
# -----------------------------------------------------------------------
# -----------------------------------------------------------------------
# Fan-in barrier (workflow orchestration)
# -----------------------------------------------------------------------
class SqliteFanInRepository:
    """Workflow fan-in barrier rows: the coordination contract
    (``fan_in_groups``) + the per-slot lineage roster (``fan_in_members``).

    Payload-free by design — results ride mailbox_messages, never these
    rows. See docs/design/WORKFLOW_ORCHESTRATION.md.
    """

    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    async def create_group(self, group: FanInGroup) -> str:
        await self.conn.execute(
            """
            INSERT INTO fan_in_groups (
              id, coordinator_agent_id, parent_task_id, expect_replies,
              quorum, result_schema, budget_tokens, deadline, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open')
            """,
            (
                group.id,
                group.coordinator_agent_id,
                group.parent_task_id,
                group.expect_replies,
                group.quorum,
                _json(group.result_schema),
                group.budget_tokens,
                group.deadline.isoformat(),
            ),
        )
        await _commit(self.conn)
        return group.id

    async def get(self, group_id: str) -> FanInGroup | None:
        async with self.conn.execute(
            "SELECT * FROM fan_in_groups WHERE id = ?", (group_id,)
        ) as cur:
            row = await cur.fetchone()
        return self._row_to_group(row) if row else None

    async def add_member(self, member: FanInMember) -> None:
        await self.conn.execute(
            """
            INSERT INTO fan_in_members (group_id, leg_key, child_task_id, child_agent_id)
            VALUES (?, ?, ?, ?)
            """,
            (member.group_id, member.leg_key, member.child_task_id, member.child_agent_id),
        )
        await _commit(self.conn)

    async def get_member(self, group_id: str, leg_key: int) -> FanInMember | None:
        async with self.conn.execute(
            "SELECT * FROM fan_in_members WHERE group_id = ? AND leg_key = ?",
            (group_id, leg_key),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return FanInMember(
            group_id=row["group_id"],
            leg_key=row["leg_key"],
            child_task_id=row["child_task_id"],
            child_agent_id=row["child_agent_id"],
        )

    async def members(self, group_id: str) -> list[FanInMember]:
        async with self.conn.execute(
            "SELECT * FROM fan_in_members WHERE group_id = ? ORDER BY leg_key",
            (group_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            FanInMember(
                group_id=r["group_id"],
                leg_key=r["leg_key"],
                child_task_id=r["child_task_id"],
                child_agent_id=r["child_agent_id"],
            )
            for r in rows
        ]

    async def any_open(self) -> bool:
        """Fast Phase 0.5 early-return probe."""
        async with self.conn.execute(
            "SELECT 1 FROM fan_in_groups WHERE status = 'open' LIMIT 1"
        ) as cur:
            return (await cur.fetchone()) is not None

    async def find_open(self, limit: int = 20) -> list[FanInGroup]:
        async with self.conn.execute(
            "SELECT * FROM fan_in_groups WHERE status = 'open' ORDER BY deadline LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [self._row_to_group(r) for r in rows]

    async def set_status(
        self,
        group_id: str,
        status: str,
        *,
        guard: str | None = None,
    ) -> bool:
        """Transition a group's status. With ``guard`` set, only flips when
        the current status equals it — the single-winner idiom (claim_lease
        shape) so two scheduler processes can't both resolve one group.
        Returns True iff a row flipped. resolved_at is stamped on any
        non-open status."""
        resolved = ", resolved_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')"
        tail = " AND status = ?" if guard is not None else ""
        params: tuple[Any, ...] = (
            (status, group_id, guard) if guard is not None else (status, group_id)
        )
        async with self.conn.execute(
            f"UPDATE fan_in_groups SET status = ?{resolved} "
            f"WHERE id = ?{tail} RETURNING id",
            params,
        ) as cur:
            row = await cur.fetchone()
        await _commit(self.conn)
        return row is not None

    @staticmethod
    def _row_to_group(row: aiosqlite.Row) -> FanInGroup:
        return FanInGroup(
            id=row["id"],
            coordinator_agent_id=row["coordinator_agent_id"],
            parent_task_id=row["parent_task_id"],
            expect_replies=row["expect_replies"],
            quorum=row["quorum"],
            result_schema=_parse_json(row["result_schema"]) or {},
            budget_tokens=row["budget_tokens"],
            dry_round=row["dry_round"],
            deadline=row["deadline"],  # pydantic coerces the ISO string
            status=row["status"],
        )


class SqliteRepositories:
    """Bundle all SQLite repositories sharing one aiosqlite connection.

    Attributes are declared with their Protocol types (``PersonaRepository``
    etc.) so callers that accept the ``Repositories`` Protocol see the
    abstract surface — mypy treats Protocol attribute types as invariant,
    and without these annotations every callsite would flag a spurious
    "incompatible type" against the concrete ``SqliteFooRepository``.
    """

    personas: PersonaRepository
    agents: AgentRepository
    tasks: TaskRepository
    wakeups: WakeupRepository
    mailbox: MailboxRepository
    scheduled_mail: ScheduledMailRepository
    outbox: OutboxRepository
    skills: SkillRepository
    artifacts: ArtifactRepository
    local_hot: LocalHotRepository
    blobs: BlobRepository
    fan_in: FanInRepository

    def __init__(
        self,
        conn: aiosqlite.Connection,
        *,
        personas_dir: Path | None = None,
        persona_overrides: dict[str, PersonaOverride] | None = None,
    ):
        """``personas_dir`` is the filesystem root for persona definitions
        (``~/.lyre/personas/`` in production; a tmp dir in most tests).
        When omitted, a fresh writable tempdir is allocated so tests that
        construct ``SqliteRepositories(conn)`` without thinking about
        personas can still ``upsert`` / ``list_active`` against it.

        ``persona_overrides`` overlays single fields from ``config.toml
        [personas.<name>]`` on every persona read (model_preference,
        allowed_lyre_tools). Identity.md remains the SSOT for everything
        else.
        """
        self.conn = conn
        # No personas_dir → allocate one. ``mkdtemp`` returns a writable
        # path so ``upsert`` works; the dir is small and gets reaped with
        # /tmp eventually. Production paths (main.py, onboard.py) always
        # pass an explicit ``personas_dir``.
        if personas_dir is None:
            personas_dir = Path(tempfile.mkdtemp(prefix="lyre-personas-"))
        self.personas = FilesystemPersonaRepository(
            personas_dir, persona_overrides=persona_overrides,
        )
        self.agents = SqliteAgentRepository(conn)
        self.tasks = SqliteTaskRepository(conn)
        self.wakeups = SqliteWakeupRepository(conn)
        self.mailbox = SqliteMailboxRepository(conn)
        self.scheduled_mail = SqliteScheduledMailRepository(conn)
        self.outbox = SqliteOutboxRepository(conn)
        self.skills = SqliteSkillRepository(conn)
        self.artifacts = SqliteArtifactRepository(conn)
        self.local_hot = SqliteLocalHotRepository(conn)
        self.blobs = SqliteBlobRepository(conn)
        self.fan_in = SqliteFanInRepository(conn)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """Batch several DAO writes into ONE atomic commit.

        Any DAO mutator called inside this block auto-suppresses its own
        commit (it commits via ``_commit()``, a no-op while ``_IN_TRANSACTION``
        is set), so every write joins one transaction that this block commits
        once — or rolls back entirely if the body raises. There is no per-call
        flag to pass and none to forget, so a mutator can't silently
        half-commit a multi-row write.

        This is the "advance state AND emit signal" seam the workflow
        scheduler needs (resolve a barrier + park/resume a task + enqueue a
        signal as one unit). Lyre runs SQLite at the default deferred
        isolation level, so the first DML implicitly opens the transaction and
        the suppressed inner commits let it span the whole block.

        Nested ``transaction()`` calls just join the outer one (the outermost
        block owns the single commit/rollback). The flag is a ContextVar, so
        it is scoped to this async task and never leaks into a concurrent flow
        that shares the same connection.
        """
        if _IN_TRANSACTION.get():
            # Already inside a transaction — join it; the outer block commits.
            yield
            return
        token = _IN_TRANSACTION.set(True)
        try:
            yield
            await self.conn.commit()
        except BaseException:
            await self.conn.rollback()
            raise
        finally:
            _IN_TRANSACTION.reset(token)
