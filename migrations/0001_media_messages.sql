-- Additive migration: keeps the existing text table and all ACK history intact.
CREATE TABLE IF NOT EXISTS inbox_media_messages (
  id TEXT PRIMARY KEY,
  msgid TEXT NOT NULL,
  corp_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  from_user TEXT NOT NULL,
  msg_type TEXT NOT NULL CHECK (msg_type IN ('image', 'file')),
  media_id TEXT NOT NULL,
  filename TEXT NOT NULL DEFAULT '',
  file_size INTEGER,
  msg_time INTEGER NOT NULL,
  received_at TEXT NOT NULL,
  acked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_media_pending
  ON inbox_media_messages(received_at, id) WHERE acked_at IS NULL;
