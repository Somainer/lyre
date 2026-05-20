-- Lyre initial schema
-- See PERSISTENCE_SCHEMA.md §3 for full design and Postgres equivalents.
-- This file is idempotent (CREATE TABLE IF NOT EXISTS) so it can be re-run safely.

------------------------------------------------------------
-- Personas (global)
------------------------------------------------------------
CREATE TABLE IF NOT EXISTS personas (
  name                 TEXT PRIMARY KEY,
  role_description     TEXT NOT NULL,
  system_prompt        TEXT NOT NULL,
  allowed_lyre_tools   TEXT NOT NULL DEFAULT '[]',
  -- Q9 (2026-05-17): renamed from model_routing. JSON with tier/requires/prefer.
  model_preference     TEXT,
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
-- Persona profiles (含 owner Soul)
------------------------------------------------------------
CREATE TABLE IF NOT EXISTS persona_profiles (
  persona_name         TEXT PRIMARY KEY REFERENCES personas(name),
  profile              TEXT NOT NULL,
  updated_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- Vector companion table (sqlite-vec); enable at runtime via loadable extension
-- CREATE VIRTUAL TABLE persona_profiles_vec USING vec0(persona_name TEXT PRIMARY KEY, embedding FLOAT[768]);

------------------------------------------------------------
-- Tasks
------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tasks (
  id                TEXT PRIMARY KEY,
  parent_task_id    TEXT REFERENCES tasks(id),
  persona_name      TEXT NOT NULL REFERENCES personas(name),
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

------------------------------------------------------------
-- Wakeups (cold-archive index)
------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wakeups (
  id               TEXT PRIMARY KEY,
  task_id          TEXT NOT NULL REFERENCES tasks(id),
  persona_name     TEXT NOT NULL REFERENCES personas(name),
  started_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ended_at         TEXT,
  end_status       TEXT,
  token_input      INTEGER,
  token_output     INTEGER,
  wall_clock_ms    INTEGER,
  tool_call_count  INTEGER,
  provider         TEXT,
  model            TEXT,
  failure_report   TEXT,
  transcript_uri   TEXT
);

CREATE INDEX IF NOT EXISTS wakeups_task ON wakeups(task_id, started_at);

------------------------------------------------------------
-- Mailboxes + messages + outbox
------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mailboxes (
  recipient                TEXT PRIMARY KEY,
  last_processed_msg_id    INTEGER NOT NULL DEFAULT 0,
  metadata                 TEXT
);

CREATE TABLE IF NOT EXISTS mailbox_messages (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  recipient       TEXT NOT NULL REFERENCES mailboxes(recipient),
  external_id     TEXT NOT NULL,
  sender          TEXT NOT NULL,
  urgency         TEXT NOT NULL CHECK (urgency IN ('blocker','high','normal','low')),
  body            TEXT NOT NULL,
  task_id         TEXT REFERENCES tasks(id),
  parent_msg_id   INTEGER REFERENCES mailbox_messages(id),
  metadata        TEXT,
  delivered_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE (recipient, external_id)
);

CREATE INDEX IF NOT EXISTS mailbox_messages_inbox
  ON mailbox_messages(recipient, urgency, id);

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
-- Local-hot, Global facts, Artifacts, Skills
------------------------------------------------------------
CREATE TABLE IF NOT EXISTS local_hot (
  task_id      TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  key          TEXT NOT NULL,
  value        TEXT,
  blob_uri     TEXT,
  updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (task_id, key)
);

CREATE TABLE IF NOT EXISTS global_facts (
  id              TEXT PRIMARY KEY,
  kind            TEXT NOT NULL,
  scope           TEXT,
  body            TEXT NOT NULL,
  cold_pointer    TEXT,
  source_task_id  TEXT REFERENCES tasks(id),
  metadata        TEXT,
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS global_facts_kind ON global_facts(kind, scope);

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
-- Schema version table (for future migrations)
------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_migrations (
  version    INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

INSERT OR IGNORE INTO schema_migrations (version) VALUES (1);
