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
AUTHOR = "benjdicken"

CASES = [
    {
        "name": "full text on request",
        "question": "Read me the full text of the LRU cache debugging post by @GonnabeNikhil.",
        "expect": [],
        "answer_has": ["lru"],
        "min_citations": 1,
        "min_answer_chars": 1500,
        "require": [{"tool": "get_bookmark", "arg": "full"}],
    },
    {
        "name": "follow-up keeps the person",
        "turns": ["who do I bookmark most?", "show me all posts of ben"],
        "expect": [],
        "stop_after": None,
        "answer_has": [],
        "min_citations": 7,
        "only_handle": AUTHOR,
        "forbid": ["vector_search"],
    },
    {
        "name": "exact author listing",
        "question": "can you give me all posts by Ben Dicken?",
        "expect": ["sql_query"],
        "stop_after": "sql_query",
        "answer_has": ["7"],
        "min_citations": 7,
    },
    {
        "name": "count by month",
        "question": "how many bookmarks did I save in November 2026?",
        "expect": ["sql_query"],
        "stop_after": "sql_query",
        "answer_has": [],
    },
    {
        "name": "sql answer carries links",
        "question": "show me the three most recent posts I saved, with links",
        "expect": ["sql_query"],
        "stop_after": "sql_query",
        "answer_has": [],
        "min_citations": 3,
    },
    {
        "name": "read one known post",
        "question": "what does the Varnan hiring post say?",
        "expect": [],
        "stop_after": None,
        "answer_has": ["varnan"],
        "min_citations": 1,
    },
    {
        "name": "absence is allowed",
        "question": "did I save anything about the Rust borrow checker?",
        "expect": [],
        "stop_after": None,
        "answer_has": [],
    },
]


def ask(token: str, question: str, history=None, ledger=None) -> dict:
    body = json.dumps({"question": question, "history": history or [], "ledger": ledger or []}).encode()
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

    for want in case.get("require") or []:
        if not any(
            step.get("tool") == want["tool"] and (step.get("args") or {}).get(want["arg"])
            for step in result.get("tools") or []
        ):
            problems.append(f"expected {want['tool']} with {want['arg']}=true")

    for name in case.get("forbid") or []:
        if name in tools:
            problems.append(f"{name} must not run here")

    stop = case.get("stop_after")
    if stop and stop in tools:
        after = tools[tools.index(stop) + 1 :]
        leaked = [name for name in after if name in SEARCH_TOOLS]
        if leaked:
            problems.append(f"searched again after exact {stop}: {leaked}")

    answer = (result.get("answer") or "").lower()
    if len(answer) < int(case.get("min_answer_chars") or 0):
        problems.append(f"answer is only {len(answer)} characters")
    for needle in case["answer_has"]:
        if needle.lower() not in answer:
            problems.append(f"answer lacks '{needle}'")
    if not answer:
        problems.append("empty answer")

    citations = result.get("citations") or []
    wanted = case.get("min_citations", 0)
    if len(citations) < wanted:
        problems.append(f"expected >= {wanted} citations, got {len(citations)}")
    only = case.get("only_handle")
    for row in citations:
        if not row.get("url"):
            problems.append(f"citation [{row.get('n')}] has no url")
        if only and only not in str(row.get("handle") or "").lower():
            problems.append(f"citation [{row.get('n')}] is @{row.get('handle')}, expected @{only}")
    if citations:
        numbers = sorted(row.get("n") for row in citations if row.get("n"))
        if numbers != list(range(1, len(numbers) + 1)):
            problems.append(f"citation numbers are not 1..n: {numbers}")
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
        turns = case.get("turns") or [case["question"]]
        history, ledger, result = [], [], {}
        for question in turns:
            result = ask(token, question, history, ledger)
            if result.get("error"):
                break
            history += [
                {"role": "user", "content": question},
                {"role": "assistant", "content": result.get("answer") or ""},
            ]
            ledger += [
                f"- {step.get('tool')} {step.get('args')} -> {step.get('result')}"
                for step in result.get("tools") or []
                if step.get("tool") != "model"
            ]
        problems = check(case, result)
        took = time.monotonic() - started
        tools = " -> ".join(step.get("tool", "?") for step in result.get("tools") or []) or "none"
        print(f"[{'pass' if not problems else 'FAIL'}] {case['name']}  ({took:.0f}s)")
        for question in turns:
            print(f"       q:     {question}")
        print(f"       tools: {tools}")
        for step in result.get("tools") or []:
            print(f"              {step.get('tool')}: {str(step.get('result'))[:90]}")
        if result.get("answer"):
            print(f"       a:     {result['answer'][:150]}")
        chips = result.get("citations") or []
        print(f"       chips: {len(chips)}" + (f" -> {[row.get('n') for row in chips]}" if chips else ""))
        for problem in problems:
            print(f"       ! {problem}")
        print()
        failures += len(problems) > 0

    print(f"{len(CASES) - failures}/{len(CASES)} cases pass")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
