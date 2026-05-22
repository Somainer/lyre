-- Lyre migration 0005: mail reactions.
--
-- A reaction is a lightweight ack-shaped marker an agent leaves on a
-- specific mail message — "I saw this, no further action needed". Unlike
-- a reply (which would be a new mailbox_messages row, count toward
-- recipient unread, and trigger Phase 0 auto-wake), a reaction:
--   • does NOT create a mailbox_messages row
--   • does NOT affect count_unread / read_unread / Phase 0
--   • is visible to the original sender via mailbox_get_message and the
--     dashboard's mail-detail view
--
-- Designed to break the handshake-storm pattern where two polite agents
-- ping-pong "收到 / closing" replies indefinitely. Either side can land
-- the loop by issuing a reaction instead of a reply.
--
-- The CHECK constraint is intentionally tight (only 'ack' for now). To
-- add a new reaction kind, write a follow-up migration that drops + recreates
-- the table with the broader CHECK — forces a deliberate decision rather
-- than a kind-vocabulary creep.

CREATE TABLE IF NOT EXISTS mail_reactions (
  msg_id       INTEGER NOT NULL REFERENCES mailbox_messages(id),
  reactor      TEXT NOT NULL REFERENCES agents(id),
  kind         TEXT NOT NULL CHECK (kind IN ('ack')),
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (msg_id, reactor, kind)
);

-- Lookup path used by `get_message(msg_id)` to attach reactions:
-- "all reactions for this msg, newest-first".
CREATE INDEX IF NOT EXISTS mail_reactions_msg_id
  ON mail_reactions(msg_id, created_at);
