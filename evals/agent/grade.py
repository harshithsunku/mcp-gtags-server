#!/usr/bin/env python3
"""Grade an A/B run: deterministic checks over agent answers, then a markdown report.

Reads questions.jsonl + results/<run-id>/<arm>/<qid>.json (parsed by run_ab.py),
writes results/<run-id>/scores.csv and results/<run-id>/report.md.
Regrading is free — fix a check, rerun this; never rerun the agent for grader bugs.

Usage:  python3 grade.py --run-id full
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_questions(path: Path) -> list[dict]:
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(json.loads(line))
    return out


def answer_line(text: str) -> str | None:
    """Last 'ANSWER: ...' line in the response, if any."""
    hits = re.findall(r"^\s*ANSWER:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
    return hits[-1].strip() if hits else None


def grade_case(q: dict, rec: dict) -> dict:
    checks = q.get("checks", {})
    text = (rec.get("result") or "")
    low = text.lower()
    ans = answer_line(text)
    failures: list[str] = []

    if rec.get("timed_out"):
        failures.append("timed_out")
    elif rec.get("is_error") or "result" not in rec:
        failures.append("agent_error")
    else:
        for needle in checks.get("contains_all") or []:
            if needle.lower() not in low:
                failures.append(f"missing:{needle}")
        any_list = checks.get("contains_any") or []
        if any_list and not any(n.lower() in low for n in any_list):
            failures.append(f"none_of:{'|'.join(any_list)}")
        if checks.get("regex") and not re.search(checks["regex"], text, re.IGNORECASE):
            failures.append(f"regex:{checks['regex']}")
        expected = checks.get("expected_yesno")
        if expected:
            # Prefer the ANSWER line; if it lacks a yes/no (agent answered with just a
            # symbol name, say), fall back to the first yes/no in the whole response.
            m = re.search(r"\b(yes|no)\b", ans or "", re.IGNORECASE) or \
                re.search(r"\b(yes|no)\b", text, re.IGNORECASE)
            got = m.group(1).lower() if m else None
            # Implied yes: naming the required evidence (contains_all satisfied) asserts
            # the path exists even without the literal word. Expected-"no" stays strict.
            all_ok = bool(checks.get("contains_all")) and not any(
                f.startswith("missing:") for f in failures)
            if got != expected and not (expected == "yes" and got is None and all_ok):
                failures.append(f"yesno:want={expected},got={got}")
        for bad in checks.get("forbidden") or []:
            if ans is not None and bad.lower() in ans.lower():
                failures.append(f"forbidden:{bad}")

    return {
        "id": q["id"], "tier": q["tier"], "category": q["category"], "arm": rec.get("arm"),
        "passed": not failures, "failures": ";".join(failures),
        "answer_line": ans or "", "parse_ok": ans is not None,
        "timed_out": bool(rec.get("timed_out")),
        "duration_ms": rec.get("duration_ms"), "num_turns": rec.get("num_turns"),
        "tool_calls": rec.get("tool_calls"), "mcp_tool_calls": rec.get("mcp_tool_calls"),
        "input_tokens": rec.get("input_tokens"), "output_tokens": rec.get("output_tokens"),
        "cost_usd": rec.get("total_cost_usd"),
        "answer_excerpt": (text[-400:] if text else "(no answer)"),
    }


def median(vals: list) -> float | str:
    vals = [v for v in vals if isinstance(v, (int, float))]
    return round(statistics.median(vals), 1) if vals else "-"


def rate(rows: list[dict]) -> str:
    if not rows:
        return "-"
    n = sum(1 for r in rows if r["passed"])
    return f"{n}/{len(rows)} ({100 * n / len(rows):.0f}%)"


def make_report(scores: list[dict], meta: dict, questions: list[dict]) -> str:
    arms = sorted({s["arm"] for s in scores if s["arm"]})
    by = lambda arm, **kw: [s for s in scores if s["arm"] == arm and all(s[k] == v for k, v in kw.items())]
    tiers = ["simple", "medium", "complex"]
    cats = sorted({q["category"] for q in questions})

    lines = ["# Agent A/B eval: gtags MCP vs no MCP", ""]
    lines += [f"- **Model:** {meta.get('model')}   **Kernel:** `{meta.get('kernel_sha', '?')[:12]}` at {meta.get('kernel_root')}",
              f"- **Run:** {meta.get('run_id')} started {meta.get('started')}   **CLI:** {meta.get('claude_version')}",
              f"- Arms: `gtags` = agent + gtags MCP (stdio, strict); `none` = same agent, no MCP (grep/read only).", ""]

    lines += ["## Correctness", "", "| slice | " + " | ".join(arms) + " |", "|---|" + "---|" * len(arms)]
    lines.append("| **overall** | " + " | ".join(rate(by(a)) for a in arms) + " |")
    for t in tiers:
        lines.append(f"| tier: {t} | " + " | ".join(rate(by(a, tier=t)) for a in arms) + " |")
    for c in cats:
        lines.append(f"| category: {c} | " + " | ".join(rate(by(a, category=c)) for a in arms) + " |")
    lines.append("")

    if {"gtags", "none"} <= set(arms):
        g = {s["id"]: s for s in scores if s["arm"] == "gtags"}
        n = {s["id"]: s for s in scores if s["arm"] == "none"}
        both = sorted(set(g) & set(n))
        flips_gtags = [i for i in both if g[i]["passed"] and not n[i]["passed"]]
        flips_none = [i for i in both if n[i]["passed"] and not g[i]["passed"]]
        lines += ["## Paired comparison (the honest headline)", "",
                  f"- gtags-pass / none-fail: **{len(flips_gtags)}** — {', '.join(flips_gtags) or '(none)'}",
                  f"- none-pass / gtags-fail: **{len(flips_none)}** — {', '.join(flips_none) or '(none)'}",
                  f"- both pass: {sum(1 for i in both if g[i]['passed'] and n[i]['passed'])}, "
                  f"both fail: {sum(1 for i in both if not g[i]['passed'] and not n[i]['passed'])}", ""]

    lines += ["## Efficiency (medians)", "", "| metric | " + " | ".join(arms) + " |", "|---|" + "---|" * len(arms)]
    for label, key in [("wall time (ms)", "duration_ms"), ("turns", "num_turns"),
                       ("tool calls", "tool_calls"), ("MCP tool calls", "mcp_tool_calls"),
                       ("output tokens", "output_tokens"), ("cost (USD)", "cost_usd")]:
        vals = []
        for a in arms:
            v = median([s[key] for s in by(a)])
            vals.append(f"{v:.3f}" if key == "cost_usd" and isinstance(v, float) else str(v))
        lines.append(f"| {label} | " + " | ".join(vals) + " |")
    for a in arms:
        rows = by(a)
        total_cost = sum(s["cost_usd"] or 0 for s in rows)
        used_mcp = sum(1 for s in rows if (s["mcp_tool_calls"] or 0) > 0)
        lines.append(f"| total cost `{a}` | ${total_cost:.2f} ({len(rows)} runs, MCP used in {used_mcp}) |" + " |" * (len(arms) - 1))
    lines.append("")

    diag = [f"- parse rate (ANSWER: line found): " +
            ", ".join(f"`{a}` {sum(1 for s in by(a) if s['parse_ok'])}/{len(by(a))}" for a in arms),
            f"- timeouts: " + ", ".join(f"`{a}` {sum(1 for s in by(a) if s['timed_out'])}" for a in arms)]
    lines += ["## Diagnostics", ""] + diag + [""]

    disagree = []
    if {"gtags", "none"} <= set(arms):
        disagree = [i for i in both if g[i]["passed"] != n[i]["passed"]]
    if disagree:
        lines += ["## Cases where the arms disagree", ""]
        qmap = {q["id"]: q for q in questions}
        for i in disagree:
            lines += [f"### {i} ({qmap[i]['tier']}/{qmap[i]['category']})",
                      f"> {qmap[i]['question']}", ""]
            for a in ("gtags", "none"):
                s = g[i] if a == "gtags" else n[i]
                verdict = "PASS" if s["passed"] else f"FAIL ({s['failures']})"
                ansline = s["answer_line"] or s["answer_excerpt"].replace("\n", " ")[:160]
                lines.append(f"- **{a}** {verdict} — `{ansline[:200]}`")
            lines.append("")

    failing = [s for s in scores if not s["passed"]]
    if failing:
        lines += ["## All failures (for grader audit)", ""]
        for s in failing:
            lines.append(f"- `{s['arm']}/{s['id']}`: {s['failures']} — answer: `{(s['answer_line'] or s['answer_excerpt'].replace(chr(10), ' '))[:160]}`")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--questions", default=str(HERE / "questions.jsonl"))
    args = ap.parse_args()

    run_dir = HERE / "results" / args.run_id
    meta = json.loads((run_dir / "meta.json").read_text()) if (run_dir / "meta.json").exists() else {}
    questions = load_questions(Path(args.questions))

    scores: list[dict] = []
    for arm_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
        for q in questions:
            f = arm_dir / f"{q['id']}.json"
            if f.exists():
                scores.append(grade_case(q, json.loads(f.read_text())))

    if not scores:
        raise SystemExit(f"no results found under {run_dir}")

    csv_path = run_dir / "scores.csv"
    fields = [k for k in scores[0] if k != "answer_excerpt"]
    with csv_path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(scores)

    report = make_report(scores, meta, questions)
    (run_dir / "report.md").write_text(report)
    print(f"wrote {csv_path} and {run_dir / 'report.md'}")
    for arm in sorted({s["arm"] for s in scores}):
        rows = [s for s in scores if s["arm"] == arm]
        print(f"  {arm}: {rate(rows)}")


if __name__ == "__main__":
    main()
