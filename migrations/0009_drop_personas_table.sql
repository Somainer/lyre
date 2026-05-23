-- Drop the personas table entirely. Personas are filesystem-only
-- (~/.lyre/personas/<name>/identity.md is the single source of truth)
-- via FilesystemPersonaRepository. The DB was redundantly mirroring
-- the same content with a file→DB sync direction every bootstrap;
-- this is the same SSOT shift already done for skills (memory/skills/
-- proposed ↔ approved) but for personas.
--
-- Side effect: three FK clauses on `agents.persona_name`,
-- `tasks.persona_name`, `wakeups.persona_name` point at personas(name)
-- and would error on INSERT once the table is gone. The columns
-- themselves stay (the runtime still reads persona_name on every wakeup
-- to pick the right persona file), but the FK constraint is removed
-- via the standard SQLite rebuild-via-temp-table pattern.
--
-- PRAGMA foreign_keys must be flipped OUTSIDE the transaction — SQLite
-- silently no-ops the pragma when called inside an open transaction.

PRAGMA foreign_keys = OFF;

BEGIN;

------------------------------------------------------------
-- agents: drop FK on persona_name
------------------------------------------------------------
CREATE TABLE agents_v2 (
  id                TEXT PRIMARY KEY,
  persona_name      TEXT NOT NULL,
  status            TEXT NOT NULL DEFAULT 'idle'
                    CHECK (status IN ('idle','busy','archived')),
  parent_agent_id   TEXT,
  created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  archived_at       TEXT,
  metadata          TEXT
);

INSERT INTO agents_v2 (
  id, persona_name, status, parent_agent_id,
  created_at, archived_at, metadata
)
SELECT
  id, persona_name, status, parent_agent_id,
  created_at, archived_at, metadata
FROM agents;

DROP TABLE agents;
ALTER TABLE agents_v2 RENAME TO agents;

CREATE INDEX IF NOT EXISTS agents_persona ON agents(persona_name, status);
CREATE INDEX IF NOT EXISTS agents_status ON agents(status);
CREATE INDEX IF NOT EXISTS agents_parent ON agents(parent_agent_id);

------------------------------------------------------------
-- tasks: drop FK on persona_name (keep all other columns including
-- git_context added in 0008)
------------------------------------------------------------
CREATE TABLE tasks_v2 (
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
  checkpoint        TEXT,
  tier_overrides    TEXT,
  deadline          TEXT,
  metadata          TEXT,
  created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  completed_at      TEXT,
  git_context       TEXT
);

INSERT INTO tasks_v2 (
  id, parent_task_id, persona_name, agent_id, goal, acceptance, status,
  lease_duration_s, lease_holder, lease_until, checkpoint, tier_overrides,
  deadline, metadata, created_at, updated_at, completed_at, git_context
)
SELECT
  id, parent_task_id, persona_name, agent_id, goal, acceptance, status,
  lease_duration_s, lease_holder, lease_until, checkpoint, tier_overrides,
  deadline, metadata, created_at, updated_at, completed_at, git_context
FROM tasks;

DROP TABLE tasks;
ALTER TABLE tasks_v2 RENAME TO tasks;

CREATE INDEX IF NOT EXISTS tasks_status_lease ON tasks(status, lease_until);
CREATE INDEX IF NOT EXISTS tasks_parent ON tasks(parent_task_id);
CREATE INDEX IF NOT EXISTS tasks_agent_status ON tasks(agent_id, status);
CREATE INDEX IF NOT EXISTS tasks_created ON tasks(created_at);
CREATE INDEX IF NOT EXISTS tasks_updated ON tasks(updated_at);
CREATE INDEX IF NOT EXISTS tasks_completed
  ON tasks(completed_at) WHERE status = 'completed';

------------------------------------------------------------
-- wakeups: drop FK on persona_name
------------------------------------------------------------
CREATE TABLE wakeups_v2 (
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
  context_peak_tokens   INTEGER,
  compaction_count      INTEGER NOT NULL DEFAULT 0
);

INSERT INTO wakeups_v2 (
  id, task_id, persona_name, agent_id, started_at, ended_at, end_status,
  token_input, token_output, wall_clock_ms, tool_call_count, provider,
  model, failure_report, transcript_uri, context_peak_tokens, compaction_count
)
SELECT
  id, task_id, persona_name, agent_id, started_at, ended_at, end_status,
  token_input, token_output, wall_clock_ms, tool_call_count, provider,
  model, failure_report, transcript_uri, context_peak_tokens, compaction_count
FROM wakeups;

DROP TABLE wakeups;
ALTER TABLE wakeups_v2 RENAME TO wakeups;

CREATE INDEX IF NOT EXISTS wakeups_task ON wakeups(task_id, started_at);
CREATE INDEX IF NOT EXISTS wakeups_agent ON wakeups(agent_id, started_at);
CREATE INDEX IF NOT EXISTS wakeups_active ON wakeups(started_at)
  WHERE ended_at IS NULL;
CREATE INDEX IF NOT EXISTS wakeups_started ON wakeups(started_at);
CREATE INDEX IF NOT EXISTS wakeups_ended ON wakeups(ended_at);

------------------------------------------------------------
-- Now safe to drop personas (no FK references remain)
------------------------------------------------------------
DROP TABLE personas;

COMMIT;

PRAGMA foreign_keys = ON;
