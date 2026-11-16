-- Schema for the bookmark index.
-- Apply with: npx wrangler d1 execute DB --local --file=schema.sql
--        prod: npx wrangler d1 execute DB --remote --file=schema.sql

CREATE TABLE IF NOT EXISTS posts (
  id            TEXT PRIMARY KEY,          -- post id, the natural key
  url           TEXT NOT NULL,
  handle        TEXT NOT NULL DEFAULT '',
  author        TEXT NOT NULL DEFAULT '',
  text          TEXT NOT NULL DEFAULT '',
  posted_at     TEXT,                      -- ISO 8601, from X
  first_seen_at TEXT NOT NULL,             -- our proxy for "bookmarked at"
  gone          INTEGER NOT NULL DEFAULT 0,-- post deleted or protected later
  embedded_at   TEXT,                      -- NULL means: needs embedding
  raw           TEXT                       -- original payload, kept forever
);

CREATE INDEX IF NOT EXISTS posts_posted_at ON posts (posted_at DESC);
CREATE INDEX IF NOT EXISTS posts_handle ON posts (handle);
CREATE INDEX IF NOT EXISTS posts_embedded ON posts (embedded_at);

-- Standalone FTS index: post_id is stored but not tokenised.
CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5 (
  post_id UNINDEXED,
  text,
  handle,
  tokenize = 'unicode61 remove_diacritics 2'
);

-- Chat
CREATE TABLE IF NOT EXISTS conversations (
  id         TEXT PRIMARY KEY,
  title      TEXT NOT NULL DEFAULT 'New chat',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL,
  role            TEXT NOT NULL,
  content         TEXT NOT NULL,
  sources         TEXT,
  model           TEXT,
  tool_trace      TEXT,
  created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS messages_conversation ON messages (conversation_id, id);
