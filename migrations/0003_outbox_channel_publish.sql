-- Outbox: add 'channel_publish' to the kind CHECK constraint.
--
-- The 0001 schema pinned `kind IN ('mailbox_send','tier1_notification')`
-- so a fresh insert of a 'channel_publish' row trips the constraint
-- and the dispatcher never sees it. External-channel integrations
-- (Lark today, Slack/Discord later) all funnel through this one
-- new kind — payload.channel routes to the matching channel in
-- ChannelRegistry at dispatch time.
--
-- SQLite has no ALTER TABLE … DROP CHECK, so the canonical workaround
-- is rebuild-via-temp-table. Wrapped in a transaction so partial
-- failure can't leave the schema in an inconsistent half-renamed
-- state (kill-test invariant: 铁律 3).

BEGIN;

CREATE TABLE outbox_v2 (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id            TEXT NOT NULL REFERENCES tasks(id),
  wakeup_id          TEXT NOT NULL REFERENCES wakeups(id),
  kind               TEXT NOT NULL CHECK (kind IN
                       ('mailbox_send','tier1_notification',
                        'channel_publish')),
  payload            TEXT NOT NULL,
  external_id        TEXT NOT NULL,
  created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  dispatched_at      TEXT,
  dispatch_attempts  INTEGER NOT NULL DEFAULT 0,
  last_error         TEXT,
  UNIQUE (kind, external_id)
);

INSERT INTO outbox_v2 (
  id, task_id, wakeup_id, kind, payload, external_id,
  created_at, dispatched_at, dispatch_attempts, last_error
)
SELECT
  id, task_id, wakeup_id, kind, payload, external_id,
  created_at, dispatched_at, dispatch_attempts, last_error
FROM outbox;

DROP TABLE outbox;
ALTER TABLE outbox_v2 RENAME TO outbox;

COMMIT;
