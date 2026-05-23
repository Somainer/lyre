-- Decouple worktree from git.
--
-- Two coupled schema changes that follow from "worker is a pure
-- sandbox, git is task-scoped optional capability":
--
-- 1. ADD tasks.git_context (TEXT, nullable) — JSON-encoded GitContext
--    {repo_url, base_branch, target_branch}. When set, the scheduler
--    provisions an ephemeral SSH keypair + agent and clones the repo
--    into the task's worktree before the worker arrives. When NULL,
--    the worktree stays an empty tmpdir.
--
-- 2. DROP personas.needs_worktree — every LLM persona now gets a
--    worktree unconditionally (empty tmpdirs are effectively free).
--    The git-vs-not distinction has moved to the task level, where
--    it belongs: a single worker-maintainer agent can have one task
--    with git_context (code edit) and another without (skill
--    migration, data shaping, research). SQLite has no DROP COLUMN
--    on older 3.x — rebuild-via-temp-table pattern, same as the
--    outbox migrations.

BEGIN;

------------------------------------------------------------
-- tasks.git_context
------------------------------------------------------------
ALTER TABLE tasks ADD COLUMN git_context TEXT;

------------------------------------------------------------
-- personas: drop needs_worktree by rebuild
------------------------------------------------------------
CREATE TABLE personas_v2 (
  name                 TEXT PRIMARY KEY,
  role_description     TEXT NOT NULL,
  system_prompt        TEXT NOT NULL,
  allowed_lyre_tools   TEXT NOT NULL DEFAULT '[]',
  model_preference     TEXT,
  status               TEXT NOT NULL DEFAULT 'approved'
                       CHECK (status IN ('proposed','approved','deprecated')),
  proposed_by_task_id  TEXT,
  reviewer             TEXT,
  reviewed_at          TEXT,
  metadata             TEXT,
  created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  display_name         TEXT,
  kind                 TEXT
);

INSERT INTO personas_v2 (
  name, role_description, system_prompt, allowed_lyre_tools,
  model_preference, status, proposed_by_task_id, reviewer,
  reviewed_at, metadata, created_at, updated_at,
  display_name, kind
)
SELECT
  name, role_description, system_prompt, allowed_lyre_tools,
  model_preference, status, proposed_by_task_id, reviewer,
  reviewed_at, metadata, created_at, updated_at,
  display_name, kind
FROM personas;

DROP TABLE personas;
ALTER TABLE personas_v2 RENAME TO personas;

CREATE INDEX IF NOT EXISTS personas_status ON personas(status);

COMMIT;
