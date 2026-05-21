-- Multimodal: blob storage for images/documents attached to mail.
--
-- Bytes live on disk at ${object_store}/blobs/<sha256>.<ext> (content-
-- addressed, immutable, dedup'd for free). This table is the metadata
-- index — `mailbox_messages.attachments` carries a JSON list of blob_ids
-- and the runtime joins back here for media_type / size lookups.
--
-- No FK from mailbox_messages.attachments → blobs(id) because the column
-- is JSON; integrity is enforced at the tool layer (mailbox_send refuses
-- unknown blob_ids).

CREATE TABLE IF NOT EXISTS blobs (
  id           TEXT PRIMARY KEY,         -- sha256 hex of the file contents
  media_type   TEXT NOT NULL,            -- e.g. 'image/png', 'application/pdf'
  size_bytes   INTEGER NOT NULL,
  -- Original filename when uploaded via dashboard. Optional, no semantic
  -- meaning — just preserved for human display ("screenshot.png" beats
  -- raw hash in a mail-detail page). NULL for blobs that arrive without
  -- a filename (tool returns, future cases).
  filename     TEXT,
  -- Who originated this blob — same shape as mailbox.sender / agent.id.
  -- Audit trail: "where did this image come from?". Owner uploads use
  -- 'owner'; future tool-returned blobs would tag the agent that ran
  -- the tool.
  source       TEXT NOT NULL,
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS blobs_created ON blobs(created_at);

-- Attachments column on mail. JSON list of blob_ids (strings); NULL or
-- '[]' means "no attachments". Kept on the same row as the body so a
-- single mailbox_get_message returns everything an adapter needs to
-- assemble the user message — no extra round-trip.
ALTER TABLE mailbox_messages ADD COLUMN attachments TEXT;
