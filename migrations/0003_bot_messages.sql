CREATE TABLE IF NOT EXISTS bot_messages (
  id TEXT PRIMARY KEY,
  bot_id TEXT NOT NULL,
  msgid TEXT NOT NULL,
  from_user TEXT NOT NULL,
  chat_id TEXT NOT NULL,
  msg_type TEXT NOT NULL,
  content TEXT NOT NULL,
  attachments TEXT NOT NULL,
  received_at TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  acked_at TEXT,
  reply_content TEXT,
  replied_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_bot_pending
ON bot_messages(received_at, id) WHERE acked_at IS NULL;
