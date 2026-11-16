# twitter-bookmarks

Index your X (Twitter) bookmarks into Cloudflare D1, then search and chat over them.

X hides your bookmarks behind an endless scroll and gives you no way to export or search them. A launchd
agent on the Mac drives your logged-in Chrome through [browse](https://opencode.ai/browse), captures the
GraphQL responses X itself loads, and pushes each post to a Worker. The Worker serves an HTMX interface
behind Cloudflare Access.

## Status

**Spike complete.** The four risky parts are proven locally:

| Part | State |
|---|---|
| FastAPI inside a Cloudflare Python Worker | works |
| Jinja2 templates served from Workers Static Assets | works |
| D1 with FTS5, `MATCH` and `bm25()` | works |
| Idempotent `/ingest` and HTMX `/search` partials | works |
| browse capture of X's `Bookmarks` GraphQL | works: 40 posts from 2 pages, author/text/media/cursors all present |
| Password login with a signed cookie | works |
| Hybrid search (FTS5 + bge-m3, RRF) with vector fallback | works, floor 0.45 |
| Chat with sessions in D1, glm-5.3 + deepseek fallback | works |

Not built yet: the tool loop, streaming, tags, unread state, OCR, threads, UI design.

## Architecture

```
Mac (awake)                                  Cloudflare
┌──────────────────────────┐                 ┌───────────────────────────────┐
│ launchd, hourly          │  POST /ingest   │ Worker: FastAPI + Jinja2      │
│ python + browse          │  Bearer token   │  ├─ D1: posts, posts_fts      │
│ scrape → batch 25 posts  │ ──────────────► │  ├─ Static Assets: templates  │
└──────────────────────────┘                 │  └─ Worker secret: model key  │
                                             │                               │
     you ─────── Cloudflare Access (OTP) ──► │  /  /search  /ingest  /health │
                                             └───────────────────────────────┘
```

## Local development

Needs Node.js, `uv`, and Python 3.13 or newer.

```bash
uv sync                                  # fastapi, jinja2, workers-py
cp .dev.vars.example .dev.vars           # INGEST_TOKEN=dev-token
npx wrangler d1 execute DB --local --file=schema.sql
uv run pywrangler dev                    # http://localhost:8787
```

`pywrangler` requires `uv` 0.13.4 or newer. This repo installs `uv` in `.venv` and prepends it, so the
Homebrew `uv` on the machine stays untouched:

```bash
PATH="$PWD/.venv/bin:$PATH" uv run pywrangler dev
```

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Feed of the newest 20 bookmarks |
| `GET` | `/search?q=` | HTMX partial, FTS5 `MATCH` with `bm25()` order |
| `POST` | `/ingest` | Batch upsert, max 25 posts, `Authorization: Bearer $INGEST_TOKEN`, returns `{stored, new}` |
| `GET` | `/health` | Post, FTS row and pending-embedding counts |
| `GET` | `/chat` | Chat page; `?c=<id>` loads a session, `?new=1` starts one |
| `POST` | `/chat` | No-JS path: answers, stores, then redirects to the conversation |
| `POST` | `/chat/stream` | Same loop, as Server-Sent Events: `status`, `tool`, `answer`, `error` |
| `POST` | `/chat/{id}/delete` | Delete a conversation and its messages |
| `POST` | `/reindex` | Embed posts where `embedded_at IS NULL` |
| `GET` | `/debug/search?q=` | BM25, cosine and fused scores, for tuning |

## The sync agent

`agent/sync.py` drives your real Chrome: it opens `/i/bookmarks`, captures the GraphQL bodies X fetches,
parses them, and posts each page. It stops as soon as a page brings nothing new.

```bash
TB_API=https://twitter-bookmarks.umaralikhan1299.workers.dev ./agent/sync.py             # hourly shape
TB_API=https://twitter-bookmarks.umaralikhan1299.workers.dev ./agent/sync.py --deep      # 40 pages
./agent/sync.py --dry-run --save-raw /tmp/raw      # capture and parse, post nothing
```

The token is read from `~/.config/twitter-bookmarks/token` (mode 600) or `INGEST_TOKEN`.
State lands in `~/.local/state/twitter-bookmarks/state.json`.

**Transport detail:** `browse execute` truncates its result near 100 KB, so `agent/capture.js` writes each
page body with `files.write()` into the temp dir and returns only paths. Python reads and deletes them.

Hourly job: `launchd/com.umaralikhan.twitter-bookmarks.sync.plist`, installed at
`~/Library/LaunchAgents/`, log at `~/Library/Logs/twitter-bookmarks-sync.log`.

```bash
launchctl print gui/$(id -u)/com.umaralikhan.twitter-bookmarks.sync | head -20
tail -20 ~/Library/Logs/twitter-bookmarks-sync.log
```

Ingest body:

```json
{
  "posts": [
    {
      "id": "1234567890123456789",
      "url": "https://x.com/handle/status/1234567890123456789",
      "handle": "handle",
      "author": "Display Name",
      "text": "post text, with the full note tweet when X provides one",
      "posted_at": "2026-11-01T10:00:00.000Z",
      "raw": "{...}"
    }
  ]
}
```

`id` is the key. A repeat insert updates text and keeps the original `first_seen_at`.

## Chat

The model runs a small tool loop (max 3 rounds) instead of one retrieval shot:

| Tool | Purpose |
|---|---|
| `search_bookmarks` | hybrid FTS5 + vector (RRF) |
| `vector_search` | meaning only |
| `sql_query` | one read-only `SELECT`/`WITH` against D1 — counts, dates, grouping, filters |
| `get_bookmark` | one post in full |

`glm-5.3` answers; `deepseek-v4.1-flash` takes over if the primary errors. Every request carries
`x-opencode-session: tb-<conversation id>`; OpenCode Go rejects calls without it. Answers cite posts as `[n]`,
and only cited posts appear as chips. Each stored message keeps `tool_trace` so you can inspect what ran.

`sql_query` is guarded: one statement, `SELECT`/`WITH` only, a blocklist of write and pragma verbs, 50 rows,
300 characters per field, `raw` stripped.

The page streams progress over SSE: `status` → `tool` (one per call) → `answer`. The browser appends the user
bubble and a pending assistant bubble, then replaces the pending one with the returned HTML. Chats can be
deleted from the sidebar (hover a title, press ×).

Sessions live in D1: `conversations` and `messages` (role, content, sources JSON, model). A new chat is named
after its first question.

## Secrets

| Name | Purpose |
|---|---|
| `INGEST_TOKEN` | Bearer token for `POST /ingest`, read by the sync agent |
| `APP_PASSWORD_HASH` | PBKDF2 hash; rotate with `scripts/password.py` |
| `SESSION_SECRET` | Signs the `tb_session` cookie |
| `GO_API_KEY` | OpenCode Go key for chat |

Set them with `printf '%s' "$VALUE" | npx wrangler secret put NAME` — no trailing newline, or login breaks.

## Data rules

- **No bookmark timestamp exists.** X does not expose when you saved a post. `first_seen_at` is recorded
  instead and must be labelled as such in the UI.
- **Posts are never deleted locally.** A re-sync that finds a post gone sets `gone = 1`; `raw` keeps the
  original payload so the text survives deletion on X.
- **The X session never leaves the Mac.** Only post text and metadata go to Cloudflare.

## Deploy

```bash
npx wrangler d1 create twitter-bookmarks     # put the id in wrangler.jsonc
npx wrangler d1 execute DB --remote --file=schema.sql
npx wrangler secret put INGEST_TOKEN
PATH="$PWD/.venv/bin:$PATH" uv run pywrangler deploy
```

## Layout

```
src/main.py                      FastAPI app, D1 helpers, FTS query builder
schema.sql                       posts + posts_fts
agent/sync.py                    capture, parse, post, state
agent/capture.js                 the browse script that walks the list
agent/parse.py                   GraphQL payload -> posts
src/main.py                      FastAPI: pages, chat, search, auth, embeddings
migrations/                      SQL applied with wrangler d1 execute
scripts/password.py              password hash maker
launchd/                         hourly job template
public/templates/                Jinja2: base, index, partials/results
public/htmx.min.js               vendored HTMX 2.0.4
wrangler.jsonc                   Worker, assets, D1 binding
```
