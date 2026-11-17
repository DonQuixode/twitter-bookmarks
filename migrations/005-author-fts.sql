-- D20: index the display name. "Ben Dicken" lives in posts.author and was invisible to search.
DROP TABLE IF EXISTS posts_fts;

CREATE VIRTUAL TABLE posts_fts USING fts5 (
  post_id UNINDEXED,
  text,
  handle,
  author,
  tokenize = 'unicode61 remove_diacritics 2'
);

INSERT INTO posts_fts (post_id, text, handle, author)
SELECT id, COALESCE(text, ''), COALESCE(handle, ''), COALESCE(author, '') FROM posts;
