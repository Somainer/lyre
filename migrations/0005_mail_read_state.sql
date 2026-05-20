-- 0005_mail_read_state.sql
-- Replace the cursor-based "I've processed up to msg N" with per-message
-- read state. Priorities mean agents don't read in monotonic order —
-- a cursor is the wrong shape. Each row has its own read_at.
--
-- Also add `title` so listings can show a meaningful subject line without
-- carrying full body bytes through the prompt every wakeup.
--
-- Cleanly cuts the old `last_processed_msg_id` column from `mailboxes`.
-- The Phase 0 dedup cursor `last_auto_triggered_msg_id` (stored inside
-- mailboxes.metadata as JSON) stays — it's a scheduler-side anti-loop
-- knob, not an agent-side read pointer.

-- ---------------------------------------------------------------------------
-- mailbox_messages
-- ---------------------------------------------------------------------------

-- Per-message read state. NULL = unread. Set by `mailbox_read` (auto-mark)
-- or explicit `mark_read`. Owner-side mailbox stays NULL (owner is a human,
-- not addressed by this).
ALTER TABLE mailbox_messages ADD COLUMN read_at TEXT;

-- Subject-line summary for inbox listings. Written at send time:
--   - explicit `title` param if the sender supplied one (≤140 char)
--   - first non-empty line of body, truncated to 140 char, otherwise.
-- Listings show `title` only; full body is fetched via mailbox_get_message.
ALTER TABLE mailbox_messages ADD COLUMN title TEXT;

-- Partial index: serving the common "give me my unread mail" query.
-- Sort hint: urgency-then-id is the read order we want (blocker first).
CREATE INDEX IF NOT EXISTS mailbox_messages_unread
  ON mailbox_messages(recipient, urgency, id) WHERE read_at IS NULL;

-- ---------------------------------------------------------------------------
-- scheduled_mail
-- ---------------------------------------------------------------------------
-- Writer's explicit title must survive the delay until delivery, so the
-- scheduled_mail row also carries title (delivered through to the
-- materialized mailbox_messages.title at fire time).
ALTER TABLE scheduled_mail ADD COLUMN title TEXT;

-- ---------------------------------------------------------------------------
-- mailboxes — drop last_processed_msg_id
-- ---------------------------------------------------------------------------
-- SQLite supports DROP COLUMN since 3.35. The Phase 0 cursor lives in the
-- metadata JSON column already (added by Q4 work); leaving it alone.
ALTER TABLE mailboxes DROP COLUMN last_processed_msg_id;
