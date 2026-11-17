"""twitter-bookmarks: index X bookmarks into D1, then search and chat over them.

Runs as a Cloudflare Python Worker (FastAPI through the Workers ASGI adapter).

Auth: a password (PBKDF2 hash in a Worker secret) sets an HMAC-signed session cookie.
Search: hybrid FTS5 + vector (RRF), falling back to vector-only when hybrid finds nothing.
Chat: glm-5.3 through OpenCode Go, deepseek-v4.1-flash as fallback, sessions in D1.
"""

import base64
import binascii
import hashlib
import html
import hmac
import asyncio
import json
import re
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from jinja2 import DictLoader, Environment, select_autoescape
from markupsafe import Markup
from js import Object, fetch as js_fetch
from pyodide.ffi import to_js
from workers import asgi

app = FastAPI(title="twitter-bookmarks")

TEMPLATE_NAMES = (
    "base.html",
    "index.html",
    "login.html",
    "chat.html",
    "partials/results.html",
    "partials/messages.html",
)
_template_cache: dict[str, str] = {}

SESSION_COOKIE = "tb_session"
SESSION_SECONDS = 30 * 24 * 3600
PUBLIC_PATHS = {"/login", "/logout", "/ingest", "/eval/ask", "/health", "/htmx.min.js"}

EMBED_MODEL = "@cf/baai/bge-m3"
VECTOR_DIMS = 1024
# Cosine floor for a semantic neighbour to join a hybrid result. Below it, the meaning match is noise.
HYBRID_VECTOR_FLOOR = 0.45
RRF_K = 60

GO_BASE = "https://opencode.ai/inference/go/openai/v1"
CHAT_MODEL = "deepseek-v4.1-flash"
CHAT_FALLBACK = "glm-5.3"
CHUNK_CHARS = 1000
CHUNK_OVERLAP = 200
PREVIEW_CHARS = 500
# One tool message is JSON-capped; this leaves room for the wrapper and escaping.
FULL_TEXT_CHARS = 8000
VECTOR_TOP_K = 30

_login_failures: list[float] = []
LOGIN_WINDOW = 300
LOGIN_LIMIT = 8


# ---------------------------------------------------------------- plumbing


def env_of(request: Request):
    return request.scope["env"]


def secret_of(request: Request, name: str) -> str:
    return env_secret(env_of(request), name)


def env_secret(env, name: str) -> str:
    value = getattr(env, name, "")
    return "" if value is None else str(value)


def to_py(value):
    """JS proxies expose to_py(); plain values pass through."""
    return value.to_py() if hasattr(value, "to_py") else value


async def fetch_all(db, sql: str, params: tuple = ()) -> list[dict]:
    stmt = db.prepare(sql)
    if params:
        stmt = stmt.bind(*params)
    return to_py((await stmt.all()).results)


async def fetch_one(db, sql: str, params: tuple = ()) -> dict | None:
    stmt = db.prepare(sql)
    if params:
        stmt = stmt.bind(*params)
    row = await stmt.first()
    return to_py(row) if row else None


async def load_templates(env) -> None:
    for name in TEMPLATE_NAMES:
        if name in _template_cache:
            continue
        resp = await env.ASSETS.fetch(f"https://assets.local/templates/{name}")
        if resp.status != 200:
            raise RuntimeError(f"template {name} not found in assets ({resp.status})")
        _template_cache[name] = await resp.text()


async def render(request: Request, name: str, context: dict, status_code: int = 200) -> HTMLResponse:
    await load_templates(env_of(request))
    jinja = Environment(
        loader=DictLoader(_template_cache),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    jinja.filters["linkify"] = linkify
    # no-store: a deploy must never leave an old page in the browser cache.
    return HTMLResponse(
        jinja.get_template(name).render(**context),
        status_code=status_code,
        headers={"cache-control": "no-store"},
    )


# ---------------------------------------------------------------- auth


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def pbkdf2_sha256(password: bytes, salt: bytes, iterations: int, length: int = 32) -> bytes:
    """PBKDF2-HMAC-SHA256 in pure Python: Pyodide's hashlib has no pbkdf2_hmac.

    Mirrors scripts/password.py, which must stay identical.
    """
    mac = hmac.new(password, b"", hashlib.sha256)
    blocks = -(-length // 32)
    out = b""
    for index in range(1, blocks + 1):
        seed = mac.copy()
        seed.update(salt + index.to_bytes(4, "big"))
        u = seed.digest()
        acc = int.from_bytes(u, "big")
        for _ in range(iterations - 1):
            step = mac.copy()
            step.update(u)
            u = step.digest()
            acc ^= int.from_bytes(u, "big")
        out += acc.to_bytes(32, "big")
    return out[:length]


def verify_password(password: str, stored: str) -> bool:
    """stored = pbkdf2_sha256$<iterations>$<salt b64>$<hash b64>"""
    # A stray newline from a secret upload must not turn into "wrong password".
    stored = (stored or "").strip()
    try:
        algorithm, iterations, salt_b64, hash_b64 = stored.split("$")
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    try:
        salt = unb64(salt_b64)
        expected = unb64(hash_b64)
        rounds = int(iterations)
    except (ValueError, binascii.Error):
        return False
    if rounds < 1 or rounds > 2_000_000:
        return False
    actual = pbkdf2_sha256(password.encode(), salt, rounds)
    return hmac.compare_digest(actual, expected)


def sign(secret: str, payload: str) -> str:
    return b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())


def make_session(secret: str) -> str:
    payload = b64(json.dumps({"exp": int(time.time()) + SESSION_SECONDS}).encode())
    return f"{payload}.{sign(secret, payload)}"


def session_ok(secret: str, value: str) -> bool:
    if not secret or not value or "." not in value:
        return False
    payload, signature = value.rsplit(".", 1)
    if not hmac.compare_digest(sign(secret, payload), signature):
        return False
    try:
        data = json.loads(unb64(payload))
    except (ValueError, binascii.Error):
        return False
    return int(data.get("exp") or 0) > time.time()


def throttle_login() -> bool:
    """True when the caller may try again. In-memory, per isolate: a speed bump, not a wall."""
    now = time.time()
    while _login_failures and now - _login_failures[0] > LOGIN_WINDOW:
        _login_failures.pop(0)
    return len(_login_failures) < LOGIN_LIMIT


@app.middleware("http")
async def require_session(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS:
        return await call_next(request)

    if session_ok(secret_of(request, "SESSION_SECRET"), request.cookies.get(SESSION_COOKIE, "")):
        return await call_next(request)

    if request.headers.get("hx-request") == "true":
        return Response(status_code=401, headers={"HX-Redirect": "/login"})
    return RedirectResponse("/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return await render(request, "login.html", {"error": ""})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request):
    if not throttle_login():
        return await render(request, "login.html", {"error": "Too many attempts. Wait five minutes."}, status_code=429)

    # Parsed by hand: starlette's form parser needs python-multipart, which the bundle does not carry.
    raw_body = (await request.body()).decode("utf-8", "replace")
    password = (parse_qs(raw_body).get("password") or [""])[0]
    stored = secret_of(request, "APP_PASSWORD_HASH")
    secret = secret_of(request, "SESSION_SECRET")

    if not stored or not secret:
        return await render(request, "login.html", {"error": "Worker secrets are not set."}, status_code=500)

    if not password or not verify_password(password, stored):
        _login_failures.append(time.time())
        return await render(request, "login.html", {"error": "Wrong password."}, status_code=401)

    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        make_session(secret),
        max_age=SESSION_SECONDS,
        httponly=True,
        # Secure cookies are not sent over plain http, which local dev uses.
        secure=request.url.scheme == "https",
        samesite="lax",
        path="/",
    )
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# ---------------------------------------------------------------- embeddings


def chunks(text: str) -> list[str]:
    """1000 characters with 200 of overlap, so a thought cut in half is still found."""
    flat = " ".join((text or "").split())
    if not flat:
        return []
    if len(flat) <= CHUNK_CHARS:
        return [flat]
    out = []
    start = 0
    while start < len(flat):
        out.append(flat[start : start + CHUNK_CHARS])
        if start + CHUNK_CHARS >= len(flat):
            break
        start += CHUNK_CHARS - CHUNK_OVERLAP
    return out


async def embed_texts(env, texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    result = to_py(await env.AI.run(EMBED_MODEL, {"text": texts}))
    vectors = result.get("data") or result.get("response") or []
    return [[float(x) for x in vector] for vector in vectors]


async def embed_posts(env, rows: list[dict]) -> int:
    """Embed text chunks and upsert them to Vectorize. Returns how many posts were embedded."""
    pending: list[dict] = []
    texts: list[str] = []
    for row in rows:
        for index, chunk in enumerate(chunks(row.get("text") or "")):
            pending.append(
                {
                    "id": f"{row['id']}#{index}",
                    "post_id": row["id"],
                    "handle": row.get("handle") or "",
                    "posted_at": row.get("posted_at") or "",
                }
            )
            texts.append(chunk)
    if not texts:
        return 0

    vectors = await embed_texts(env, texts)
    if len(vectors) != len(pending):
        raise RuntimeError(f"embedding count mismatch: {len(vectors)} vectors for {len(texts)} chunks")

    for offset in range(0, len(pending), 50):
        batch = []
        for item, vector in zip(pending[offset : offset + 50], vectors[offset : offset + 50]):
            batch.append(
                {
                    "id": item["id"],
                    "values": vector,
                    "metadata": {
                        "post_id": item["post_id"],
                        "handle": item["handle"],
                        "posted_at": item["posted_at"],
                    },
                }
            )
        await env.VECTORS.upsert(batch)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    statements = []
    for post_id in {item["post_id"] for item in pending}:
        statements.append(env.DB.prepare("UPDATE posts SET embedded_at = ? WHERE id = ?").bind(now, post_id))
    if statements:
        await env.DB.batch(statements)
    return len({item["post_id"] for item in pending})


# ---------------------------------------------------------------- pages


URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")


def linkify(text) -> Markup:
    """Escape, then turn bare URLs into links. Safe because escaping happens first."""
    escaped = html.escape(str(text or ""))

    def replace(match: re.Match) -> str:
        url = match.group(0)
        trimmed = url.rstrip(".,;:!?)]}")
        tail = url[len(trimmed) :]
        return f'<a href="{trimmed}" target="_blank" rel="noreferrer">{trimmed}</a>{tail}'

    linked = URL_PATTERN.sub(replace, escaped)
    # The model writes light markdown; render the two forms it actually uses.
    linked = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", linked)
    linked = re.sub(r"`([^`]+)`", r"<code>\1</code>", linked)
    return Markup(linked)


def fts_query(raw: str) -> str:
    """Every token must match; the last one may be a prefix, so "ben" finds "benjdicken"."""
    words = []
    for token in (raw or "").replace('"', " ").split():
        clean = "".join(ch for ch in token if ch.isalnum() or ch in "_-")
        if clean:
            words.append(clean)
    terms = [f'"{word}"' for word in words]
    if words and len(words[-1]) >= 3:
        terms[-1] = f'"{words[-1]}"*'
    return " ".join(terms)


async def lexical_search(db, query: str) -> list[dict]:
    match = fts_query(query)
    if not match:
        return []
    return await fetch_all(
        db,
        """SELECT p.id, p.handle, p.text, p.url, p.posted_at, bm25(posts_fts) AS score
           FROM posts_fts JOIN posts p ON p.id = posts_fts.post_id
           WHERE posts_fts MATCH ?
           ORDER BY score LIMIT 30""",
        (match,),
    )


def rrf_fuse(lists: list[list[dict]], limit: int = 30) -> list[dict]:
    """Reciprocal rank fusion: rank position matters, score scales do not."""
    scores: dict[str, float] = {}
    rows: dict[str, dict] = {}
    for ranked in lists:
        for position, row in enumerate(ranked, start=1):
            post_id = row["id"]
            scores[post_id] = scores.get(post_id, 0.0) + 1.0 / (RRF_K + position)
            rows.setdefault(post_id, row)
    ordered = sorted(scores, key=lambda post_id: scores[post_id], reverse=True)[:limit]
    fused = []
    for post_id in ordered:
        row = dict(rows[post_id])
        row["rank_score"] = round(scores[post_id], 5)
        fused.append(row)
    return fused


async def hybrid_search(env, query: str) -> tuple[list[dict], str]:
    """Keywords and meaning together. Vector-only is the fallback, not the default."""
    try:
        lexical = await lexical_search(env.DB, query)
    except Exception:  # noqa: BLE001 - a broken index must not break the page
        lexical = []
    try:
        semantic = await vector_search(env, query)
    except Exception:  # noqa: BLE001 - Vectorize is remote; local dev and outages both land here
        semantic = []
    strong = [row for row in semantic if float(row.get("score") or 0.0) >= HYBRID_VECTOR_FLOOR]

    fused = rrf_fuse([lexical, strong])
    if fused:
        return fused, "hybrid"
    if semantic:
        return semantic, "vector"
    return [], "none"


async def vector_search(env, query: str) -> list[dict]:
    vectors = await embed_texts(env, [query])
    if not vectors:
        return []
    result = to_py(await env.VECTORS.query(vectors[0], {"topK": VECTOR_TOP_K, "returnMetadata": "all"}))
    matches = result.get("matches") or []

    order: list[str] = []
    scores: dict[str, float] = {}
    for match in matches:
        # Vector ids are "<post id>#<chunk>"; do not depend on metadata coming back.
        metadata = match.get("metadata") or {}
        post_id = metadata.get("post_id") or str(match.get("id") or "").split("#")[0]
        if not post_id:
            continue
        if post_id not in scores:
            order.append(post_id)
            scores[post_id] = float(match.get("score") or 0)
    if not order:
        return []

    ids = order[:30]
    placeholders = ",".join("?" * len(ids))
    rows = await fetch_all(
        env.DB,
        f"SELECT id, handle, text, url, posted_at FROM posts WHERE id IN ({placeholders})",
        tuple(ids),
    )
    by_id = {row["id"]: row for row in rows}
    out = []
    for post_id in ids:
        row = by_id.get(post_id)
        if row:
            row = dict(row)
            row["score"] = scores.get(post_id, 0.0)
            out.append(row)
    return out


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    db = env_of(request).DB
    rows = await fetch_all(
        db,
        "SELECT id, handle, text, url, posted_at FROM posts ORDER BY first_seen_at DESC LIMIT 20",
    )
    total = await fetch_one(db, "SELECT COUNT(*) AS n FROM posts")
    pending = await fetch_one(db, "SELECT COUNT(*) AS n FROM posts WHERE embedded_at IS NULL")
    return await render(
        request,
        "index.html",
        {"rows": rows, "total": (total or {}).get("n", 0), "pending": (pending or {}).get("n", 0), "q": ""},
    )


@app.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = "", mode: str = "auto"):
    env = env_of(request)
    query = (q or "").strip()
    rows: list[dict] = []
    used = "keywords"

    if query:
        if mode == "lexical":
            rows, used = await lexical_search(env.DB, query), "keywords"
        elif mode == "vector":
            rows, used = await vector_search(env, query), "vector"
        else:
            rows, used = await hybrid_search(env, query)

    return await render(
        request,
        "partials/results.html",
        {"rows": rows, "q": query, "used": used, "mode": mode},
    )


# ---------------------------------------------------------------- api


@app.get("/health")
async def health(request: Request):
    db = env_of(request).DB
    try:
        count = await fetch_one(db, "SELECT COUNT(*) AS n FROM posts")
        fts = await fetch_one(db, "SELECT COUNT(*) AS n FROM posts_fts")
        pending = await fetch_one(db, "SELECT COUNT(*) AS n FROM posts WHERE embedded_at IS NULL")
    except Exception as error:
        return JSONResponse({"ok": False, "error": str(error)}, status_code=503)
    return {
        "ok": True,
        "posts": (count or {}).get("n"),
        "fts_rows": (fts or {}).get("n"),
        "pending_embeddings": (pending or {}).get("n"),
    }


@app.post("/ingest")
async def ingest(request: Request):
    env = env_of(request)
    db = env.DB
    token = secret_of(request, "INGEST_TOKEN")
    if not token or request.headers.get("authorization", "") != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="bad token")

    body = await request.json()
    posts = body.get("posts") or []
    if not isinstance(posts, list) or not posts:
        raise HTTPException(status_code=400, detail="posts[] is required")
    if len(posts) > 25:
        raise HTTPException(status_code=413, detail="send at most 25 posts per request")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ids = [str(item.get("id") or "").strip() for item in posts]
    placeholders = ",".join("?" * len(ids))
    existing = await fetch_all(db, f"SELECT id FROM posts WHERE id IN ({placeholders})", tuple(ids))
    known = {row["id"] for row in existing}
    fresh = sum(1 for post_id in ids if post_id not in known)

    statements = []
    for post in posts:
        post_id = str(post.get("id") or "").strip()
        if not post_id:
            raise HTTPException(status_code=400, detail="every post needs an id")
        statements.append(
            db.prepare(
                """INSERT INTO posts (id, url, handle, author, text, posted_at, first_seen_at, raw)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     url = excluded.url,
                     handle = excluded.handle,
                     author = excluded.author,
                     text = excluded.text,
                     posted_at = COALESCE(excluded.posted_at, posts.posted_at),
                     embedded_at = CASE WHEN posts.text = excluded.text THEN posts.embedded_at ELSE NULL END,
                     raw = excluded.raw"""
            ).bind(
                post_id,
                post.get("url") or "",
                post.get("handle") or "",
                post.get("author") or "",
                post.get("text") or "",
                post.get("posted_at"),
                now,
                post.get("raw") or "",
            )
        )
        statements.append(db.prepare("DELETE FROM posts_fts WHERE post_id = ?").bind(post_id))
        statements.append(
            db.prepare("INSERT INTO posts_fts (post_id, text, handle, author) VALUES (?, ?, ?, ?)").bind(
                post_id,
                post.get("text") or "",
                post.get("handle") or "",
                post.get("author") or "",
            )
        )

    await db.batch(statements)

    # Embedding is best effort: a failure must not lose the post, /reindex can catch up.
    embedding_error = ""
    try:
        await embed_posts(env, [item for item in posts if item.get("text")])
    except Exception as error:  # noqa: BLE001 - report, do not fail the ingest
        embedding_error = str(error)[:200]

    answer = {"ok": True, "stored": len(posts), "new": fresh}
    if embedding_error:
        answer["embedding_error"] = embedding_error
    return answer


@app.get("/debug/stream")
async def debug_stream():
    """Temporary: proves whether SSE chunks leave the Worker before the handler finishes."""

    async def events():
        for index in range(4):
            yield f"data: tick {index}\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(events(), media_type="text/event-stream")


@app.get("/debug/search")
async def debug_search(request: Request, q: str = ""):
    """Numbers behind the ranking, for tuning and for the chat tool."""
    env = env_of(request)
    query = (q or "").strip()
    lexical = await lexical_search(env.DB, query) if query else []
    semantic = await vector_search(env, query) if query else []
    rows, used = await hybrid_search(env, query) if query else ([], "none")
    return {
        "q": query,
        "floor": HYBRID_VECTOR_FLOOR,
        "used": used,
        "lexical": [{"id": row["id"], "bm25": round(float(row.get("score") or 0), 3)} for row in lexical[:10]],
        "semantic": [
            {"id": row["id"], "cosine": round(float(row.get("score") or 0), 3)} for row in semantic[:10]
        ],
        "fused": [{"id": row["id"], "rrf": row.get("rank_score")} for row in rows[:10]],
    }


@app.post("/reindex")
async def reindex(request: Request, limit: int = 20):
    env = env_of(request)
    rows = await fetch_all(
        env.DB,
        "SELECT id, handle, text, posted_at FROM posts WHERE embedded_at IS NULL AND text != '' LIMIT ?",
        (min(max(limit, 1), 50),),
    )
    if not rows:
        remaining = await fetch_one(env.DB, "SELECT COUNT(*) AS n FROM posts WHERE embedded_at IS NULL")
        answer = {"ok": True, "embedded": 0, "remaining": (remaining or {}).get("n", 0)}
    else:
        embedded = await embed_posts(env, rows)
        remaining = await fetch_one(env.DB, "SELECT COUNT(*) AS n FROM posts WHERE embedded_at IS NULL")
        answer = {"ok": True, "embedded": embedded, "remaining": (remaining or {}).get("n", 0)}

    if request.headers.get("hx-request") == "true":
        return HTMLResponse(
            f'<span class="hint">{answer["embedded"]} embedded, {answer["remaining"]} left.</span>'
        )
    return answer


# ---------------------------------------------------------------- tools


MAX_TOOL_ROUNDS = 3

SQL_FORBIDDEN = (
    "pragma", "attach", "detach", "insert", "update", "delete", "replace",
    "drop", "alter", "create", "vacuum", "reindex", "begin", "commit", "rollback",
)

SYSTEM_PROMPT = (
    "You answer questions about the user's saved X bookmarks, using tools.\n"
    "Tools:\n"
    "- search_bookmarks: hybrid keyword + meaning search. Start here for topic questions.\n"
    "- vector_search: meaning only. Use it when the user's words are unlikely to appear in the text.\n"
    "- sql_query: read-only SQLite. Use it for counts, dates, handles, grouping and filters.\n"
    "- get_bookmark: one post in full, by id.\n"
    "Rules:\n"
    "1. Call at most 3 tools before answering. Do not repeat the same call.\n"
    "2. Every result reports authority and complete. authority=exact with complete=true means you already\n"
    "   have the whole answer: answer now and do not search again. authority=ranked or approximate is a\n"
    "   candidate list, so a different query may help. Never cite rows marked citable=false.\n"
    "3. Answer only from tool output. Every claim carries the post number in square brackets, like [2].\n"
    "4. Never invent a post, a link, a number or an id.\n"
    "5. If the tools return nothing useful, say so in one line and name a better search phrase.\n"
    "6. Be short: three sentences at most, then the citations. If the user asks for a post's full text,\n"
    "   quote it: fetch it with get_bookmark full=true and reproduce it without cutting it.\n"
    "7. No markdown headings and no separate citations or sources list: the interface already shows the posts.\n"
    "8. Earlier turns are real history. If the user names a person, or points back at a person already named,\n"
    "   pass author= to search_bookmarks, or filter by author in SQL. Do not search a bare first name.\n"
    "9. A follow-up refers to the conversation, not just to this sentence. Resolve it against the earlier turns\n"
    "   before you choose a tool."
)

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_bookmarks",
            "description": "Hybrid search over the saved bookmarks: keyword match (FTS5/BM25) fused with meaning (bge-m3 vectors). Best default for topic questions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Words or a natural-language description"},
                    "author": {
                        "type": "string",
                        "description": "Optional. Restrict to one person: handle (@name) or display name.",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20, "description": "Default 6"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "vector_search",
            "description": "Meaning-only search over the bookmarks, no keyword requirement. Use when the user's words probably do not appear in any post.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sql_query",
            "description": (
                "Run ONE read-only SQLite statement (SELECT or WITH only) against the bookmarks database. "
                "Use it for counts, dates, filters and grouping. Returns at most 50 rows. Never select raw. "
                "Include id, handle, url and posted_at in the SELECT when the rows are posts that you will "
                "cite: those rows come back with an [n] you can use. Aggregate rows get no [n]. "
                "String fields are cut at 300 characters: call get_bookmark with full=true for the whole "
                "text of a post.\n"
                "Schema:\n"
                "posts(id TEXT PRIMARY KEY, url TEXT, handle TEXT, author TEXT, text TEXT, posted_at TEXT ISO, "
                "first_seen_at TEXT ISO, gone INTEGER, embedded_at TEXT, raw TEXT)\n"
                "posts_fts(post_id UNINDEXED, text, handle, author) -- FTS5 virtual table, join on posts.id = posts_fts.post_id\n"
                "conversations(id TEXT, title TEXT, created_at TEXT, updated_at TEXT)\n"
                "messages(id INTEGER, conversation_id TEXT, role TEXT, content TEXT, sources TEXT, model TEXT, tool_trace TEXT, created_at TEXT)"
            ),
            "parameters": {
                "type": "object",
                "properties": {"sql": {"type": "string", "description": "One SELECT or WITH statement"}},
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_bookmark",
            "description": (
                "Fetch one bookmark by its post id. By default you get the first 500 characters; "
                "pass full=true when the user wants the whole post or you must quote beyond the preview."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Post id, as seen in search results"},
                    "full": {
                        "type": "boolean",
                        "description": "Return the whole stored text instead of the 500-character preview.",
                    },
                    "from_char": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "With full=true, start at this character. Use it to continue a long post.",
                    },
                },
                "required": ["id"],
            },
        },
    },
]


def sql_guard(sql: str) -> str:
    """One read-only statement, or an error. This is the only place user text reaches SQL."""
    text = (sql or "").strip()
    if not text:
        raise ValueError("sql is empty")
    if len(text) > 2000:
        raise ValueError("sql is longer than 2000 characters")
    body = text.rstrip(";")
    if ";" in body:
        raise ValueError("exactly one statement is allowed")
    lowered = body.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise ValueError("only SELECT and WITH statements are allowed")
    for word in SQL_FORBIDDEN:
        if re.search(rf"\b{word}\b", lowered):
            raise ValueError(f"{word} is not allowed")
    return body


def trim_row(row: dict, limit: int = 300) -> dict:
    out = {}
    for key, value in row.items():
        if key == "raw":
            continue
        if isinstance(value, str) and len(value) > limit:
            out[key] = value[:limit] + "..."
        else:
            out[key] = value
    return out


def cited_sources(sources: list[dict], answer: str) -> list[dict]:
    """Only what the answer cites. No fallback: a row the answer did not use is not a source."""
    cited = {int(number) for number in re.findall(r"\[(\d+)\]", answer)}
    return [source for source in sources if source.get("n") in cited]


def register(rows: list[dict], registry: dict, limit: int = 6) -> list[dict]:
    """Give every post one stable number, so the model's [n] matches the UI chips."""
    out = []
    for row in rows[:limit]:
        raw_id = row.get("id")
        if raw_id is None or str(raw_id) == "":
            continue  # an aggregate row is not a post and cannot be cited
        post_id = str(raw_id)
        if post_id not in registry:
            registry[post_id] = {
                "id": post_id,
                "handle": row.get("handle"),
                # A SQL projection may omit url; the canonical form always works.
                "url": row.get("url") or f"https://x.com/i/web/status/{post_id}",
                "posted_at": row.get("posted_at"),
                "n": len(registry) + 1,
            }
        item = registry[post_id]
        text = row.get("text") or ""
        out.append(
            {
                "n": item["n"],
                "id": post_id,
                "handle": row.get("handle"),
                "posted_at": row.get("posted_at"),
                "text": text[:PREVIEW_CHARS],
                # Tell the model the truth: there is more of this post.
                "text_truncated": len(text) > PREVIEW_CHARS,
            }
        )
    return out


def text_view(text: str, from_char: int = 0, limit: int = FULL_TEXT_CHARS) -> tuple[str, bool, int]:
    """A window of the text, whether more remains, and the total length."""
    total = len(text or "")
    piece = (text or "")[from_char : from_char + limit]
    more = from_char + len(piece) < total
    return piece, more, total


def matches_author(row: dict, author: str) -> bool:
    """Case-insensitive match on the handle or the display name."""
    wanted = author.lower().lstrip("@")
    handle = str(row.get("handle") or "").lower().lstrip("@")
    name = str(row.get("author") or "").lower()
    return bool(wanted) and (wanted in handle or wanted in name)


async def posts_by_author(db, author: str, limit: int = 20) -> list[dict]:
    """The direct route: a person's posts, newest first, without guessing at keywords."""
    like = f"%{author.lstrip('@')}%"
    return await fetch_all(
        db,
        """SELECT id, handle, author, text, url, posted_at FROM posts
           WHERE handle LIKE ? OR author LIKE ?
           ORDER BY posted_at DESC LIMIT ?""",
        (like, like, limit),
    )


async def tool_search(env, args: dict, registry: dict, mode: str) -> dict:
    query = str(args.get("query") or "").strip()
    author = str(args.get("author") or "").strip()
    if not query and not author:
        raise ValueError("query or author is required")
    limit = min(int(args.get("limit") or 6), 20)
    if not query:
        # "all posts by X" needs no keywords at all.
        rows = await posts_by_author(env.DB, author, limit)
        used = "author"
    elif mode == "vector":
        rows = await vector_search(env, query)
        used = "vector"
    else:
        rows, used = await hybrid_search(env, query)
    if author and query:
        kept = [row for row in rows if matches_author(row, author)]
        if not kept:
            # The keyword hit other people; go straight to the person.
            kept = await posts_by_author(env.DB, author, limit)
            used = f"{used}+author"
        rows = kept
    posts = register(rows, registry, limit)
    if mode == "vector":
        return {
            "used": "vector",
            "count": len(posts),
            "posts": posts,
            # No keyword evidence behind these rows. The model must not treat them as proof.
            "authority": "approximate",
            "complete": False,
            "citable": False,
            "note": "Nearest neighbours by meaning only. Not matches. Do not cite these as the answer.",
        }
    return {
        "used": used,
        "count": len(posts),
        "posts": posts,
        "authority": "ranked",
        "complete": False,
        "citable": True,
        "note": "Top ranked matches, not the full set. A different query may find more.",
    }


async def tool_sql(env, args: dict, registry: dict) -> dict:
    statement = sql_guard(str(args.get("sql") or ""))
    rows = await fetch_all(env.DB, statement)
    trimmed = [trim_row(row) for row in rows[:50]]
    truncated = len(rows) > 50
    # Rows that are posts get the same stable [n] as search hits, so the answer can link them.
    registered = {item["id"]: item["n"] for item in register(trimmed, registry, 50)}
    for row in trimmed:
        number = registered.get(str(row.get("id")))
        if number is not None:
            row["n"] = number
    return {
        "row_count": len(rows),
        "rows": trimmed,
        "truncated": truncated,
        "authority": "exact",
        "complete": not truncated,
        "citable": True,
        "note": "Every row the query matched." if not truncated else "Capped at 50 rows: there is more.",
    }


async def tool_get(env, args: dict, registry: dict) -> dict:
    post_id = str(args.get("id") or "").strip()
    if not post_id:
        raise ValueError("id is required")
    full = bool(args.get("full"))
    from_char = max(0, int(args.get("from_char") or 0))
    row = await fetch_one(
        env.DB,
        "SELECT id, url, handle, author, text, posted_at, first_seen_at FROM posts WHERE id = ?",
        (post_id,),
    )
    if not row:
        return {"found": False, "id": post_id}
    item = register([row], registry, 1)[0]
    text = row.get("text") or ""
    if full:
        piece, more, total = text_view(text, from_char)
        note = (
            f"Characters {from_char + 1}-{from_char + len(piece)} of {total}. "
            f"Call again with from_char={from_char + len(piece)} for the rest."
            if more
            else f"The whole stored text of this post ({total} characters)."
        )
    else:
        piece, more, total = item["text"], len(text) > PREVIEW_CHARS, len(text)
        note = (
            "The first 500 characters. Call again with full=true for the whole text."
            if more
            else f"The whole stored text of this post ({total} characters)."
        )
    return {
        "found": True,
        "post": {
            **item,
            "author": row.get("author"),
            "url": row.get("url"),
            "text": piece,
            "text_truncated": more,
            "text_chars_total": total,
        },
        "authority": "exact",
        # complete means: nothing of this post is still unread. A preview is not complete.
        "complete": not more,
        "citable": True,
        "note": note,
    }


async def run_tool(env, name: str, args: dict, registry: dict) -> dict:
    if name == "search_bookmarks":
        return await tool_search(env, args, registry, "hybrid")
    if name == "vector_search":
        return await tool_search(env, args, registry, "vector")
    if name == "sql_query":
        return await tool_sql(env, args, registry)
    if name == "get_bookmark":
        return await tool_get(env, args, registry)
    raise ValueError(f"unknown tool: {name}")


def preview(result) -> str:
    if isinstance(result, dict):
        authority = result.get("authority", "")
        complete = "complete" if result.get("complete") else "partial"
        if "posts" in result:
            return f"{result.get('count', 0)} posts via {result.get('used')} ({authority}, {complete})"
        if "row_count" in result:
            return f"{result['row_count']} rows ({authority}, {complete})"
        if "found" in result:
            if not result["found"]:
                return "not found"
            post = result.get("post") or {}
            return "partial text" if post.get("text_truncated") else "full text"
        if "error" in result:
            return f"error: {str(result['error'])[:120]}"
    return str(result)[:120]


async def call_model(env, messages: list[dict], model: str, session: str, tools=None) -> dict:
    """OpenCode Go, OpenAI-compatible. Returns {content, tool_calls}."""
    key = env_secret(env, "GO_API_KEY")
    if not key:
        raise RuntimeError("GO_API_KEY is not set")

    payload = {"model": model, "messages": messages, "temperature": 0.2, "max_tokens": 2500}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    init = to_js(
        {
            "method": "POST",
            "headers": {
                "content-type": "application/json",
                "authorization": f"Bearer {key}",
                # OpenCode Go routes on this header and rejects requests without it.
                "x-opencode-session": session,
            },
            "body": json.dumps(payload),
        },
        dict_converter=Object.fromEntries,
    )
    response = await js_fetch(GO_BASE + "/chat/completions", init)
    status = int(response.status)
    text = str(await response.text())
    if status != 200:
        raise RuntimeError(f"{model} HTTP {status}: {text[:160]}")

    choices = json.loads(text).get("choices") or []
    if not choices:
        raise RuntimeError(f"{model} sent no choices: {text[:160]}")
    message = choices[0].get("message") or {}
    content = message.get("content") or ""
    calls = message.get("tool_calls") or []
    if not content and not calls and message.get("reasoning"):
        raise RuntimeError(f"{model} used the whole budget on reasoning")
    return {"content": content, "tool_calls": calls}


HISTORY_CHAR_LIMIT = 200_000
LEDGER_CALLS = 12


def ledger_lines(traces: list[list[dict]], limit: int = LEDGER_CALLS) -> list[str]:
    """One line per tool call from earlier turns: the call and its summary, never the payload."""
    lines = []
    for trace in traces:
        for step in trace or []:
            if step.get("tool") == "model":
                continue
            args = json.dumps(step.get("args") or {}, separators=(",", ":"))
            lines.append(f"- {step.get('tool')} {args[:160]} -> {str(step.get('result'))[:120]}")
    return lines[-limit:]


def history_messages(rows: list[dict], limit: int = HISTORY_CHAR_LIMIT) -> list[dict]:
    """All prior turns, text only. A runaway conversation keeps its newest messages."""
    messages = [
        {"role": row["role"], "content": row["content"]}
        for row in rows
        if row.get("content") and row.get("role") in ("user", "assistant")
    ]
    total = sum(len(message["content"]) for message in messages)
    dropped = 0
    while messages and total > limit:
        total -= len(messages.pop(0)["content"])
        dropped += 1
    if dropped:
        messages.insert(0, {"role": "user", "content": "(Earlier turns omitted: this chat is very long.)"})
    return messages


def build_prompt(ledger: list[str]) -> str:
    if not ledger:
        return SYSTEM_PROMPT
    return (
        SYSTEM_PROMPT
        + "\nEarlier tool calls in this conversation. Results are summarized, not reproduced:\n"
        + "\n".join(ledger)
        + "\nIf you need that data again, re-issue the call. To quote a post, call get_bookmark.\n"
    )


async def load_history(db, conversation_id: str) -> tuple[list[dict], list[str]]:
    rows = await fetch_all(
        db,
        "SELECT role, content, tool_trace FROM messages WHERE conversation_id = ? ORDER BY id",
        (conversation_id,),
    )
    traces = []
    for row in rows:
        if row.get("tool_trace"):
            try:
                traces.append(json.loads(row["tool_trace"]))
            except json.JSONDecodeError:
                continue
    return history_messages(rows), ledger_lines(traces)


async def agent_answer(
    env, question: str, session: str, emit=None, history: list[dict] | None = None,
    ledger: list[str] | None = None,
) -> tuple[str, list[dict], str, str, list[dict]]:
    """Tool loop: the model chooses search, vector search, SQL or a single post lookup."""

    async def say(kind: str, text: str) -> None:
        if emit is not None:
            await emit(kind, {"text": text})

    registry: dict[str, dict] = {}
    trace: list[dict] = []
    messages = [{"role": "system", "content": build_prompt(ledger or [])}]
    messages.extend(history or [])
    messages.append({"role": "user", "content": question})

    await say("status", "Searching your bookmarks...")
    model = CHAT_MODEL
    for attempt in range(2):
        try:
            for round_index in range(MAX_TOOL_ROUNDS + 1):
                allow_tools = round_index < MAX_TOOL_ROUNDS
                reply = await call_model(env, messages, model, session, TOOL_SCHEMAS if allow_tools else None)

                if reply["tool_calls"]:
                    messages.append(
                        {"role": "assistant", "content": reply["content"] or None, "tool_calls": reply["tool_calls"]}
                    )
                    for call in reply["tool_calls"]:
                        name = (call.get("function") or {}).get("name") or ""
                        try:
                            args = json.loads((call.get("function") or {}).get("arguments") or "{}")
                        except json.JSONDecodeError:
                            args = {}
                        await say("tool", f"Running {name}...")
                        try:
                            result = await run_tool(env, name, args, registry)
                        except Exception as error:  # noqa: BLE001 - report the tool error to the model
                            result = {"error": str(error)[:300]}
                        trace.append({"round": round_index + 1, "tool": name, "args": args, "result": preview(result)})
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.get("id"),
                                "name": name,
                                "content": json.dumps(result)[:12000],
                            }
                        )
                    continue

                return reply["content"], list(registry.values()), "tools" if trace else "none", model, trace

            # Out of rounds: force an answer from what the tools already returned.
            messages.append({"role": "user", "content": "Stop calling tools. Answer now from what you have."})
            reply = await call_model(env, messages, model, session, None)
            return reply["content"], list(registry.values()), "tools", model, trace

        except Exception as error:  # noqa: BLE001 - fall back once, then give up
            trace.append({"round": 0, "tool": "model", "args": {}, "result": f"{model}: {str(error)[:160]}"})
            if attempt == 0:
                model = CHAT_FALLBACK
                messages = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": question},
                ]
                continue
            break

    return (
        "The model did not answer. Try again in a moment.",
        list(registry.values()),
        "none",
        "",
        trace,
    )


async def create_conversation(db, title: str) -> dict:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conversation = {"id": uuid.uuid4().hex[:10], "title": (title or "New chat")[:80]}
    await db.batch(
        [
            db.prepare("INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)").bind(
                conversation["id"], conversation["title"], now, now
            )
        ]
    )
    return conversation


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request, c: str = "", new: int = 0):
    db = env_of(request).DB
    conversation = None
    if new:
        # Reuse an unused chat instead of piling up empty "New chat" rows.
        conversation = await fetch_one(
            db,
            """SELECT c.id, c.title FROM conversations c
               WHERE NOT EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = c.id)
               ORDER BY c.updated_at DESC LIMIT 1""",
        )
    if not new and c:
        conversation = await fetch_one(db, "SELECT id, title FROM conversations WHERE id = ?", (c,))
    if conversation is None and not new:
        conversation = await fetch_one(db, "SELECT id, title FROM conversations ORDER BY updated_at DESC LIMIT 1")
    if conversation is None:
        conversation = await create_conversation(db, "New chat")

    history = await fetch_all(
        db,
        "SELECT role, content, sources, model, tool_trace FROM messages WHERE conversation_id = ? ORDER BY id",
        (conversation["id"],),
    )
    for message in history:
        message["sources"] = json.loads(message["sources"]) if message.get("sources") else []
        message["trace"] = json.loads(message["tool_trace"]) if message.get("tool_trace") else []

    conversations = await fetch_all(db, "SELECT id, title FROM conversations ORDER BY updated_at DESC LIMIT 20")
    pending = await fetch_one(db, "SELECT COUNT(*) AS n FROM posts WHERE embedded_at IS NULL")
    return await render(
        request,
        "chat.html",
        {
            "conversation": conversation,
            "messages": history,
            "conversations": conversations,
            "pending": (pending or {}).get("n", 0),
        },
    )


@app.post("/chat", response_class=HTMLResponse)
async def chat_send(request: Request):
    env = env_of(request)
    db = env.DB
    form = parse_qs((await request.body()).decode("utf-8", "replace"))
    question = (form.get("message") or [""])[0].strip()
    conversation_id = (form.get("conversation") or [""])[0].strip()
    if not question:
        raise HTTPException(status_code=400, detail="message is required")

    conversation = None
    if conversation_id:
        conversation = await fetch_one(db, "SELECT id, title FROM conversations WHERE id = ?", (conversation_id,))
    if conversation is None:
        conversation = await create_conversation(db, question[:60])

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    statements = [
        db.prepare(
            "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, 'user', ?, ?)"
        ).bind(conversation["id"], question, now)
    ]
    if (conversation.get("title") or "New chat") == "New chat":
        statements.append(
            db.prepare("UPDATE conversations SET title = ? WHERE id = ?").bind(question[:60], conversation["id"])
        )
    await db.batch(statements)

    history, ledger = await load_history(env.DB, conversation["id"])
    answer, sources, used, model, trace = await agent_answer(
        env, question, f"tb-{conversation['id']}", history=history, ledger=ledger
    )
    slim_all = [
        {
            "id": row["id"],
            "n": row.get("n"),
            "handle": row.get("handle"),
            "url": row.get("url"),
            "posted_at": row.get("posted_at"),
        }
        for row in sources
    ]
    slim = cited_sources(slim_all, answer)

    await db.batch(
        [
            db.prepare(
                "INSERT INTO messages (conversation_id, role, content, sources, model, tool_trace, created_at) VALUES (?, 'assistant', ?, ?, ?, ?, ?)"
            ).bind(conversation["id"], answer, json.dumps(slim), model, json.dumps(trace), now),
            db.prepare("UPDATE conversations SET updated_at = ? WHERE id = ?").bind(now, conversation["id"]),
        ]
    )

    # No-JS fallback: store the answer, then load the page normally.
    return RedirectResponse(f"/chat?c={conversation['id']}", status_code=303)


@app.post("/eval/ask")
async def eval_ask(request: Request):
    """Tool-routing eval: bearer token, no session, no database writes.

    Returns the answer plus the tool trace, so a test can assert WHICH tools ran.
    """
    env = env_of(request)
    token = secret_of(request, "INGEST_TOKEN")
    if not token or request.headers.get("authorization", "") != f"Bearer {token}":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        payload = json.loads((await request.body()) or b"{}")
    except json.JSONDecodeError:
        return JSONResponse({"error": "body must be JSON"}, status_code=400)
    question = str(payload.get("question") or "").strip()
    if not question:
        return JSONResponse({"error": "question is required"}, status_code=400)
    history = payload.get("history") or []
    answer, sources, used, model, trace = await agent_answer(
        env, question, "tb-eval", history=history, ledger=payload.get("ledger") or []
    )
    # Same filtering the chat routes use, so a test sees the chips a reader would see.
    citations = cited_sources(sources, answer)
    return {
        "answer": answer,
        "model": model,
        "used": used,
        "tools": trace,
        "sources": len(sources),
        "citations": [
            {"n": row.get("n"), "handle": row.get("handle"), "url": row.get("url")} for row in citations
        ],
    }


@app.post("/chat/stream")
async def chat_stream(request: Request):
    """Same loop as /chat, but the browser sees progress as it happens (Server-Sent Events)."""
    env = env_of(request)
    db = env.DB
    form = parse_qs((await request.body()).decode("utf-8", "replace"))
    question = (form.get("message") or [""])[0].strip()
    conversation_id = (form.get("conversation") or [""])[0].strip()
    if not question:
        raise HTTPException(status_code=400, detail="message is required")

    conversation = None
    if conversation_id:
        conversation = await fetch_one(db, "SELECT id, title FROM conversations WHERE id = ?", (conversation_id,))
    if conversation is None:
        conversation = await create_conversation(db, question[:60])

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    statements = [
        db.prepare(
            "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, 'user', ?, ?)"
        ).bind(conversation["id"], question, now)
    ]
    if (conversation.get("title") or "New chat") == "New chat":
        statements.append(
            db.prepare("UPDATE conversations SET title = ? WHERE id = ?").bind(question[:60], conversation["id"])
        )
    await db.batch(statements)

    async def events():
        queue: asyncio.Queue = asyncio.Queue()

        async def emit(kind: str, payload: dict) -> None:
            await queue.put({"kind": kind, "payload": payload})

        async def work() -> None:
            try:
                await emit("start", {"conversation": conversation["id"]})
                history, ledger = await load_history(env.DB, conversation["id"])
                answer, sources, used, model, trace = await agent_answer(
                    env, question, f"tb-{conversation['id']}", emit, history=history, ledger=ledger
                )
                slim_all = [
                    {
                        "id": row["id"],
                        "n": row.get("n"),
                        "handle": row.get("handle"),
                        "url": row.get("url"),
                        "posted_at": row.get("posted_at"),
                    }
                    for row in sources
                ]
                slim = cited_sources(slim_all, answer)

                await db.batch(
                    [
                        db.prepare(
                            "INSERT INTO messages (conversation_id, role, content, sources, model, tool_trace, created_at) "
                            "VALUES (?, 'assistant', ?, ?, ?, ?, ?)"
                        ).bind(conversation["id"], answer, json.dumps(slim), model, json.dumps(trace), now),
                        db.prepare("UPDATE conversations SET updated_at = ? WHERE id = ?").bind(now, conversation["id"]),
                    ]
                )
                # Only the assistant bubble: the page already added the user message optimistically.
                fragment = await render(
                    request,
                    "partials/messages.html",
                    {
                        "messages": [
                            {
                                "role": "assistant",
                                "content": answer,
                                "sources": slim,
                                "model": model,
                                "trace": trace,
                            }
                        ]
                    },
                )
                await emit("answer", {"html": fragment.body.decode("utf-8", "replace")})
            except Exception as error:  # noqa: BLE001 - report to the browser, never hang it
                await emit("error", {"text": f"Something failed: {str(error)[:200]}"})
            finally:
                await queue.put({"kind": "done", "payload": None})

        task = asyncio.create_task(work())
        yield "retry: 2000\n\n"
        while True:
            item = await queue.get()
            if item["kind"] == "done":
                break
            yield f"event: {item['kind']}\ndata: {json.dumps(item['payload'])}\n\n"
        await task

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
    )


@app.post("/chat/{conversation_id}/delete")
async def chat_delete(request: Request, conversation_id: str):
    db = env_of(request).DB
    await db.batch(
        [
            db.prepare("DELETE FROM messages WHERE conversation_id = ?").bind(conversation_id),
            db.prepare("DELETE FROM conversations WHERE id = ?").bind(conversation_id),
        ]
    )
    return RedirectResponse("/chat", status_code=303)


@app.get("/{path:path}")
async def static_files(path: str, request: Request):
    env = env_of(request)
    resp = await env.ASSETS.fetch(f"https://assets.local/{path}")
    return Response(
        content=await resp.bytes(),
        status_code=resp.status,
        headers={"content-type": resp.headers.get("content-type", "application/octet-stream")},
    )


Default = asgi.entrypoint(app)
