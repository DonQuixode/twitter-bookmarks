-- Chat sessions and messages.
CREATE TABLE IF NOT EXISTS conversations (
  id         TEXT PRIMARY KEY,
  title      TEXT NOT NULL DEFAULT 'New chat',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL,
  role            TEXT NOT NULL,             -- 'user' or 'assistant'
  content         TEXT NOT NULL,
  sources         TEXT,                      -- JSON list of {id,handle,url,posted_at}
  model           TEXT,                      -- which model answered
  created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS messages_conversation ON messages (conversation_id, id);
