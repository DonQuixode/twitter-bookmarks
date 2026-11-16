"""Parser tests against a trimmed page captured from the live bookmarks endpoint."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))
import parse  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "bookmarks-page.json"


@pytest.fixture(scope="module")
def page() -> dict:
    return parse.parse_body(FIXTURE.read_text())


def test_reads_every_post_on_the_page(page):
    assert len(page["posts"]) == 2
    assert page["skipped"] == 0


def test_both_cursors_are_found(page):
    assert page["top_cursor"]
    assert page["bottom_cursor"]


def test_author_comes_from_the_core_object_not_legacy(page):
    handles = [post["handle"] for post in page["posts"]]
    assert handles == ["sydneyrunkle", "CShorten30"]
    assert page["posts"][0]["author"] == "Sydney Runkle"


def test_url_is_built_from_handle_and_id(page):
    post = page["posts"][0]
    assert post["url"] == f"https://x.com/sydneyrunkle/status/{post['id']}"


def test_timestamp_is_converted_to_iso(page):
    assert page["posts"][0]["posted_at"] == "2026-11-16T17:07:53+00:00"


def test_long_post_uses_note_tweet_text(page):
    post = page["posts"][1]
    assert post["long_form"] is True
    assert post["text"].startswith("Hey everyone!")
    assert len(post["text"]) > 100


def test_media_is_extracted(page):
    media = page["posts"][1]["media"]
    assert len(media) == 1
    assert media[0]["type"] == "photo"
    assert media[0]["url"].startswith("https://pbs.twimg.com/media/")


def test_quoted_post_is_extracted(page):
    quoted = page["posts"][0]["quoted"]
    assert quoted is not None
    assert quoted["handle"] == "hwchase17"
    assert quoted["id"]


def test_raw_is_kept_verbatim(page):
    raw = json.loads(page["posts"][0]["raw"])
    assert raw["rest_id"] == page["posts"][0]["id"]
    assert raw["legacy"]["bookmarked"] is True


def test_html_entities_are_unescaped():
    payload = {
        "data": {
            "bookmark_timeline_v2": {
                "timeline": {
                    "instructions": [
                        {
                            "entries": [
                                {
                                    "content": {
                                        "itemContent": {
                                            "tweet_results": {
                                                "result": {
                                                    "rest_id": "42",
                                                    "legacy": {"full_text": "a &amp; b &lt; c", "created_at": "Mon Nov 16 17:07:53 +0000 2026"},
                                                    "core": {"user_results": {"result": {"core": {"screen_name": "someone", "name": "Some One"}}}},
                                                }
                                            }
                                        }
                                    }
                                }
                            ]
                        }
                    ]
                }
            }
        }
    }
    posts = parse.parse_page(payload)["posts"]
    assert posts[0]["text"] == "a & b < c"


def test_tombstones_are_counted_not_crashed():
    payload = {
        "data": {
            "bookmark_timeline_v2": {
                "timeline": {
                    "instructions": [
                        {
                            "entries": [
                                {"content": {"itemContent": {"tweet_results": {"result": {"__typename": "TweetTombstone"}}}}},
                                {"content": {"cursorType": "Bottom", "value": "abc"}},
                            ]
                        }
                    ]
                }
            }
        }
    }
    page = parse.parse_page(payload)
    assert page["posts"] == []
    assert page["skipped"] == 1
    assert page["bottom_cursor"] == "abc"


def test_non_timeline_bodies_are_rejected():
    with pytest.raises(ValueError):
        parse.parse_body('{"data":{"viewer":{"user_results":{}}}}')
    with pytest.raises(ValueError):
        parse.parse_body("not json at all")


def test_iso_date_tolerates_junk():
    assert parse.iso_date("tomorrow") is None
    assert parse.iso_date(None) is None
