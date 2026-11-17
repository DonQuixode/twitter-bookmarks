"""History replay, the tool ledger, and author matching (D20).

The ledger is the reference mechanism: the model sees that a call happened and
what it returned, never the 50-row payload again.
"""

import base64
import binascii
import hashlib
import hmac
import json
import pathlib
import re

SOURCE = pathlib.Path("src/main.py").read_text()


def load(*names, extra=None):
    def unb64(text):
        return base64.b64decode(text, validate=True)

    def pbkdf2_sha256(password, salt, iterations, length=32):
        return hashlib.pbkdf2_hmac("sha256", password, salt, iterations, length)

    namespace = {
        "re": re,
        "json": json,
        "hmac": hmac,
        "binascii": binascii,
        "unb64": unb64,
        "pbkdf2_sha256": pbkdf2_sha256,
        "list": list,
        "dict": dict,
        "str": str,
        "int": int,
        "len": len,
    }
    namespace.update(extra or {})
    # Module constants the functions read at call time; parsed so drift shows up here.
    for name in ("LEDGER_CALLS", "HISTORY_CHAR_LIMIT"):
        found = re.search(rf"^{name} = (\d+)", SOURCE, re.MULTILINE)
        assert found, f"{name} not found in main.py"
        namespace[name] = int(found.group(1))
    out = {}
    for name in names:
        start = SOURCE.index(f"def {name}(")
        # The next top-level function, sync or async.
        following = re.search(r"\n(?:async )?def ", SOURCE[start + 10 :])
        end = start + 10 + following.start() if following else len(SOURCE)
        exec(compile(SOURCE[start:end], "main.py", "exec"), namespace)
        out[name] = namespace[name]
    return out if len(out) > 1 else out[names[0]]


def test_author_matches_handle_or_name():
    matches = load("matches_author")
    row = {"handle": "BenjDicken", "author": "Ben Dicken"}
    assert matches(row, "Ben Dicken")
    assert matches(row, "@BenjDicken")
    assert matches(row, "ben dicken")
    assert not matches(row, "Ben Smith")
    assert not matches(row, "")


def test_ledger_is_one_line_per_call_without_payload():
    ledger = load("ledger_lines")
    traces = [
        [
            {"tool": "sql_query", "args": {"sql": "SELECT id FROM posts"}, "result": "7 rows (exact, complete)"},
            {"tool": "model", "args": {}, "result": "deepseek: timeout"},
        ],
        [{"tool": "search_bookmarks", "args": {"query": "zig"}, "result": "3 posts via hybrid"}],
    ]
    lines = ledger(traces)
    assert len(lines) == 2, "the model bookkeeping entry is not a tool call"
    assert "sql_query" in lines[0] and "7 rows" in lines[0]
    assert "search_bookmarks" in lines[1]
    assert "SELECT id FROM posts" in lines[0]


def test_ledger_is_capped_to_the_newest_calls():
    ledger = load("ledger_lines")
    traces = [[{"tool": f"t{i}", "args": {}, "result": "x"}] for i in range(30)]
    assert len(ledger(traces)) == 12
    assert "t29" in ledger(traces)[-1]


def test_history_keeps_order_and_skips_empty_content():
    history = load("history_messages")
    rows = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": None},
        {"role": "assistant", "content": "second"},
        {"role": "tool", "content": "not replayed"},
    ]
    assert history(rows) == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
    ]


def test_history_trims_oldest_first_when_huge():
    history = load("history_messages")
    rows = [{"role": "user", "content": "x" * 100} for _ in range(10)]
    kept = history(rows, limit=250)
    assert len(kept) == 3, "two full messages plus the omission note"
    assert kept[0]["content"].startswith("(Earlier turns omitted")


def test_prompt_carries_the_ledger_and_the_honesty_rule():
    prompt = load("build_prompt", extra={"SYSTEM_PROMPT": "BASE"})
    assert prompt([]) == "BASE"
    built = prompt(["- sql_query {} -> 7 rows"])
    assert built.startswith("BASE")
    assert "summarized, not reproduced" in built
    assert "re-issue the call" in built
