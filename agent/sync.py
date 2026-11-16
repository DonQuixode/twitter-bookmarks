#!/usr/bin/env python3
"""Sync X bookmarks into the Worker.

One run: drive the real Chrome through browse, capture the GraphQL bodies X itself fetches, parse them,
and POST each page to /ingest. Stops as soon as a page brings nothing new, unless --deep is given.

    INGEST_TOKEN=... TB_API=https://twitter-bookmarks.example.workers.dev ./agent/sync.py
    ./agent/sync.py --deep --max-pages 40
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import parse  # noqa: E402

HERE = Path(__file__).resolve().parent
DEFAULT_STATE = Path.home() / ".local" / "state" / "twitter-bookmarks" / "state.json"
DEFAULT_TOKEN_FILE = Path.home() / ".config" / "twitter-bookmarks" / "token"
BATCH_LIMIT = 25  # D1 allows 100 bound params; /ingest caps at 25 posts


def find_browse() -> str:
    candidate = os.environ.get("BROWSE_BIN")
    if candidate:
        return candidate
    local = Path.home() / ".local" / "bin" / "browse"
    return str(local) if local.exists() else "browse"


def read_token(path: Path) -> str:
    token = os.environ.get("INGEST_TOKEN")
    if token:
        return token.strip()
    if path.exists():
        return path.read_text().strip()
    raise SystemExit(f"no token: set INGEST_TOKEN or write {path}")


def post(api: str, token: str, posts: list[dict], timeout: float = 60) -> dict:
    payload = {"posts": posts}
    request = urllib.request.Request(
        api.rstrip("/") + "/ingest",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {token}",
            # Cloudflare's browser integrity check rejects the default Python-urllib agent (error 1010).
            "user-agent": "twitter-bookmarks-sync/0.1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")[:300]
        raise SystemExit(f"ingest failed: HTTP {error.code} {body}") from error


def sendable(post_record: dict) -> dict:
    """Only what /ingest accepts today."""
    return {
        "id": post_record["id"],
        "url": post_record["url"],
        "handle": post_record["handle"],
        "author": post_record["author"],
        "text": post_record["text"],
        "posted_at": post_record["posted_at"],
        "raw": post_record["raw"],
    }


def run_capture(browse: str, args: dict, timeout_ms: int) -> dict:
    template = (HERE / "capture.js").read_text()
    script = template.replace("__ARGS__", json.dumps(args))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
        handle.write(script)
        path = handle.name
    try:
        result = subprocess.run(
            [browse, "execute", "--file", path, "--timeoutMs", str(timeout_ms)],
            capture_output=True,
            text=True,
            timeout=timeout_ms / 1000 + 60,
        )
    finally:
        Path(path).unlink(missing_ok=True)

    if result.returncode != 0:
        raise SystemExit(f"browse execute failed: {(result.stderr or result.stdout).strip()[:500]}")
    output = result.stdout
    start = output.find("{")
    if start < 0:
        raise SystemExit(f"no JSON from browse: {output.strip()[:400]}")
    # The CLI prints the result, then warnings and notes. Take the first complete JSON value only.
    try:
        value, end = json.JSONDecoder().raw_decode(output[start:])
    except json.JSONDecodeError as error:
        raise SystemExit(f"cannot read browse result: {error}") from error
    trailing = output[start + end :].strip()
    if trailing:
        print(f"browse said: {trailing[:400]}", file=sys.stderr)
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync X bookmarks into the Worker")
    parser.add_argument("--api", default=os.environ.get("TB_API", ""), help="Worker base URL")
    parser.add_argument("--max-pages", type=int, default=0, help="pages to walk (default 4, deep 40)")
    parser.add_argument("--deep", action="store_true", help="ignore the early stop")
    parser.add_argument("--dry-run", action="store_true", help="capture and parse, but do not POST")
    parser.add_argument("--save-raw", default="", help="write each captured page body to this directory")
    parser.add_argument("--state", default=os.environ.get("TB_STATE", str(DEFAULT_STATE)))
    parser.add_argument("--token-file", default=str(DEFAULT_TOKEN_FILE))
    parser.add_argument("--start-url", default="https://x.com/i/bookmarks")
    parser.add_argument("--settle-ms", type=int, default=6000)
    parser.add_argument("--scroll-px", type=int, default=4000)
    parser.add_argument("--scroll-wait-ms", type=int, default=2600)
    options = parser.parse_args()

    started = time.time()
    max_pages = options.max_pages or (40 if options.deep else 4)
    token = "" if options.dry_run else read_token(Path(options.token_file))
    if not options.dry_run and not options.api:
        raise SystemExit("no API: set TB_API or pass --api")

    capture_args = {
        "maxPages": max_pages,
        "startUrl": options.start_url,
        "settleMs": options.settle_ms,
        "scrollPx": options.scroll_px,
        "scrollWaitMs": options.scroll_wait_ms,
    }
    captured = run_capture(find_browse(), capture_args, timeout_ms=max(120_000, max_pages * 12_000))

    raw_dir = Path(options.save_raw) if options.save_raw else None
    if raw_dir:
        raw_dir.mkdir(parents=True, exist_ok=True)

    seen_cursors: set[str] = set()
    summary = {
        "pages": 0,
        "posts": 0,
        "new": 0,
        "duplicate_pages": 0,
        "skipped": 0,
        "batch_errors": [],
        "last_bottom_cursor": None,
        "stopped_because": "max-pages",
    }

    for index, page in enumerate(captured.get("pages") or []):
        body_path = Path(page.get("path") or "")
        try:
            body = body_path.read_text()
        except OSError as error:
            summary["batch_errors"].append(f"page {index}: cannot read {body_path}: {error}")
            continue
        try:
            parsed = parse.parse_body(body)
        except ValueError as error:
            summary["batch_errors"].append(f"page {index}: {error}")
            if raw_dir:
                raw_dir.mkdir(parents=True, exist_ok=True)
                (raw_dir / f"page-{index:02d}.bad.json").write_text(body)
            continue

        cursor = parsed.get("bottom_cursor")
        if cursor and cursor in seen_cursors:
            summary["duplicate_pages"] += 1
            continue
        if cursor:
            seen_cursors.add(cursor)
        summary["last_bottom_cursor"] = cursor or summary["last_bottom_cursor"]

        if raw_dir:
            raw_dir.mkdir(parents=True, exist_ok=True)
            (raw_dir / f"page-{index:02d}.json").write_text(body)

        posts = parsed["posts"]
        summary["pages"] += 1
        summary["posts"] += len(posts)
        summary["skipped"] += parsed["skipped"]
        if not posts:
            continue

        for offset in range(0, len(posts), BATCH_LIMIT):
            batch = [sendable(item) for item in posts[offset : offset + BATCH_LIMIT]]
            if options.dry_run:
                new_in_batch = len(batch)
            else:
                answer = post(options.api, token, batch)
                new_in_batch = int(answer.get("new", 0))
            summary["new"] += new_in_batch

        if not options.deep and summary["new"] == 0 and summary["posts"] > 0:
            summary["stopped_because"] = "nothing-new"
            break

    if not raw_dir:
        for page in captured.get("pages") or []:
            Path(page.get("path") or "").unlink(missing_ok=True)

    summary["seconds"] = round(time.time() - started, 1)
    summary["page_url"] = captured.get("pageUrl")
    summary["capture_notes"] = captured.get("notes")
    summary["capture_dropped"] = captured.get("dropped", 0)

    state_path = Path(options.state)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    previous = {}
    if state_path.exists():
        try:
            previous = json.loads(state_path.read_text())
        except json.JSONDecodeError:
            previous = {}
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    deep_done = now if options.deep and not summary["batch_errors"] else previous.get("deep_completed_at")
    state = {
        "last_run": now,
        "last_success": now if not summary["batch_errors"] else previous.get("last_success"),
        "last_summary": summary,
        "deep_completed_at": deep_done,
    }
    state_path.write_text(json.dumps(state, indent=2) + "\n")

    print(json.dumps(summary, indent=1))
    return 0 if not summary["batch_errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
