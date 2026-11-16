"""Parse the JSON that X's bookmarks page fetches into post records.

Everything here is pure: pass it a decoded GraphQL payload, get posts back. No network, no browser.
The shapes were confirmed against the live endpoint (see decisions.md, D2).
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime

TWEET_PATH = ("itemContent", "tweet_results", "result")
X_DATE = "%a %b %d %H:%M:%S %z %Y"


def _dig(value, path):
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _timeline_instructions(payload: dict) -> list:
    """Find the instructions list whatever the timeline is called this month."""
    data = payload.get("data") or {}
    for value in data.values():
        if isinstance(value, dict) and isinstance(value.get("timeline"), dict):
            instructions = value["timeline"].get("instructions")
            if isinstance(instructions, list):
                return instructions
    # Some payloads nest one level deeper: data.<something>.timeline_v2.timeline
    for value in data.values():
        if isinstance(value, dict):
            for inner in value.values():
                if isinstance(inner, dict) and isinstance(inner.get("instructions"), list):
                    return inner["instructions"]
    return []


def iso_date(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw, X_DATE).isoformat(timespec="seconds")
    except ValueError:
        return None


def author_of(tweet: dict) -> tuple[str, str, str]:
    """Return (handle, display name, user id). Handles both the new and the old user schema."""
    user = _dig(tweet, ("core", "user_results", "result")) or {}
    if isinstance(user, dict) and "tweet" in user:  # rare wrapper
        user = user.get("tweet") or {}
    core = user.get("core") or {}
    legacy = user.get("legacy") or {}
    handle = core.get("screen_name") or legacy.get("screen_name") or ""
    name = core.get("name") or legacy.get("name") or ""
    return handle, name, user.get("rest_id") or legacy.get("id_str") or ""


def text_of(tweet: dict) -> tuple[str, bool]:
    """Long posts put the full text in note_tweet; legacy.full_text is the truncated preview."""
    note = _dig(tweet, ("note_tweet", "note_tweet_results", "result", "text"))
    legacy = tweet.get("legacy") or {}
    if isinstance(note, str) and note.strip():
        return html.unescape(note), True
    return html.unescape(legacy.get("full_text") or ""), False


def media_of(tweet: dict) -> list[dict]:
    legacy = tweet.get("legacy") or {}
    entities = legacy.get("extended_entities") or legacy.get("entities") or {}
    out = []
    for media in entities.get("media") or []:
        if not isinstance(media, dict):
            continue
        item = {
            "type": media.get("type") or "photo",
            "url": media.get("media_url_https") or media.get("media_url") or "",
        }
        if media.get("ext_alt_text"):
            item["alt"] = media["ext_alt_text"]
        variants = (media.get("video_info") or {}).get("variants") or []
        urls = [v.get("url") for v in variants if isinstance(v, dict) and v.get("url")]
        if urls:
            item["variants"] = urls
        out.append(item)
    return out


def quoted_of(tweet: dict) -> dict | None:
    result = _dig(tweet, ("quoted_status_result", "result"))
    if not isinstance(result, dict):
        return None
    if "tweet" in result and isinstance(result["tweet"], dict):  # TweetWithVisibilityResults
        result = result["tweet"]
    handle, name, _ = author_of(result)
    text, _ = text_of(result)
    quoted_id = result.get("rest_id") or (result.get("legacy") or {}).get("id_str")
    if not quoted_id:
        return None
    return {"id": quoted_id, "handle": handle, "author": name, "text": text}


def unwrap(result: dict) -> dict:
    """TweetWithVisibilityResults wraps the tweet one level down."""
    if isinstance(result, dict) and isinstance(result.get("tweet"), dict):
        return result["tweet"]
    return result


def extract_post(result: dict) -> dict | None:
    tweet = unwrap(result)
    legacy = tweet.get("legacy") or {}
    post_id = tweet.get("rest_id") or legacy.get("id_str")
    if not post_id:
        return None

    handle, name, user_id = author_of(tweet)
    text, long_form = text_of(tweet)
    quoted = quoted_of(tweet)
    return {
        "id": str(post_id),
        "url": f"https://x.com/{handle or 'i'}/status/{post_id}",
        "handle": handle,
        "author": name,
        "author_id": user_id,
        "text": text,
        "long_form": long_form,
        "posted_at": iso_date(legacy.get("created_at")),
        "lang": legacy.get("lang") or "",
        "conversation_id": legacy.get("conversation_id_str") or "",
        "in_reply_to": legacy.get("in_reply_to_status_id_str") or "",
        "quoted": quoted,
        "media": media_of(tweet),
        "raw": json.dumps(tweet, separators=(",", ":")),
    }


def parse_page(payload: dict) -> dict:
    """One GraphQL bookmarks page -> posts plus the cursors that page offered."""
    posts: list[dict] = []
    skipped = 0
    cursors: dict[str, str] = {}

    for instruction in _timeline_instructions(payload):
        for entry in instruction.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            content = entry.get("content") or {}
            cursor_type = content.get("cursorType")
            if cursor_type:
                cursors[str(cursor_type).lower()] = content.get("value") or ""
                continue
            result = _dig(content, TWEET_PATH)
            if not isinstance(result, dict):
                continue
            if result.get("__typename") in ("TweetTombstone", "TweetUnavailable"):
                skipped += 1
                continue
            post = extract_post(result)
            if post:
                posts.append(post)
            else:
                skipped += 1

    return {
        "posts": posts,
        "top_cursor": cursors.get("top"),
        "bottom_cursor": cursors.get("bottom"),
        "skipped": skipped,
    }


def parse_body(text: str) -> dict:
    """Decode one captured response body. Raises ValueError when it is not a timeline."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"not JSON: {error}") from error
    page = parse_page(payload)
    if not page["posts"] and not page["bottom_cursor"]:
        raise ValueError("no posts and no bottom cursor; not a bookmarks page")
    return page


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
