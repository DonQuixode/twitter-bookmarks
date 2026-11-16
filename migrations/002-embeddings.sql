-- Adds the embedding marker. D1 has no ADD COLUMN IF NOT EXISTS, so run this once.
ALTER TABLE posts ADD COLUMN embedded_at TEXT;
CREATE INDEX IF NOT EXISTS posts_embedded ON posts (embedded_at);
