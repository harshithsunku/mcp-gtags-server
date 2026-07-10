#!/usr/bin/env python3
"""A/B runner: ask an agent (claude -p) kernel questions with and without the gtags MCP.

Arm "gtags" gets the MCP over stdio (mcp-gtags.json); arm "none" gets an empty
config. --strict-mcp-config keeps both arms hermetic. Transcripts land in
results/<run-id>/<arm>/<qid>.stream.jsonl, parsed records in <qid>.json.
Resumable: existing parsed records are skipped unless --force.

Usage:
  python3 run_ab.py --run-id dry1 --only q01-def-vfs_read,q22-macro-sys_read
  python3 run_ab.py --run-id full --kernel-root /home/ai/linux
"""
from __future__ import annotations

import argparse
import datetime
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

ANSWER_CONTRACT = (
    "You are answering a question about the Linux kernel source tree in the current "
    "directory. Investigate the actual source; do not answer from memory alone and do "
    "not use the internet. Be concise. End your response with a single line of the form "
    "'ANSWER: <value>' where <value> is a repo-relative path (optionally path:line), a "
    "comma-separated list of symbol names, 'yes' or 'no', or a CONFIG_ option name, "
    "as the question requires."
)


def load_questions(path: Path) -> list[dict]:
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(json.loads(line))
    return out


def find_claude(explicit: str | None) -> str:
    if explicit:
        return explicit
    found = shutil.which("claude")
    if found:
        return found
    bundled = sorted(
        Path.home().glob(".vscode-server/extensions/anthropic.claude-code-*/resources/native-binary/claude")
    )
    if bundled:
        return str(bundled[-1])
    sys.exit("error: claude CLI not found; pass --claude-bin")


def parse_stream(stream_path: Path) -> dict:
    """Extract the final result event + tool-call counts from a stream-json transcript."""
    rec: dict = {"tool_calls": 0, "mcp_tool_calls": 0, "tool_names": {}}
    result_event = None
    for line in stream_path.read_text().splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    name = block.get("name", "?")
                    rec["tool_calls"] += 1
                    if name.startswith("mcp__gtags"):
                        rec["mcp_tool_calls"] += 1
                    rec["tool_names"][name] = rec["tool_names"].get(name, 0) + 1
        elif ev.get("type") == "result":
            result_event = ev
    if result_event is None:
        rec["error"] = "no result event in transcript"
        return rec
    usage = result_event.get("usage", {})
    rec.update(
        result=result_event.get("result", ""),
        is_error=result_event.get("is_error", False),
        duration_ms=result_event.get("duration_ms"),
        num_turns=result_event.get("num_turns"),
        total_cost_usd=result_event.get("total_cost_usd"),
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        cache_read_input_tokens=usage.get("cache_read_input_tokens"),
    )
    return rec


def run_one(q: dict, arm: str, args, claude_bin: str, out_dir: Path) -> dict:
    stream_path = out_dir / f"{q['id']}.stream.jsonl"
    mcp_config = HERE / ("mcp-gtags.json" if arm == "gtags" else "mcp-none.json")
    cmd = [
        claude_bin, "-p", q["question"],
        "--model", args.model,
        "--output-format", "stream-json", "--verbose",
        "--mcp-config", str(mcp_config), "--strict-mcp-config",
        "--disallowedTools", "WebSearch,WebFetch,Write,Edit,NotebookEdit",
        "--append-system-prompt", ANSWER_CONTRACT,
    ]
    started = datetime.datetime.now(datetime.timezone.utc).isoformat()
    timed_out = False
    try:
        with stream_path.open("w") as fh:
            subprocess.run(
                cmd, cwd=args.kernel_root, stdout=fh,
                stderr=subprocess.PIPE, timeout=args.timeout, check=False,
            )
    except subprocess.TimeoutExpired:
        timed_out = True
    rec = parse_stream(stream_path) if stream_path.exists() else {}
    rec.update(id=q["id"], arm=arm, timed_out=timed_out, started=started)
    return rec


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--questions", default=str(HERE / "questions.jsonl"))
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--kernel-root", default="/home/ai/linux")
    ap.add_argument("--model", default="claude-sonnet-5")
    ap.add_argument("--timeout", type=int, default=300, help="seconds per agent run")
    ap.add_argument("--arms", default="gtags,none")
    ap.add_argument("--only", default="", help="comma-separated question ids")
    ap.add_argument("--tier", default="", help="filter: simple|medium|complex")
    ap.add_argument("--force", action="store_true", help="redo existing results")
    ap.add_argument("--claude-bin", default=None)
    args = ap.parse_args()

    questions = load_questions(Path(args.questions))
    if args.only:
        wanted = set(args.only.split(","))
        questions = [q for q in questions if q["id"] in wanted]
    if args.tier:
        questions = [q for q in questions if q["tier"] == args.tier]
    arms = args.arms.split(",")
    claude_bin = find_claude(args.claude_bin)

    run_dir = HERE / "results" / args.run_id
    for arm in arms:
        (run_dir / arm).mkdir(parents=True, exist_ok=True)

    meta_path = run_dir / "meta.json"
    if not meta_path.exists() or args.force:
        kernel_sha = subprocess.run(
            ["git", "-C", args.kernel_root, "rev-parse", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip() or "unknown (not a git tree)"
        cli_ver = subprocess.run(
            [claude_bin, "--version"], capture_output=True, text=True
        ).stdout.strip()
        meta_path.write_text(json.dumps({
            "run_id": args.run_id, "model": args.model, "kernel_root": args.kernel_root,
            "kernel_sha": kernel_sha, "claude_version": cli_ver,
            "timeout_s": args.timeout, "n_questions": len(questions),
            "started": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }, indent=2))

    total = len(questions) * len(arms)
    done = 0
    for q in questions:  # interleave arms per question so drift hits both equally
        for arm in arms:
            done += 1
            parsed_path = run_dir / arm / f"{q['id']}.json"
            if parsed_path.exists() and not args.force:
                print(f"[{done}/{total}] skip {arm}/{q['id']} (exists)")
                continue
            print(f"[{done}/{total}] run  {arm}/{q['id']} ...", flush=True)
            rec = run_one(q, arm, args, claude_bin, run_dir / arm)
            parsed_path.write_text(json.dumps(rec, indent=2))
            status = "TIMEOUT" if rec.get("timed_out") else f"{rec.get('duration_ms', '?')}ms"
            print(f"          -> {status}, turns={rec.get('num_turns')}, "
                  f"tools={rec.get('tool_calls')} (mcp={rec.get('mcp_tool_calls')}), "
                  f"cost=${rec.get('total_cost_usd') or 0:.3f}", flush=True)
    print(f"done: {done} runs in {run_dir}")


if __name__ == "__main__":
    main()
