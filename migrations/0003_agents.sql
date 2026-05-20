-- 0003_agents.sql
-- Agent as first-class entity, distinct from persona role.
--
-- Mental model:
--   - persona = role definition (the personas table; one md file)
--   - agent   = running instance with own identity, mailbox, task queue.
--               Multiple agents can share one persona (e.g. several workers
--               of persona=worker-maintainer running in parallel).
--
-- After this migration:
--   - mailbox recipients are agent_ids (not persona names)
--   - tasks reference agent_id; persona_name stays as a denormalized
--     convenience column (filled from agent.persona_name on insert) until
--     a later cleanup migration drops it
--   - wakeups gain agent_id for the same reason

CREATE TABLE IF NOT EXISTS agents (
  id            TEXT PRIMARY KEY,
  persona_name  TEXT NOT NULL REFERENCES personas(name),
  status        TEXT NOT NULL DEFAULT 'idle'
                CHECK (status IN ('idle','busy','archived')),
  created_by    TEXT,    -- 'owner' or another agent_id
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  archived_at   TEXT,
  metadata      TEXT     -- JSON: {model_id?, description?, ...}
);

CREATE INDEX IF NOT EXISTS agents_persona ON agents(persona_name, status);
CREATE INDEX IF NOT EXISTS agents_status ON agents(status);

-- tasks: add agent_id (keep persona_name as denorm; later migration drops it).
ALTER TABLE tasks ADD COLUMN agent_id TEXT REFERENCES agents(id);
CREATE INDEX IF NOT EXISTS tasks_agent_status ON tasks(agent_id, status);

-- wakeups: same
ALTER TABLE wakeups ADD COLUMN agent_id TEXT REFERENCES agents(id);
CREATE INDEX IF NOT EXISTS wakeups_agent ON wakeups(agent_id, started_at);

-- mailboxes.recipient is conceptually an agent_id now. We deliberately do NOT
-- add a hard FK here for two reasons:
--   1. Bootstrap order: a fresh DB has no agents until the bootstrap runs,
--      and the `owner` mailbox must be writable by Phase 0 before that.
--   2. The existing UNIQUE(recipient, external_id) index already prevents
--      typos from accumulating quietly. Tool-level validation
--      (mailbox_send/read) is the canonical place to reject unknown agents.

-- A second cursor for Phase 0 auto-dispatch (already added in code via
-- mailboxes.metadata JSON; nothing to migrate here).
