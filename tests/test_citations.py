"""Citations: only what the answer used, and SQL rows must be linkable.

Both rules come from D19. They lived inline in two routes, which is why the
fallback bug survived in both copies.
"""

import base64
import binascii
import hashlib
import hmac
import pathlib
import re

SOURCE = pathlib.Path("src/main.py").read_text()


def load(name: str):
    """Pull one top-level function out of main.py without importing pyodide."""
    start = SOURCE.index(f"def {name}(")
    end = SOURCE.index("\ndef ", start + 10)
    body = SOURCE[start:end]

    def unb64(text):
        return base64.b64decode(text, validate=True)

    def pbkdf2_sha256(password, salt, iterations, length=32):
        return hashlib.pbkdf2_hmac("sha256", password, salt, iterations, length)

    namespace = {
        "re": re,
        "hmac": hmac,
        "binascii": binascii,
        "unb64": unb64,
        "pbkdf2_sha256": pbkdf2_sha256,
        "list": list,
        "dict": dict,
    }
    exec(compile(body, "main.py", "exec"), namespace)
    return namespace[name]


cited_sources = load("cited_sources")
register = load("register")


def post(post_id, url=None):
    return {"id": post_id, "handle": "BenjDicken", "url": url, "posted_at": "2026-11-17", "text": "hi"}


def test_only_cited_sources_survive():
    sources = [
        {"n": 1, "id": "a", "handle": "a"},
        {"n": 2, "id": "b", "handle": "b"},
        {"n": 3, "id": "c", "handle": "c"},
    ]
    assert cited_sources(sources, "Two posts matter [2] and [0x].") == [sources[1]]
    assert cited_sources(sources, "See [1][3].") == [sources[0], sources[2]]


def test_uncited_answer_shows_nothing():
    """The old 'or slim_all' fallback showed nearest neighbours as sources."""
    sources = [{"n": 1, "id": "a", "handle": "a"}]
    assert cited_sources(sources, "No numbers here.") == []


def test_sql_rows_get_numbers_and_a_url():
    registry = {}
    rows = [post("111"), post("222", url="https://x.com/BenjDicken/status/222")]
    numbers = {item["id"]: item["n"] for item in register(rows, registry, 50)}
    assert numbers == {"111": 1, "222": 2}
    # A projection without url still yields a working link.
    assert registry["111"]["url"] == "https://x.com/i/web/status/111"
    assert registry["222"]["url"] == "https://x.com/BenjDicken/status/222"


def test_aggregate_rows_are_skipped():
    registry = {}
    assert register([{"author": "Ben Dicken", "count": 7}], registry, 50) == []
    assert registry == {}


def test_numbers_are_stable_across_tools():
    registry = {}
    assert register([post("111")], registry, 50)[0]["n"] == 1
    # The same post found again by search must not get a second number.
    assert register([post("111")], registry, 50)[0]["n"] == 1
    assert register([post("333")], registry, 50)[0]["n"] == 2
