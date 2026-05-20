-- 0004_scheduled_mail.sql
-- Future mail: schedule a mailbox_send to fire at a later moment, optionally
-- recurring (interval or cron). Powers cron-like supervision, timeouts,
-- "remind me later" patterns.
--
-- Lifecycle:
--   pending      — `scheduled_for` is in the future; scheduler tick will
--                  deliver when due
--   completed    — delivered (and, for recurring, no more occurrences in
--                  window OR `recur_until` passed)
--   cancelled    — explicit cancel; future fires stopped (past deliveries
--                  remain in mailbox_messages, untouched)
--   bounced     — recipient agent was archived at delivery time; a
--                  notice was delivered to the creator instead
--
-- Recurrence model:
--   recur_kind = 'interval' → recur_value is a duration string like '1h',
--                              '24h', '1w', '30m'. Next fire = last delivery
--                              + parsed duration. Minimum 1 minute.
--   recur_kind = 'cron'     → recur_value is a 5-field POSIX cron expression
--                              like '0 9 * * 1-5'. Next fire computed via
--                              croniter from last delivery time.
--   recur_kind NULL         → one-shot. `scheduled_for` doesn't change;
--                              status -> 'completed' after delivery.
--
-- After each delivery the row's `scheduled_for` is mutated to the next-fire
-- time; we don't insert one row per occurrence. The history of actual
-- deliveries lives in mailbox_messages (linkable via last_delivery_id
-- pointing at the most-recent delivered message).

CREATE TABLE IF NOT EXISTS scheduled_mail (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  recipient         TEXT NOT NULL,    -- agent_id ('owner' allowed as special)
  sender            TEXT NOT NULL,
  urgency           TEXT NOT NULL
                    CHECK(urgency IN ('blocker','high','normal','low')),
  body              TEXT NOT NULL,
  task_id           TEXT REFERENCES tasks(id),
  parent_msg_id     INTEGER REFERENCES mailbox_messages(id),
  metadata          TEXT,             -- JSON

  -- Next-fire time. Mutated after each delivery for recurring mail.
  scheduled_for     TEXT NOT NULL,    -- ISO 8601 UTC

  -- Recurrence (NULL = one-shot)
  recur_kind        TEXT CHECK(recur_kind IN ('interval','cron')),
  recur_value       TEXT,             -- "1w" | cron expr | NULL
  recur_until       TEXT,             -- ISO 8601 UTC; NULL = no horizon
                                      -- (capped at first_fire + 1y at create)
  occurrence_count  INTEGER NOT NULL DEFAULT 0,

  -- Bookkeeping
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

-- Most queries are "find rows whose scheduled_for is in the past and which
-- are still pending" — partial index makes that O(due_count).
CREATE INDEX IF NOT EXISTS scheduled_mail_due
  ON scheduled_mail(scheduled_for) WHERE status='pending';
CREATE INDEX IF NOT EXISTS scheduled_mail_recipient
  ON scheduled_mail(recipient, status);
CREATE INDEX IF NOT EXISTS scheduled_mail_creator
  ON scheduled_mail(created_by_agent, status);
