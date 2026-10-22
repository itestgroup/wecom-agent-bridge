CREATE TABLE IF NOT EXISTS kf_notifications (
  id TEXT PRIMARY KEY,
  open_kfid TEXT NOT NULL,
  sync_token TEXT NOT NULL,
  event_time INTEGER NOT NULL,
  received_at TEXT NOT NULL,
  acked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_kf_pending
ON kf_notifications(received_at, id) WHERE acked_at IS NULL;
