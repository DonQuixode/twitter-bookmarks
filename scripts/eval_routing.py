#!/usr/bin/env python3
"""Tool-routing eval for the bookmark chat.

Replays fixed questions against the deployed Worker and checks WHICH tools ran,
not only what the answer says. The tool sequence is the part that broke: the
model called sql_query, got the exact rows, then searched again and cited the
noise that came back.

The eval calls POST /eval/ask, which is guarded by INGEST_TOKEN and writes
nothing to the database.

Run: python3 scripts/eval_routing.py
Env: TB_BASE overrides the worker URL, TB_TOKEN overrides the token file.
"""

import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("TB_BASE", "https://twitter-bookmarks.umaralikhan1299.workers.dev").rstrip("/")
TOKEN_FILE = pathlib.Path(
    os.environ.get("TB_TOKEN", os.path.expanduser("~/.config/twitter-bookmarks/token"))
)
CASE_TIMEOUT = 180
SEARCH_TOOLS = {"search_bookmarks", "vector_search"}

CASES = [
    {
        "name": "exact author listing",
        "question": "can you give me all posts by Ben Dicken?",
        "expect": ["sql_query"],
        "stop_after": "sql_query",
        "answer_has": ["7"],
    },
    {
        "name": "count by month",
        "question": "how many bookmarks did I save in November 2026?",
        "expect": ["sql_query"],
        "stop_after": "sql_query",
        "answer_has": [],
    },
    {
        "name": "read one known post",
        "question": "what does the Varnan hiring post say?",
        "expect": [],
        "stop_after": None,
        "answer_has": ["varnan"],
    },
    {
        "name": "absence is allowed",
        "question": "did I save anything about the Rust borrow checker?",
        "expect": [],
        "stop_after": None,
        "answer_has": [],
    },
]


def ask(token: str, question: str) -> dict:
    body = json.dumps({"question": question}).encode()
    request = urllib.request.Request(
        f"{BASE}/eval/ask",
        data=body,
        headers={
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
            "user-agent": "twitter-bookmarks-eval/1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=CASE_TIMEOUT) as response:
            raw = response.read()
            if not raw.strip():
                return {"error": f"empty body, status {response.status}"}
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"error": f"not JSON, status {response.status}: {raw[:160]!r}"}
    except urllib.error.HTTPError as error:
        return {"error": f"status {error.code}: {error.read()[:200].decode('utf-8', 'replace')}"}


def check(case: dict, result: dict) -> list[str]:
    if result.get("error"):
        return [result["error"]]

    problems = []
    tools = [step.get("tool") for step in result.get("tools") or []]

    for name in case["expect"]:
        if name not in tools:
            problems.append(f"expected {name}, ran {tools or 'no tools'}")

    stop = case["stop_after"]
    if stop and stop in tools:
        after = tools[tools.index(stop) + 1 :]
        leaked = [name for name in after if name in SEARCH_TOOLS]
        if leaked:
            problems.append(f"searched again after exact {stop}: {leaked}")

    answer = (result.get("answer") or "").lower()
    for needle in case["answer_has"]:
        if needle.lower() not in answer:
            problems.append(f"answer lacks '{needle}'")
    if not answer:
        problems.append("empty answer")
    return problems


def main() -> int:
    token = os.environ.get("TB_TOKEN_VALUE") or (
        TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else ""
    )
    if not token:
        sys.exit(f"no ingest token: set TB_TOKEN_VALUE or create {TOKEN_FILE}")

    print(f"routing eval against {BASE}\n")
    failures = 0
    for case in CASES:
        started = time.monotonic()
        result = ask(token, case["question"])
        problems = check(case, result)
        took = time.monotonic() - started
        tools = " -> ".join(step.get("tool", "?") for step in result.get("tools") or []) or "none"
        print(f"[{'pass' if not problems else 'FAIL'}] {case['name']}  ({took:.0f}s)")
        print(f"       q:     {case['question']}")
        print(f"       tools: {tools}")
        for step in result.get("tools") or []:
            print(f"              {step.get('tool')}: {str(step.get('result'))[:90]}")
        if result.get("answer"):
            print(f"       a:     {result['answer'][:150]}")
        for problem in problems:
            print(f"       ! {problem}")
        print()
        failures += len(problems) > 0

    print(f"{len(CASES) - failures}/{len(CASES)} cases pass")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
