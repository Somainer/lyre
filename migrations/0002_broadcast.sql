-- Lyre 0002: mailbox broadcast support.
--
-- Adds broadcast_id (groups copies of one multi-recipient send) and
-- recipients_all (JSON list of every recipient on the broadcast, so each
-- agent reading the message knows who else is on the thread).
--
-- SQLite has no `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`; idempotency is
-- handled by the runner consulting schema_migrations before applying this
-- file.

ALTER TABLE mailbox_messages ADD COLUMN broadcast_id TEXT;
ALTER TABLE mailbox_messages ADD COLUMN recipients_all TEXT;

CREATE INDEX IF NOT EXISTS mailbox_messages_broadcast
  ON mailbox_messages(broadcast_id);
