"""Correctness eval harness — measure the server against a golden query set.

``mcp-gtags-server eval --golden evals/golden.jsonl --root /path/to/tree``
runs every case in the golden set against a real indexed tree and prints
per-category pass rates plus two headline numbers:

- **recall** — of the cases that assert expected content (paths, callers,
  counts, guards, reachability), how many found everything they expected;
- **precision@1** — of the cases that assert a ranking (``top_path``), how
  many returned the expected site as the FIRST result.

The golden file is JSON Lines; blank lines and ``#`` comments are skipped.
Each case::

    {"id": "macro-sys_read", "category": "macro", "tool": "find_definition",
     "args": {"symbol": "sys_read"},
     "expect": {"paths": ["fs/read_write.c"], "top_path": ["fs/read_write.c"],
                "resolved_via": "macro:SYSCALL_DEFINE"}}

Supported ``expect`` checks (all optional, all must hold for a pass):

- ``min_results``      — envelope ``total`` is at least this
- ``paths``            — every listed path appears among result paths
- ``top_path``         — the first result's path is one of these (ranking)
- ``resolved_via``     — envelope/info field equals this exactly
- ``callers``          — every listed name appears as a ``caller``
- ``symbols``          — every listed name appears as a record ``symbol``
- ``in_tree``          — find_callees: every name among results.in_tree[].symbol
- ``body_contains``    — get_symbol_body: substring of at least one result body
- ``status``           — update_index: results.status equals this exactly
- ``suggestions_contain`` — every name among the envelope ``suggestions``
- ``fallback``         — envelope ``fallback`` field equals this exactly
- ``guard_variants_min`` / ``definition_count_min`` — symbol_info fields
- ``exported``         — symbol_info field equals this exactly
- ``path_found``       — reachability outcome equals this boolean
- ``config_filtered_min`` — at least this many results config-filtered

Expectations are deliberately **path-level, not line-level**, so the same
golden set holds across kernel versions; CI pins one tag anyway for a
stable headline number.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path


def _load_cases(golden: Path) -> list[dict]:
    cases = []
    for n, raw in enumerate(golden.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"error: {golden}:{n}: bad JSON: {exc}")
        for field in ("id", "category", "tool", "args", "expect"):
            if field not in case:
                raise SystemExit(f"error: {golden}:{n}: case missing '{field}'")
        cases.append(case)
    return cases


def _result_records(data: dict) -> list[dict]:
    results = data.get("results")
    if isinstance(results, list):
        return [r for r in results if isinstance(r, dict)]
    return []


def _check(case: dict, data: dict) -> tuple[list[str], bool | None, bool | None]:
    """Evaluate one tool response. Returns (failures, recall_ok, top_ok);
    the booleans are None when the case has no check of that type."""
    expect = case["expect"]
    failures: list[str] = []
    recall_checks = 0
    recall_failures = 0
    top_ok: bool | None = None

    def content(ok: bool, message: str) -> None:
        nonlocal recall_checks, recall_failures
        recall_checks += 1
        if not ok:
            recall_failures += 1
            failures.append(message)

    records = _result_records(data)
    info = data.get("results") if isinstance(data.get("results"), dict) else {}
    paths = [r.get("path") for r in records]

    if (n := expect.get("min_results")) is not None:
        content((data.get("total") or 0) >= n, f"total {data.get('total')} < {n}")
    for path in expect.get("paths", []):
        content(path in paths, f"expected path {path} not in results")
    for name in expect.get("callers", []):
        callers = [r.get("caller") for r in records]
        content(name in callers, f"expected caller {name} not found")
    for name in expect.get("symbols", []):
        symbols = [r.get("symbol") for r in records]
        content(name in symbols, f"expected symbol {name} not in results")
    for name in expect.get("in_tree", []):
        in_tree = [
            c.get("symbol") for c in info.get("in_tree", []) if isinstance(c, dict)
        ]
        content(name in in_tree, f"expected in-tree callee {name} not found")
    if (needle := expect.get("body_contains")) is not None:
        bodies = [r.get("body") or "" for r in records]
        content(
            any(needle in body for body in bodies),
            f"no result body contains {needle!r}",
        )
    if (status := expect.get("status")) is not None:
        got = info.get("status")
        content(got == status, f"status {got!r} != {status!r}")
    for name in expect.get("suggestions_contain", []):
        suggestions = data.get("suggestions") or []
        content(name in suggestions, f"expected suggestion {name} not offered")
    if (fallback := expect.get("fallback")) is not None:
        got = data.get("fallback")
        content(got == fallback, f"fallback {got!r} != {fallback!r}")
    if (via := expect.get("resolved_via")) is not None:
        got = data.get("resolved_via") or info.get("resolved_via")
        content(got == via, f"resolved_via {got!r} != {via!r}")
    if (n := expect.get("guard_variants_min")) is not None:
        got = info.get("guard_variants") or 0
        content(got >= n, f"guard_variants {got} < {n}")
    if (n := expect.get("definition_count_min")) is not None:
        got = info.get("definition_count") or 0
        content(got >= n, f"definition_count {got} < {n}")
    if (exported := expect.get("exported")) is not None:
        got = info.get("exported")
        content(got == exported, f"exported {got!r} != {exported!r}")
    if (found := expect.get("path_found")) is not None:
        got = info.get("path_found")
        content(got is found, f"path_found {got} != {found}")
    if (n := expect.get("config_filtered_min")) is not None:
        got = data.get("config_filtered") or 0
        content(got >= n, f"config_filtered {got} < {n}")

    if (tops := expect.get("top_path")) is not None:
        top = paths[0] if paths else None
        top_ok = top in tops
        if not top_ok:
            failures.append(f"top result {top} not in {tops}")

    recall_ok = (recall_failures == 0) if recall_checks else None
    return failures, recall_ok, top_ok


def run(golden: str, root: str | None, threshold: float) -> int:
    """Run the golden set; print the report; return a process exit code."""
    from . import server  # deferred: importing server wires the MCP app

    golden_path = Path(golden).expanduser()
    if not golden_path.is_file():
        print(f"error: golden set not found: {golden_path}", file=sys.stderr)
        return 2
    if not root:
        print("error: eval needs --root pointing at the tree to query", file=sys.stderr)
        return 2
    cases = _load_cases(golden_path)
    tools = {
        "find_definition": server.find_definition,
        "find_references": server.find_references,
        "get_symbol_body": server.get_symbol_body,
        "find_callers": server.find_callers,
        "summarize_references": server.summarize_references,
        "find_callees": server.find_callees,
        "symbol_info": server.symbol_info,
        "list_file_symbols": server.list_file_symbols,
        "reachability": server.reachability,
        "blast_radius": server.blast_radius,
        "update_index": server.update_index,
    }

    by_category: dict[str, list[bool]] = {}
    recall_hits = recall_total = 0
    top_hits = top_total = 0
    failed_lines: list[str] = []
    started = time.time()
    for case in cases:
        tool = tools.get(case["tool"])
        if tool is None:
            print(f"error: {case['id']}: unknown tool {case['tool']}", file=sys.stderr)
            return 2
        raw = tool(**{**case["args"], "project_root": root})
        data = json.loads(raw)
        if "error" in data:
            failures: list[str] = [data["error"]]
            recall_ok: bool | None = False
            top_ok: bool | None = None
        else:
            failures, recall_ok, top_ok = _check(case, data)
        passed = not failures
        by_category.setdefault(case["category"], []).append(passed)
        if recall_ok is not None:
            recall_total += 1
            recall_hits += recall_ok
        if top_ok is not None:
            top_total += 1
            top_hits += top_ok
        if failures:
            failed_lines.append(f"  FAIL {case['id']}: {'; '.join(failures)}")

    print(f"golden set: {golden_path} ({len(cases)} cases) against {root}\n")
    width = max(len(c) for c in by_category)
    for category in sorted(by_category):
        outcomes = by_category[category]
        print(f"  {category:<{width}}  {sum(outcomes):>3}/{len(outcomes)} passed")
    total_passed = sum(sum(o) for o in by_category.values())
    rate = total_passed / len(cases) if cases else 0.0
    print()
    if recall_total:
        print(f"  recall (expected content found): {recall_hits}/{recall_total} "
              f"= {recall_hits / recall_total:.1%}")
    if top_total:
        print(f"  precision@1 (top result right):  {top_hits}/{top_total} "
              f"= {top_hits / top_total:.1%}")
    print(f"  overall: {total_passed}/{len(cases)} = {rate:.1%} "
          f"(threshold {threshold:.0%}) in {time.time() - started:.1f}s")
    if failed_lines:
        print("\nfailures:")
        print("\n".join(failed_lines))
    ok = rate >= threshold
    print(f"\n{'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1
