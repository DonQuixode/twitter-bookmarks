# decisions.md

A running log of decisions, why they were made, and what is still open. Newest open items at the bottom.
Status values: **decided**, **open**, **rejected**.

---

## D1 — Hosting shape: Cloudflare-native — decided

Python Worker (FastAPI) + D1 + Static Assets, behind Cloudflare Access.

*Why:* keeps Python and HTMX, keeps SQLite semantics and FTS5, no server to patch, app stays up when the Mac
sleeps. Python Workers are GA and D1 ships FTS5, which removed the two reasons to hesitate.

*Rejected:* Cloudflare Tunnel to the Mac (app dies when the lid closes); Python on Fly/Railway behind
Cloudflare (two vendors for one hobby app); TypeScript backend (drops Python, no gain).

## D2 — Ingestion: capture X's own GraphQL responses — decided

Drive the logged-in Chrome with browse, start a network capture filtered to `/graphql/`, scroll
`/i/bookmarks`, and parse the JSON bodies X already fetched.

*Why:* real structured data, including note tweets, entities, media, and cursors. No cookie export, no
`x-client-transaction-id` forging, no HTML scraping. The browser adds the headers; we only read the answers.

*Rejected:* DOM scraping (text truncation, lost quote/thread structure); replaying the API from Python
(needs cookie decryption and transaction-id generation).

*Constraint:* the sync can never leave the Mac without exporting the X session. Accepted.

**Verified on the live account (spike, 2026-11-16).** Two pages captured, 20 posts each, zero parse errors:

| Fact | Value |
|---|---|
| Endpoint | `/i/api/graphql/<id>/Bookmarks?variables={"count":20,...}` |
| Response body | `exchange.response.body.text` (a JSON string), 160 KB per page, `truncated: false` |
| Posts per page | 20 |
| Author handle | `core.user_results.result.core.screen_name` (**not** `.legacy.screen_name`) |
| Author id | `core.user_results.result.rest_id` |
| Text | `legacy.full_text`; long posts in `note_tweet.note_tweet_results.result.text` |
| Timestamp | `legacy.created_at`, format `Mon Nov 16 17:07:53 +0000 2026` |
| Bookmark flag | `legacy.bookmarked: true` (no bookmark **date** anywhere — confirms D6) |
| Pagination | timeline entries with `cursorType` `Top` and `Bottom` |
| URL oddity | `/i/bookmarks` serves at `/i/history`; the tab is still Bookmarks |
| Also present | `quoted_status_result`, `extended_entities.media`, `conversation_id_str`, `in_reply_to_status_id_str`, `lang` |

*Capture limits that shape the agent:* 1 MB per body, 25 MB per capture. At 160 KB per page that is ~150 pages
per capture, so batch the walk (about 30 pages per run) and restart the capture. Three scrolls produced only one
new page, so scroll further and wait longer between steps than instinct suggests.

## D3 — Storage: D1 with FTS5 — decided

`posts` keyed by post id, plus a standalone `posts_fts` (unicode61, diacritics removed) ranked by `bm25()`.

*Consequences:* 2 MB per row, 100 bound params per query, single-threaded writes. Ingest batches at 25 posts.

## D4 — Auth: Cloudflare Access with email OTP — decided

One-time PIN to `umaralikhan0712@gmail.com`. No password table, no session code of our own.

*Ingest* uses a separate bearer token in a Worker secret, so the Mac agent never needs an Access session.

## D5 — Chat model: OpenCode Go, glm-5.3 with a fallback — decided, shipped

Endpoint `https://opencode.ai/inference/go/openai/v1/chat/completions`, key in the Worker secret `GO_API_KEY`.

| Setting | Value |
|---|---|
| Primary | `deepseek-v4.1-flash` |
| Fallback | `glm-5.3` (used when the primary errors, is empty, or empties its budget) |
| Context | 1,000,000 tokens each; both declare tool support |
| Output price | $0.60/M (deepseek) vs $4.40/M (glm) |

*Owner's call, 2026-11-17:* deepseek leads because it is 7x cheaper per output token and its answers were
good in the tool loop; glm stays as the safety net when deepseek errors or returns nothing.

Three API facts that cost debugging time:

1. **`x-opencode-session` is required.** Without it: HTTP 400 `MissingSessionID`. We send `tb-<conversation id>`,
   which also keeps routing and prompt cache stable per conversation.
2. **glm-5.3 reasons before it answers.** With `max_tokens: 20` it returned `content: null` and a `reasoning`
   field. Budget is 900 tokens; the answer is refused if reasoning eats everything.
3. The endpoint is OpenAI-compatible, so no SDK is needed — plain `fetch` with a JSON body.

## D6 — No bookmark timestamp exists — decided (constraint, not a choice)

X exposes when a post was written, never when you saved it. `first_seen_at` is stored and the UI must say
"first seen", not "saved". Ordering by "recently bookmarked" is impossible.

---

# Open items requested by the owner

## D7 — OCR for media — open

Many bookmarked images are screenshots of code, charts, slides, or article text. That text is invisible to
search today.

*Proposed:* at ingestion, run OCR over each post's images and store `media_text` in `posts_fts`.
*Options:* Workers AI vision model (runs in the Worker, costs CPU per image); a vision LLM through OpenCode
Go (better on charts, slower, paid); local OCR on the Mac during sync (macOS Vision, free, no Worker limits,
keeps images off Cloudflare).
*Questions:* all images or only likely-screenshot ones? store the extracted text only, or the image too?
video frames at all? what about images that are memes with no useful text?
*Leaning:* OCR on the Mac during sync (free, no upload of media), store text only.

## D8 — Reading tweet chains and threads — open

A bookmarked reply or mid-thread post is useless alone. We store one post; the context is on X.

*Proposed:* store `conversation_id` and `in_reply_to_id`, and fetch the chain (ancestors + following
replies by the same author) once, on demand or at ingestion, into `thread_parts`.
*Needs:* a cap on chain length, handling deleted ancestors, and a reading-time estimate from the whole chain.
*Questions:* fetch every chain at ingestion (more requests, slower sync), or lazily when opened?
Store the chain as separate rows, or one assembled text blob for search?

## D9 — Frontend design — open

Server-rendered HTMX does not mean ugly. This needs actual design work, not default browser styles.

*To decide:* reading-first layout (the post, not the app chrome); density for scanning many bookmarks;
keyboard navigation (`j`/`k`, `/` to search, `o` to open); filters as visible chips, not a hidden panel;
mobile behavior; dark mode; an explicit empty state; loading states that HTMX can actually render.
*Constraint:* no build step. Plain CSS, one stylesheet, design tokens as CSS custom properties.

## D10 — A decision model — open, needs clarification

"Decision model" can mean three different things. All three are plausible; they are not the same work:

1. **Triage classifier** — given a bookmark, predict "read now / skim / skip" from text, author, length, tags.
2. **Ranking model** — order the list by expected value to you (learns from your read/skip behavior).
3. **Decision log** — a record of what you decided about each bookmark (read, skipped, archived), which then
   feeds 1 and 2.

*Leaning:* start with 3, because it generates the data 1 and 2 need, and it is a table plus three buttons.
Do not train anything before there is data.

## D11 — RAG with Cloudflare's own embeddings — open

*Proposed:* Workers AI embedding model (`@cf/baai/bge-m3` or the current equivalent) on ingest, vectors in
Vectorize, hybrid retrieval — FTS5 BM25 for exact words, vectors for meaning — then pass the top 8 to the
chat model with citations back to post ids.
*Needs:* chunk unit (one post, or a whole thread), dimension choice, re-embed policy when a post text changes,
and a cost estimate at the expected bookmark count.
*Leaning:* embed one post per vector; embed the assembled thread text when D8 lands; keep BM25 as the primary
path until the corpus is big enough to need vectors.

## D13 — Chat shape: own tool loop — decided, shipped

No framework. A hand-written loop with four tools, three rounds maximum, and a forced answer on the last turn.

| Tool | Does | Notes |
|---|---|---|
| `search_bookmarks` | hybrid FTS5 + vector, RRF | the RAG step, now chosen by the model |
| `vector_search` | meaning only | for when the user's words will not appear in any post |
| `sql_query` | one read-only SELECT/WITH on D1 | counts, dates, handles, grouping |
| `get_bookmark` | one post in full by id | follow-up on a search hit |

*Why not LangChain Deep Agents:* it optimises long-horizon planning with subagents and a virtual filesystem.
This task is one database and four lookups. It would add several MB to a 10 MB Pyodide bundle, a slower cold
start, and extra model calls per question. Revisit only if we add article extraction and multi-document reports.

**SQL safety** (`sql_guard`): one statement only, must start with `select` or `with`, a blocklist of
`pragma/attach/insert/update/delete/drop/alter/create/vacuum/...`, 2000-character cap, results trimmed to 50
rows and 300 characters per field, `raw` never returned. The model sees the schema in the tool description.

**Citations:** every post that enters the loop gets a stable number, so `[n]` in the answer matches the chip in
the UI. Only cited posts are shown as chips.

**Trace:** each answer stores `tool_trace` (tool, arguments, result summary) and renders it in a collapsed
`<details>` block. When an answer is wrong, you can see why.

## D14 — Progress streaming: SSE events, not tokens — decided, shipped

`POST /chat/stream` returns `text/event-stream`. The browser reads it with `fetch` + `TextDecoder` and swaps the
pending bubble when the answer arrives.

| Event | Payload |
|---|---|
| `status` | "Searching your bookmarks..." |
| `tool` | "Running sql_query..." |
| `answer` | the rendered HTML for the user + assistant messages |
| `error` | a short message, never a hang |

Measured on the live Worker: status at 1 s, tool at 11 s, answer at 16 s. Before streaming the first visible
pixel was at 16 s.

*Why events and not tokens:* the loop waits on tool calls, and glm-5.3 emits reasoning tokens before content.
Streaming raw deltas would show the model thinking about SQL, not the answer. Event-level progress is what
removes the dead air.

*Implementation notes:* `asyncio.Queue` + `asyncio.create_task` with an `emit` callback passed into
`agent_answer`; `StreamingResponse` does stream from the Workers ASGI adapter (verified with a 4-tick probe).
Also confirmed: `POST /chat` still answers without JavaScript (stores, then redirects).

## D15 — Save media, and OCR the heavy posts — open (owner's rule, work later)

For every bookmarked post that has media: **save the media**, and **OCR it** when either

- the post has a video, or
- the post has more than 2 images.

Rationale: a bookmark is worth keeping readable after X deletes the post, and image text (code screenshots,
slides, charts) is invisible to search today.

*Open questions to settle before building:*
1. **Where.** R2 bucket (no bucket exists yet); D1 is for text only, 2 MB per row.
2. **Videos are not OCR.** "OCR a video" needs a frame grab (poster or N frames) before OCR, or a transcript
   instead. Pick one: poster frame, fixed frames every N seconds, or audio transcription.
3. **Which images.** Only the `>2 images` posts, or every image? The rule above says every image gets saved,
   OCR only for the heavy cases.
4. **Search integration.** OCR text goes into `posts_fts` (as a separate column or appended text) and into the
   embedding chunks (D11)? If appended, re-embedding is required.
5. **Retention and size.** Media accumulates without limit; a per-post cap and a total-size budget are needed.

Related: D7 (OCR for media) and D11 (embeddings). This entry supersedes the "leaning" in D7.

---

## D12 — Capture walk policy — open, informed by the spike

Scrolling is the bottleneck, not parsing. Three wheel events produced one new page. The agent must scroll,
wait for the `Bottom` cursor to change, and stop on no-change rather than on a fixed count. Early stop after
N known ids still applies, checked per page — and it works: a second run reported `new: 0` and
`stopped_because: nothing-new`.

**Measured (first production runs):** 40 scroll steps produced 15 pages, 300 posts, in 180 s. That is about
2.7 steps per page, so a walk of the whole list needs roughly 2.7 x (bookmarks / 20) steps. The hourly job
uses 4 steps (about 1 page over the catch-up point); the deep pass uses 40.

Hourly launchd run, newest first, early stop after 60 consecutive known ids; weekly deep pass; a missed run
while the lid is closed catches up on wake. The UI shows "last synced" and marks stale data.

---

## D16 — Tool discipline: stop when the answer is in hand — CRITICAL, open

Recorded from a real failure. The owner asked **"can you give me all posts by Ben Dicken?"** and got
unrelated people in the citations. This is the most damaging class of bug in the product: a confident,
wrong answer about the owner's own data. It is critical, not cosmetic.

### Correction to the first diagnosis

The first reading of this failure was "the tool cannot express the question". **That was wrong**, and the
stored traces prove it. `sql_query` reaches the whole schema. The model used it correctly.

### What actually happened (from `messages.tool_trace` in production)

```
Q: can you give me all posts by Ben Dicken?
  1. sql_query  {SELECT id, handle, author, substr(text,1,120) FROM posts
                 WHERE author LIKE '%Dicken%' OR handle LIKE '%dicken%'}
     -> 7 rows                                    exact rows, correct answer
  2. search_bookmarks {"query": "Ben Dicken", "limit": 20}
     -> 20 posts via vector                       unrelated people, not needed
  answer cites rows from call 2

Q: all posts by @BenjDicken
  1. sql_query  {SELECT handle, author, COUNT(*) ... WHERE LOWER(author) LIKE '%dicken%'}
     -> 1 row
  2. search_bookmarks {"query": "Ben Dicken", "limit": 15} -> 15 posts via vector
  3. sql_query  {SELECT id, posted_at, text FROM posts WHERE handle='BenjDicken'}
     -> 7 rows
```

Three observed facts:

1. **The model ran a second search it did not need.** Call 1 already answered the question.
2. **The vector call returned rows for a query that has no meaning component.** "Ben Dicken" is a name;
   BM25 scores 0, the fallback fires, bge-m3 returns neighbours by meaning, and those rows are noise.
3. **Noise entered the citation registry.** The `[n]` numbers are assigned across all tool results in the
   turn, so rows from call 2 sat next to rows from call 1 with equal standing. The model cited them.

### The lesson

**Capability was never the problem. Discipline was.** The prompt says *"search_bookmarks: start here for
topic questions"* and the model obeys that habit even after an exact result set is on the table. Nothing
in the code, the prompt, or the result text said *"you already have the answer, stop"*, and nothing marked
the vector rows as approximations.

So the fix is not a schema change. It is, in order:

1. **A stop rule the code enforces.** If a tool returns rows that satisfy an exact constraint, the next
   round must not offer search tools at all. Prompt rules are advisory; the tool list is not.
2. **Result text that states its own authority.** `"7 rows (exact match on author)"` versus
   `"15 approximate neighbours by meaning — not matches, do not cite as the answer."`
3. **A prompt rule to match.** "One good call is enough. If the result answers the question, answer."
4. **Citation scoping.** The model must cite only rows from the call that produced its answer.

### Known failure shapes

| Shape | Example | Correct path | Observed |
| --- | --- | --- | --- |
| Exact author | "all posts by Ben Dicken" | `sql_query`, then stop | correct SQL, then a needless vector call, then cites the noise |
| Count / aggregate | "how many in November" | `sql_query` | ok |
| Listing | "show me everything about X" | hybrid | ok |
| Conceptual | "posts about scaling pain" | vector, then hybrid | ok |
| One known post | "what does the Varnan post say" | `get_bookmark` | ok |
| Negation / absence | "did I save anything about Zig" | must be allowed to say no | vector always returns rows |

### Options considered

- **Enforced stop + result labelling + citation scoping** — recommended. No migration, no new dependency.
- **Author/date filters on `search_bookmarks`** — still useful for latency and for honest "no result",
  but **not** required for correctness: SQL already covers it. Demoted from the original proposal.
- **Deterministic pre-router** (regex on `by @handle`) — defer; becomes a second rule engine.
- **Planner call** — rejected; adds a round trip to do what one call already did.

### Acceptance test

"All posts by Ben Dicken" must return exactly the 7 rows with `handle='BenjDicken'`, and the trace must
contain **no** `search_bookmarks` or `vector_search` call after the exact result. Add it, plus the other
shapes above, to a routing eval set that runs before each deploy.

