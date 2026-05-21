-- Lyre initial schema
-- See docs/design/PERSISTENCE_SCHEMA.md for design and Postgres equivalents.
-- This file is idempotent (CREATE TABLE IF NOT EXISTS) so it can be re-run safely.

------------------------------------------------------------
-- Personas (global)
------------------------------------------------------------
CREATE TABLE IF NOT EXISTS personas (
  name                 TEXT PRIMARY KEY,
  role_description     TEXT NOT NULL,
  system_prompt        TEXT NOT NULL,
  allowed_lyre_tools   TEXT NOT NULL DEFAULT '[]',
  model_preference     TEXT,  -- JSON with tier/requires/prefer
  needs_worktree       INTEGER NOT NULL DEFAULT 1,
  status               TEXT NOT NULL DEFAULT 'approved'
                       CHECK (status IN ('proposed','approved','deprecated')),
  proposed_by_task_id  TEXT,
  reviewer             TEXT,
  reviewed_at          TEXT,
  metadata             TEXT,
  created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS personas_status ON personas(status);

------------------------------------------------------------
-- Agents (running instances of a persona)
------------------------------------------------------------
-- persona = role definition (the personas table; one md file)
-- agent   = running instance with own identity, mailbox, task queue.
--           Multiple agents can share one persona.
CREATE TABLE IF NOT EXISTS agents (
  id                TEXT PRIMARY KEY,
  persona_name      TEXT NOT NULL REFERENCES personas(name),
  status            TEXT NOT NULL DEFAULT 'idle'
                    CHECK (status IN ('idle','busy','archived')),
  -- parent_agent_id: the agent that spawned this one (or NULL for
  -- bootstrap agents `owner`/`leader`). String "owner" is also valid
  -- when the human owner created the agent directly via CLI/dashboard.
  parent_agent_id   TEXT,
  created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  archived_at       TEXT,
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
  persona_name      TEXT NOT NULL REFERENCES personas(name),
  agent_id          TEXT REFERENCES agents(id),
  goal              TEXT NOT NULL,
  acceptance        TEXT NOT NULL,
  status            TEXT NOT NULL
                    CHECK (status IN
                      ('pending','in_progress','needs_input','completed','failed','cancelled')),
  lease_duration_s  INTEGER NOT NULL DEFAULT 1800,
  lease_holder      TEXT,
  lease_until       TEXT,
  checkpoint        TEXT,
  tier_overrides    TEXT,
  deadline          TEXT,
  metadata          TEXT,
  created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  completed_at      TEXT
);

CREATE INDEX IF NOT EXISTS tasks_status_lease ON tasks(status, lease_until);
CREATE INDEX IF NOT EXISTS tasks_parent ON tasks(parent_task_id);
CREATE INDEX IF NOT EXISTS tasks_agent_status ON tasks(agent_id, status);
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
  persona_name          TEXT NOT NULL REFERENCES personas(name),
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
  -- Number of mid-wakeup auto-compactions (>0 means we crossed the threshold
  -- at least once).
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
-- Without these, py-spy showed the dashboard pegged 28/28s inside the
-- aiosqlite worker thread doing full table scans on wakeups.
CREATE INDEX IF NOT EXISTS wakeups_active ON wakeups(started_at)
  WHERE ended_at IS NULL;
CREATE INDEX IF NOT EXISTS wakeups_started ON wakeups(started_at);
CREATE INDEX IF NOT EXISTS wakeups_ended ON wakeups(ended_at);

------------------------------------------------------------
-- Mailboxes + messages + outbox
------------------------------------------------------------
-- mailboxes.recipient is conceptually an agent_id. No hard FK because
-- bootstrap order requires the `owner` mailbox before any agent exists.
-- Tool-level validation in mailbox_send/read rejects unknown recipients.
CREATE TABLE IF NOT EXISTS mailboxes (
  recipient                TEXT PRIMARY KEY,
  metadata                 TEXT
);

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
-- that ordering — for a single recipient its entries are sorted by
-- (urgency, id), so SQLite has to pull every row for the recipient and
-- sort in memory to pick the top N by id. On a busy mailbox this is
-- the hottest path on the Mail page (and the MailboxBroadcaster poll).
-- A plain (recipient, id) index lets SQLite reverse-scan and stop at
-- LIMIT, which is what we want.
CREATE INDEX IF NOT EXISTS mailbox_messages_recipient_id
  ON mailbox_messages(recipient, id);
-- read_recent_for_audit and Activity-builder span all recipients
-- filtered by delivered_at >= cutoff. Without this it was a full
-- table scan every Home / Activity render.
CREATE INDEX IF NOT EXISTS mailbox_messages_delivered
  ON mailbox_messages(delivered_at);

CREATE TABLE IF NOT EXISTS outbox (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id            TEXT NOT NULL REFERENCES tasks(id),
  wakeup_id          TEXT NOT NULL REFERENCES wakeups(id),
  kind               TEXT NOT NULL CHECK (kind IN
                       ('mailbox_send','tier1_notification')),
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

------------------------------------------------------------
-- Scheduled mail (future mail / cron)
------------------------------------------------------------
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
-- Local-hot, Artifacts, Skills
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

------------------------------------------------------------
-- Schema version (for future migrations)
------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_migrations (
  version    INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

INSERT OR IGNORE INTO schema_migrations (version) VALUES (1);
