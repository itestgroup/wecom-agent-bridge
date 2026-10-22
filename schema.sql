CREATE TABLE IF NOT EXISTS inbox_messages (
  id TEXT PRIMARY KEY,
  msgid TEXT NOT NULL,
  corp_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  from_user TEXT NOT NULL,
  msg_type TEXT NOT NULL CHECK (msg_type = 'text'),
  content TEXT NOT NULL,
  msg_time INTEGER NOT NULL,
  received_at TEXT NOT NULL,
  acked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_inbox_pending
  ON inbox_messages(received_at, id) WHERE acked_at IS NULL;
