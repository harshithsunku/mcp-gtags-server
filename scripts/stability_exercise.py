#!/usr/bin/env python3
"""Kernel-scale stability exercise: run every tool against a real tree.

Complements the golden-set eval (correctness) with an operational view:
per-call latency (cold + warm), response size, and envelope sanity for a
representative input matrix over all 11 tools — including deliberate
stress inputs (a breadth-guard trigger, a fallback query, a suggestion
miss, a no-path reachability pair).

Usage::

    python scripts/stability_exercise.py --root /path/to/linux
    python scripts/stability_exercise.py --root /path/to/linux --json out.json
    python scripts/stability_exercise.py --root /path/to/linux --allow-edit

``--allow-edit`` additionally exercises blast_radius against a real change:
it appends a trailing space inside fs/read_write.c (whitespace-only), runs
the tool, and restores the file with ``git checkout --``. The edit is only
made when the file is clean in git; the restore runs even on failure.

``--json`` writes machine-readable results for diffing between runs.
Exit code 0 = zero anomalies, 1 = at least one.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from gtags_mcp import server  # noqa: E402


@dataclass
class Case:
    name: str
    tool: str
    args: dict
    # Sanity spec: exactly one of these describes the expected outcome.
    expect_error: str | None = None  # substring of the error envelope
    expect_message: str | None = None  # substring of an empty-result message
    min_results: int = 1  # else: envelope total must be at least this
    extra_check: object = None  # callable(data) -> str | None (anomaly reason)
    result: dict = field(default_factory=dict)


def _check(case: Case, data: dict) -> str | None:
    """Return an anomaly description, or None when the response is sane."""
    if case.expect_error is not None:
        if "error" not in data:
            return f"expected an error containing {case.expect_error!r}, got success"
        if case.expect_error not in data["error"]:
            return f"error {data['error']!r} lacks {case.expect_error!r}"
        return None
    if "error" in data:
        return f"unexpected error: {data['error']}"
    if case.expect_message is not None:
        message = data.get("message", "")
        if case.expect_message not in message:
            return f"message {message!r} lacks {case.expect_message!r}"
        return None
    total = data.get("total")
    if total is not None and total < case.min_results:
        return f"total {total} < {case.min_results}"
    if case.extra_check is not None:
        return case.extra_check(data)
    return None


def _matrix(root: str) -> list[Case]:
    def reach_found(data):
        return None if data["results"]["path_found"] else "expected path_found=true"

    def reach_not_found(data):
        return "expected path_found=false" if data["results"]["path_found"] else None

    def callees_in_tree(name):
        def check(data):
            got = [c["symbol"] for c in data["results"]["in_tree"]]
            return None if name in got else f"expected in-tree callee {name}, got {got}"
        return check

    def status_ok(data):
        return None if data["results"].get("status") == "ok" else "status != ok"

    def has_suggestions(data):
        return None if data.get("suggestions") else "expected suggestions on the miss"

    def recovered_from_export(data):
        paths = [r["path"] for r in data["results"]]
        if "kernel/locking/mutex.c" not in paths:
            return f"expected recovered kernel/locking/mutex.c, got {paths[:3]}"
        if not str(data.get("resolved_via", "")).startswith("ctags:"):
            return f"expected ctags resolved_via, got {data.get('resolved_via')!r}"
        return None

    return [
        # find_definition: plain, multi-def guarded, macro-generated,
        # parser-missed (ctags export recovery), miss.
        Case("def-plain", "find_definition", {"symbol": "vfs_read"}),
        Case("def-guarded-kmap", "find_definition", {"symbol": "kmap"}, min_results=2),
        Case("def-macro-sys_read", "find_definition", {"symbol": "sys_read"}),
        Case(
            "def-recovered-mutex_lock", "find_definition", {"symbol": "mutex_lock"},
            extra_check=recovered_from_export,
        ),
        Case("def-struct", "find_definition", {"symbol": "task_struct"}),
        Case(
            "def-miss-suggestions", "find_definition", {"symbol": "tcp_v4_r"},
            min_results=0, extra_check=has_suggestions,
        ),
        # find_references: moderate, huge (paginated), fallback.
        Case("refs-moderate", "find_references", {"symbol": "rw_verify_area"}),
        Case("refs-hot-kmalloc", "find_references", {"symbol": "kmalloc", "limit": 100}, min_results=2000),
        Case("refs-fallback", "find_references", {"symbol": "__GNUC__", "limit": 10}, min_results=50),
        # get_symbol_body: big function, struct, continuation macro.
        Case("body-function", "get_symbol_body", {"symbol": "tcp_v4_rcv"}),
        Case("body-struct", "get_symbol_body", {"symbol": "list_head"}),
        Case("body-macro", "get_symbol_body", {"symbol": "wait_event"}),
        # find_callers: small, medium, wide (the serial per-file suspect),
        # and the >500-file breadth guard.
        Case("callers-small", "find_callers", {"symbol": "rw_verify_area"}),
        Case("callers-medium", "find_callers", {"symbol": "vfs_read"}),
        Case("callers-wide", "find_callers", {"symbol": "ext4_mark_inode_dirty", "limit": 200}, min_results=30),
        Case(
            "callers-breadth-guard", "find_callers", {"symbol": "kmalloc"},
            expect_error="too broad",
        ),
        # summarize_references: the cheap wide view.
        Case("summary-hot", "summarize_references", {"symbol": "kmalloc", "limit": 10}, min_results=1000),
        Case("summary-medium", "summarize_references", {"symbol": "schedule", "limit": 10}, min_results=200),
        # find_callees.
        Case("callees-vfs_read", "find_callees", {"symbol": "vfs_read"}, extra_check=callees_in_tree("rw_verify_area")),
        Case("callees-kernel_clone", "find_callees", {"symbol": "kernel_clone"}, extra_check=callees_in_tree("copy_process")),
        # symbol_info: widespread symbol, guard-variant symbol.
        Case("info-hot", "symbol_info", {"symbol": "kmalloc"}),
        Case("info-guarded", "symbol_info", {"symbol": "kmap"}),
        # reachability: known path and honest no-path (function pointers).
        Case(
            "reach-path", "reachability",
            {"from_symbol": "ksys_read", "to_symbol": "rw_verify_area"},
            extra_check=reach_found,
        ),
        Case(
            "reach-no-path", "reachability",
            {"from_symbol": "vfs_read", "to_symbol": "ext4_file_read_iter", "max_depth": 4},
            min_results=0, extra_check=reach_not_found,
        ),
        # list_file_symbols.
        Case("filesurface", "list_file_symbols", {"file_path": "fs/read_write.c", "limit": 500}, min_results=50),
        # blast_radius on the clean tree (the edit variant runs separately).
        Case("blast-clean", "blast_radius", {}, min_results=0, expect_message="no changes"),
        # update_index: synchronous incremental refresh timing.
        Case("maint-incremental", "update_index", {}, min_results=0, extra_check=status_ok),
    ]


def _run_case(case: Case, root: str) -> None:
    tool = getattr(server, case.tool)
    timings = []
    raw = ""
    for _ in range(2):  # cold, then warm
        started = time.monotonic()
        raw = tool(**{**case.args, "project_root": root})
        timings.append((time.monotonic() - started) * 1000.0)
    try:
        data = json.loads(raw)
        anomaly = _check(case, data)
    except json.JSONDecodeError as exc:
        anomaly = f"response is not JSON: {exc}"
    case.result = {
        "name": case.name,
        "tool": case.tool,
        "cold_ms": round(timings[0], 1),
        "warm_ms": round(timings[1], 1),
        "bytes": len(raw),
        "ok": anomaly is None,
        "anomaly": anomaly,
    }


def _git(root: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", root, *args], capture_output=True, text=True
    )


BLAST_EDIT_FILE = "fs/read_write.c"


def _blast_with_edit(root: str) -> Case:
    """Whitespace-edit one indexed file, run blast_radius, always restore."""
    case = Case("blast-real-edit", "blast_radius", {"depth": 1})
    status = _git(root, "status", "--porcelain", "--", BLAST_EDIT_FILE)
    if status.returncode != 0 or status.stdout.strip():
        case.result = {
            "name": case.name, "tool": case.tool, "cold_ms": None, "warm_ms": None,
            "bytes": 0, "ok": False,
            "anomaly": f"{BLAST_EDIT_FILE} is not clean in git; refusing to edit",
        }
        return case
    target = Path(root) / BLAST_EDIT_FILE
    source = target.read_text()
    # Whitespace-only change INSIDE a function so the diff maps to it.
    needle = "\treturn ret;"
    target.write_text(source.replace(needle, needle + " ", 1), encoding="utf-8")
    try:
        server.update_index(project_root=root)  # freshness barrier before diffing
        started = time.monotonic()
        raw = server.blast_radius(project_root=root, depth=1)
        elapsed = (time.monotonic() - started) * 1000.0
        data = json.loads(raw)
        anomaly = None
        if "error" in data:
            anomaly = f"unexpected error: {data['error']}"
        elif not data.get("changed_functions"):
            anomaly = "whitespace edit mapped to no changed function"
        elif not any(r["distance"] == 1 for r in data["results"]):
            anomaly = "no direct caller found at depth 1"
        case.result = {
            "name": case.name, "tool": case.tool, "cold_ms": round(elapsed, 1),
            "warm_ms": None, "bytes": len(raw), "ok": anomaly is None,
            "anomaly": anomaly,
        }
    finally:
        _git(root, "checkout", "--", BLAST_EDIT_FILE)
        server.update_index(project_root=root)  # drop the edit from the index too
    return case


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", required=True, help="indexed tree to exercise")
    parser.add_argument("--json", help="write machine-readable results to this file")
    parser.add_argument(
        "--allow-edit", action="store_true",
        help=f"exercise blast_radius via a whitespace edit to {BLAST_EDIT_FILE} "
        "(restored with `git checkout --` afterwards)",
    )
    args = parser.parse_args()
    root = str(Path(args.root).expanduser().resolve())

    cases = _matrix(root)
    for case in cases:  # sequential on purpose: measurements, small machines
        _run_case(case, root)
        print(".", end="", flush=True)
    if args.allow_edit:
        cases.append(_blast_with_edit(root))
        print(".", end="", flush=True)
    print()

    rows = [case.result for case in cases]
    name_w = max(len(r["name"]) for r in rows)
    tool_w = max(len(r["tool"]) for r in rows)
    print(f"\nstability exercise against {root} ({len(rows)} calls)\n")
    print(f"  {'case':<{name_w}}  {'tool':<{tool_w}}  {'cold ms':>8}  {'warm ms':>8}  {'bytes':>8}")
    for r in rows:
        cold = f"{r['cold_ms']:.0f}" if r["cold_ms"] is not None else "-"
        warm = f"{r['warm_ms']:.0f}" if r["warm_ms"] is not None else "-"
        flag = "" if r["ok"] else "  <-- ANOMALY"
        print(f"  {r['name']:<{name_w}}  {r['tool']:<{tool_w}}  {cold:>8}  {warm:>8}  {r['bytes']:>8}{flag}")

    anomalies = [r for r in rows if not r["ok"]]
    print(f"\n  {len(rows) - len(anomalies)}/{len(rows)} sane; "
          f"{len(anomalies)} anomal{'y' if len(anomalies) == 1 else 'ies'}")
    for r in anomalies:
        print(f"    {r['name']}: {r['anomaly']}")

    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2) + "\n")
        print(f"\n  results written to {args.json}")
    return 1 if anomalies else 0


if __name__ == "__main__":
    sys.exit(main())
