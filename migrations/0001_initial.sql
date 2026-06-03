-- Lyre initial schema (single-file baseline).
-- See docs/design/PERSISTENCE_SCHEMA.md for design and Postgres equivalents.
-- Idempotent (CREATE TABLE IF NOT EXISTS) so it's safe to re-run.
--
-- Note: there's no `personas` table. Personas live entirely on disk at
-- ``~/.lyre/personas/<name>/identity.md`` and are served via
-- ``lyre.persistence.fs_personas.FilesystemPersonaRepository``. Tables
-- below that store a ``persona_name`` keep it as a plain TEXT column —
-- no FK, no cascade.

------------------------------------------------------------
-- Mailboxes (referenced by mailbox_messages)
------------------------------------------------------------
-- mailboxes.recipient is conceptually an agent_id. No hard FK because
-- bootstrap order requires the `owner` mailbox before any agent exists.
-- Tool-level validation in mailbox_send/read rejects unknown recipients.
CREATE TABLE IF NOT EXISTS mailboxes (
  recipient                TEXT PRIMARY KEY,
  metadata                 TEXT
);

------------------------------------------------------------
-- Agents (running instances of a persona)
------------------------------------------------------------
-- persona = role definition (markdown file on disk; see fs_personas.py)
-- agent   = running instance with own identity, mailbox, task queue.
--           Multiple agents can share one persona.
CREATE TABLE IF NOT EXISTS agents (
  id                TEXT PRIMARY KEY,
  persona_name      TEXT NOT NULL,
  -- Lifecycle only: 'idle' (alive, may or may not be running a wakeup
  -- right now) or 'archived'. There used to be a 'busy' value here
  -- but the runtime never wrote it — running state is derived from
  -- wakeups.ended_at IS NULL by list_agents and the dashboard.
  status            TEXT NOT NULL DEFAULT 'idle'
                    CHECK (status IN ('idle','archived')),
  -- parent_agent_id: the agent that spawned this one (or NULL for
  -- bootstrap agents `owner`/`leader`). String "owner" is also valid
  -- when the human owner created the agent directly via CLI/dashboard.
  parent_agent_id   TEXT,
  created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  archived_at       TEXT,
  -- Why this agent was archived, for observability (list_agents / dashboard).
  -- Free text (soft label, not a state-machine value): 'reaped' (ephemeral GC
  -- by the Phase 0.8 reaper), 'storm_halted' (restart-intensity exceeded →
  -- escalated + reclaimed), 'idle_reclaimed' (Dispatcher archived a stale
  -- non-ephemeral child), 'manual' (archive_agent tool / CLI). NULL on live
  -- agents and on rows archived before this column existed.
  archive_reason    TEXT,
  metadata          TEXT     -- JSON: {model_id?, description?, ...}
);

CREATE INDEX IF NOT EXISTS agents_persona ON agents(persona_name, status);
CREATE INDEX IF NOT EXISTS agents_status ON agents(status);
CREATE INDEX IF NOT EXISTS agents_parent ON agents(parent_agent_id);

------------------------------------------------------------
-- Tasks
------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tasks (
  id                TEXT PRIMARY KEY,
  parent_task_id    TEXT REFERENCES tasks(id),
  persona_name      TEXT NOT NULL,
  agent_id          TEXT REFERENCES agents(id),
  goal              TEXT NOT NULL,
  acceptance        TEXT NOT NULL,
  status            TEXT NOT NULL
                    CHECK (status IN
                      ('pending','in_progress','needs_input','completed','failed','cancelled')),
  lease_duration_s  INTEGER NOT NULL DEFAULT 1800,
  lease_holder      TEXT,
  lease_until       TEXT,
  -- resume_ready: park/resume seam for scheduler-driven fan-in barriers.
  -- A task parked in 'needs_input' (e.g. a coordinator awaiting a barrier)
  -- is invisible to find_pending (status!='pending') and find_expired_leases
  -- (status!='in_progress'). When the awaited event fires, the barrier (or a
  -- deadline / escalation) sets resume_ready=1; Phase 0.7
  -- (_resume_parked_tasks) is the SOLE writer that flips needs_input ->
  -- pending. Decoupling the flag from status keeps the transition
  -- single-writer and kill-safe (a SIGKILL between flag-set and flip simply
  -- re-resumes next tick). 0 = not awaiting resume.
  resume_ready      INTEGER NOT NULL DEFAULT 0,
  checkpoint        TEXT,
  tier_overrides    TEXT,
  deadline          TEXT,
  metadata          TEXT,
  -- Optional per-task git working copy. JSON-encoded GitContext
  -- (repo_url / base_branch / target_branch). NULL means the worker
  -- gets a clean tmpdir sandbox without any git provisioning.
  git_context       TEXT,
  created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  completed_at      TEXT
);

CREATE INDEX IF NOT EXISTS tasks_status_lease ON tasks(status, lease_until);
CREATE INDEX IF NOT EXISTS tasks_parent ON tasks(parent_task_id);
CREATE INDEX IF NOT EXISTS tasks_agent_status ON tasks(agent_id, status);
-- Phase 0.7 (_resume_parked_tasks) scans only parked tasks each tick;
-- the partial index keeps that O(parked) instead of O(all tasks).
CREATE INDEX IF NOT EXISTS tasks_resumable
  ON tasks(resume_ready) WHERE status = 'needs_input';
-- Dashboard hot paths:
--   * find_recent ORDER BY created_at DESC
--   * find_recently_changed WHERE updated_at >= ? + MAX(updated_at) in
--     the broadcaster snapshot
--   * count_completed_since WHERE status='completed' AND completed_at >= ?
CREATE INDEX IF NOT EXISTS tasks_created ON tasks(created_at);
CREATE INDEX IF NOT EXISTS tasks_updated ON tasks(updated_at);
CREATE INDEX IF NOT EXISTS tasks_completed
  ON tasks(completed_at) WHERE status = 'completed';

------------------------------------------------------------
-- Wakeups (cold-archive index)
------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wakeups (
  id                    TEXT PRIMARY KEY,
  task_id               TEXT NOT NULL REFERENCES tasks(id),
  persona_name          TEXT NOT NULL,
  agent_id              TEXT REFERENCES agents(id),
  started_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ended_at              TEXT,
  end_status            TEXT,
  token_input           INTEGER,
  token_output          INTEGER,
  wall_clock_ms         INTEGER,
  tool_call_count       INTEGER,
  provider              TEXT,
  model                 TEXT,
  failure_report        TEXT,
  transcript_uri        TEXT,
  -- Largest input_tokens reported across the wakeup's turns. Each API call
  -- resends the full message list, so per-turn input_tokens equals the
  -- running context size — this column captures the max.
  context_peak_tokens   INTEGER,
  -- Number of mid-wakeup auto-compactions (>0 means we crossed the
  -- threshold at least once).
  compaction_count      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS wakeups_task ON wakeups(task_id, started_at);
CREATE INDEX IF NOT EXISTS wakeups_agent ON wakeups(agent_id, started_at);
-- Hot paths used by the dashboard broadcaster (every 1s when subscribers
-- exist) and every activity/home page render:
--   * list_active() filters WHERE ended_at IS NULL — partial index over
--     just the active wakeups (tiny in a busy system; most have ended).
--   * list_recent / list_since / sum_tokens_since / MAX(started_at) /
--     ORDER BY started_at DESC — covered by an outright started_at index.
--   * MAX(ended_at) in the broadcaster snapshot — ended index.
CREATE INDEX IF NOT EXISTS wakeups_active ON wakeups(started_at)
  WHERE ended_at IS NULL;
CREATE INDEX IF NOT EXISTS wakeups_started ON wakeups(started_at);
CREATE INDEX IF NOT EXISTS wakeups_ended ON wakeups(ended_at);

------------------------------------------------------------
-- Mailbox messages + outbox + scheduled mail
------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mailbox_messages (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  recipient       TEXT NOT NULL REFERENCES mailboxes(recipient),
  external_id     TEXT NOT NULL,
  sender          TEXT NOT NULL,
  urgency         TEXT NOT NULL CHECK (urgency IN ('blocker','high','normal','low')),
  -- Subject-line summary for inbox listings. Written at send time:
  --   explicit `title` param if provided (≤140 char), or
  --   first non-empty line of body truncated to 140 char.
  title           TEXT,
  body            TEXT NOT NULL,
  -- JSON list of multimodal attachment descriptors (image / document
  -- blob_ids resolved at send time by adapters). NULL for plain-text mail.
  attachments     TEXT,
  task_id         TEXT REFERENCES tasks(id),
  parent_msg_id   INTEGER REFERENCES mailbox_messages(id),
  -- Broadcast: broadcast_id groups copies of one multi-recipient send;
  -- recipients_all is a JSON list of every recipient on the thread.
  broadcast_id    TEXT,
  recipients_all  TEXT,
  metadata        TEXT,
  delivered_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  -- NULL = unread. Set by `mailbox_read` (auto-mark) or explicit `mark_read`.
  read_at         TEXT,
  UNIQUE (recipient, external_id)
);

CREATE INDEX IF NOT EXISTS mailbox_messages_inbox
  ON mailbox_messages(recipient, urgency, id);
CREATE INDEX IF NOT EXISTS mailbox_messages_broadcast
  ON mailbox_messages(broadcast_id);
CREATE INDEX IF NOT EXISTS mailbox_messages_unread
  ON mailbox_messages(recipient, urgency, id) WHERE read_at IS NULL;
-- read_messages_paged + read_messages share the shape
--   WHERE recipient = ? [AND id <|> ?] ORDER BY id [DESC|ASC] LIMIT ?
-- The composite (recipient, urgency, id) index above CAN'T short-circuit
-- that ordering — a plain (recipient, id) index lets SQLite reverse-scan
-- and stop at LIMIT.
CREATE INDEX IF NOT EXISTS mailbox_messages_recipient_id
  ON mailbox_messages(recipient, id);
-- read_recent_for_audit and Activity-builder span all recipients
-- filtered by delivered_at >= cutoff.
CREATE INDEX IF NOT EXISTS mailbox_messages_delivered
  ON mailbox_messages(delivered_at);
-- Workflow fan-in barrier: Phase 0.5 counts delivered result-mails per group
-- by json_extract(metadata,'$.fan_in.group_id'). An expression index keeps
-- that count off a full-table scan. SQLite may decline an expression index
-- for some GROUP BY/HAVING shapes — PR2 ships an EXPLAIN QUERY PLAN test that
-- asserts it is consulted, else the barrier falls back to a bounded scan of
-- the coordinator's own inbox (recipient-narrowed), never lifetime mail.
CREATE INDEX IF NOT EXISTS mailbox_messages_fan_in
  ON mailbox_messages(json_extract(metadata, '$.fan_in.group_id'), recipient);

-- Reactions: lightweight ack channel that doesn't generate a new
-- mailbox_messages row. Lyre tracks the (msg, reactor) tuple so a
-- recipient can "read & acknowledge" without writing prose back.
CREATE TABLE IF NOT EXISTS mail_reactions (
  msg_id       INTEGER NOT NULL REFERENCES mailbox_messages(id),
  reactor      TEXT NOT NULL REFERENCES agents(id),
  kind         TEXT NOT NULL CHECK (kind IN ('ack')),
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (msg_id, reactor, kind)
);

CREATE INDEX IF NOT EXISTS mail_reactions_msg_id
  ON mail_reactions(msg_id, created_at);

-- Outbox: agent → mailbox_messages with at-least-once delivery via the
-- async dispatcher. task_id / wakeup_id are nullable so out-of-band
-- enqueues (channel webhooks, CLI sends) can flow through the same
-- pipe without inventing fake tasks.
CREATE TABLE IF NOT EXISTS outbox (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id            TEXT REFERENCES tasks(id),
  wakeup_id          TEXT REFERENCES wakeups(id),
  kind               TEXT NOT NULL CHECK (kind IN
                       ('mailbox_send','tier1_notification',
                        'channel_publish','channel_reaction_publish')),
  payload            TEXT NOT NULL,
  external_id        TEXT NOT NULL,
  created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  dispatched_at      TEXT,
  dispatch_attempts  INTEGER NOT NULL DEFAULT 0,
  last_error         TEXT,
  UNIQUE (kind, external_id)
);

CREATE INDEX IF NOT EXISTS outbox_undispatched ON outbox(created_at)
  WHERE dispatched_at IS NULL;

-- Scheduled mail (future mail / cron).
-- Lifecycle:
--   pending    — scheduled_for is in the future; scheduler tick delivers when due
--   completed  — delivered (and, for recurring, no more occurrences in window)
--   cancelled  — explicit cancel; future fires stopped
--   bounced    — recipient was archived at delivery time; notice to creator
--
-- Recurrence (NULL recur_kind = one-shot):
--   interval → recur_value is duration like '1h', '24h', '1w' (min 1m)
--   cron     → recur_value is 5-field POSIX cron expression; next-fire via croniter
CREATE TABLE IF NOT EXISTS scheduled_mail (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  recipient         TEXT NOT NULL,
  sender            TEXT NOT NULL,
  urgency           TEXT NOT NULL
                    CHECK(urgency IN ('blocker','high','normal','low')),
  title             TEXT,
  body              TEXT NOT NULL,
  task_id           TEXT REFERENCES tasks(id),
  parent_msg_id     INTEGER REFERENCES mailbox_messages(id),
  metadata          TEXT,
  scheduled_for     TEXT NOT NULL,    -- ISO 8601 UTC, mutated after each delivery
  recur_kind        TEXT CHECK(recur_kind IN ('interval','cron')),
  recur_value       TEXT,
  recur_until       TEXT,             -- NULL = no horizon
  occurrence_count  INTEGER NOT NULL DEFAULT 0,
  created_at        TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  created_by_agent  TEXT,
  created_by_task   TEXT REFERENCES tasks(id),
  status            TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','completed','cancelled','bounced')),
  last_delivery_id  INTEGER REFERENCES mailbox_messages(id),
  last_delivered_at TEXT,
  cancelled_at      TEXT,
  cancelled_by      TEXT,
  bounce_reason     TEXT
);

CREATE INDEX IF NOT EXISTS scheduled_mail_due
  ON scheduled_mail(scheduled_for) WHERE status='pending';
CREATE INDEX IF NOT EXISTS scheduled_mail_recipient
  ON scheduled_mail(recipient, status);
CREATE INDEX IF NOT EXISTS scheduled_mail_creator
  ON scheduled_mail(created_by_agent, status);

------------------------------------------------------------
-- Local-hot, Artifacts, Skills, Blobs
------------------------------------------------------------
CREATE TABLE IF NOT EXISTS local_hot (
  task_id      TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  key          TEXT NOT NULL,
  value        TEXT,
  blob_uri     TEXT,
  updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (task_id, key)
);

CREATE TABLE IF NOT EXISTS artifacts (
  id             TEXT PRIMARY KEY,
  task_id        TEXT NOT NULL REFERENCES tasks(id),
  wakeup_id      TEXT NOT NULL REFERENCES wakeups(id),
  kind           TEXT NOT NULL,
  content_hash   TEXT NOT NULL,
  blob_uri       TEXT NOT NULL,
  size_bytes     INTEGER,
  metadata       TEXT,
  created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE (content_hash)
);

CREATE INDEX IF NOT EXISTS artifacts_task ON artifacts(task_id);

CREATE TABLE IF NOT EXISTS skills (
  id              TEXT PRIMARY KEY,
  name            TEXT NOT NULL UNIQUE,
  frontmatter     TEXT NOT NULL,
  body            TEXT NOT NULL,
  status          TEXT NOT NULL
                  CHECK (status IN ('proposed','approved','deprecated')),
  source_task_id  TEXT REFERENCES tasks(id),
  reviewer        TEXT,
  reviewed_at     TEXT,
  scope           TEXT,
  metadata        TEXT,
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS skills_status ON skills(status);
CREATE INDEX IF NOT EXISTS skills_scope ON skills(scope, status);

-- Blob registry: multimodal attachments (images, PDFs, ...) the dashboard
-- uploads and adapters resolve at send time. Bytes live in the object
-- store; this table is the metadata index.
CREATE TABLE IF NOT EXISTS blobs (
  id           TEXT PRIMARY KEY,         -- sha256 hex of the file contents
  media_type   TEXT NOT NULL,            -- e.g. 'image/png', 'application/pdf'
  size_bytes   INTEGER NOT NULL,
  -- Original filename when uploaded via dashboard. Optional, no semantic
  -- meaning — just preserved for human display ("screenshot.png" beats
  -- raw hash in a mail-detail page). NULL for blobs that arrive without
  -- a filename (tool returns, future cases).
  filename     TEXT,
  -- Who originated this blob — same shape as mailbox.sender / agent.id.
  source       TEXT NOT NULL,
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS blobs_created ON blobs(created_at);

------------------------------------------------------------
-- Workflow fan-in barrier (deterministic orchestration).
-- See docs/design/WORKFLOW_ORCHESTRATION.md. The barrier is mailbox-driven:
-- results ride mailbox_messages; these rows carry ONLY the coordination
-- contract (fan_in_groups) and the per-slot lineage roster (fan_in_members),
-- never inter-agent payload. The scheduler is a read-only mailbox client that
-- resolves a group when COUNT(DISTINCT leg_key) of delivered result-mails
-- reaches `quorum` (or `deadline` passes).
------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fan_in_groups (
  id                   TEXT PRIMARY KEY,            -- coordinator-minted
  coordinator_agent_id TEXT NOT NULL REFERENCES agents(id),
  parent_task_id       TEXT REFERENCES tasks(id),   -- the task that opened it
  expect_replies       INTEGER NOT NULL,            -- intended width (durable)
  quorum               INTEGER NOT NULL,            -- trips at >= quorum delivered
  result_schema        TEXT NOT NULL,               -- JSON Schema each result validates against
  budget_tokens        INTEGER,                     -- reserved for loop-until-budget (PR7)
  dry_round            INTEGER NOT NULL DEFAULT 0,   -- reserved for loop-until-dry (PR7)
  -- NOT NULL: every group is reapable, so a dead coordinator cannot leak an
  -- open group forever — Phase 0.5 expires it past deadline (liveness).
  deadline             TEXT NOT NULL,
  status               TEXT NOT NULL DEFAULT 'open'
                       CHECK (status IN ('open','quorum_met','expired','cancelled','resolved')),
  created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  resolved_at          TEXT
);
-- Partial index: Phase 0.5 scans ONLY open groups each tick → O(open).
CREATE INDEX IF NOT EXISTS fan_in_groups_open
  ON fan_in_groups(deadline) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS fan_in_groups_coordinator
  ON fan_in_groups(coordinator_agent_id, status);

CREATE TABLE IF NOT EXISTS fan_in_members (
  group_id       TEXT NOT NULL REFERENCES fan_in_groups(id),
  leg_key        INTEGER NOT NULL,                  -- stable slot key, 0..expect_replies-1
  child_task_id  TEXT NOT NULL REFERENCES tasks(id),
  child_agent_id TEXT NOT NULL REFERENCES agents(id),
  -- (group_id, leg_key) is the dedup key, NOT child_task_id: a re-dispatched
  -- child gets a fresh task id, so keying on that would not dedup on replay.
  PRIMARY KEY (group_id, leg_key)
);
CREATE INDEX IF NOT EXISTS fan_in_members_group ON fan_in_members(group_id);

------------------------------------------------------------
-- Supervision state (Erlang/OTP restart-intensity window).
-- The supervisor reaper restarts a failed ephemeral agent's leg one-for-one,
-- bounded by max_restarts within a sliding max_seconds window; on exceed it
-- escalates (a mail to the parent) instead of looping. A TYPED table — not a
-- key in agents.metadata — because update_metadata is a full-column overwrite
-- and a torn read-modify-write of a JSON counter could silently reset the
-- window; an atomically-updatable row cannot.
------------------------------------------------------------
CREATE TABLE IF NOT EXISTS supervision_state (
  agent_id        TEXT PRIMARY KEY REFERENCES agents(id),
  restart_count   INTEGER NOT NULL DEFAULT 0,   -- restarts inside the current window
  window_start_at TEXT NOT NULL,                -- ISO 8601 UTC; window resets past max_seconds
  last_restart_at TEXT,
  last_reason     TEXT,                          -- coarse outcome of the restarted task
  escalated_at    TEXT                           -- set when intensity was exceeded
);

------------------------------------------------------------
-- Schema version
------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_migrations (
  version    INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

INSERT OR IGNORE INTO schema_migrations (version) VALUES (1);
