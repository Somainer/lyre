-- Outbox: add 'channel_reaction_publish' to the kind CHECK constraint.
--
-- A new outbox kind that mirrors Lyre reactions onto an external
-- channel (e.g. ``mailbox_react(kind="ack")`` on an owner-sent mail →
-- a ✓ emoji on the owner's original Lark message). The payload is
-- ``{channel, external_message_id, kind}`` and the dispatcher routes
-- it through ``ExternalChannel.publish_reaction``.
--
-- Same rebuild-via-temp-table pattern as 0003 / 0004 since SQLite
-- has no ALTER TABLE … DROP CHECK.

BEGIN;

CREATE TABLE outbox_v4 (
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

INSERT INTO outbox_v4 (
  id, task_id, wakeup_id, kind, payload, external_id,
  created_at, dispatched_at, dispatch_attempts, last_error
)
SELECT
  id, task_id, wakeup_id, kind, payload, external_id,
  created_at, dispatched_at, dispatch_attempts, last_error
FROM outbox;

DROP TABLE outbox;
ALTER TABLE outbox_v4 RENAME TO outbox;

COMMIT;
