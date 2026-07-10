# Agent A/B eval: does the gtags MCP make an agent better on kernel questions?

The server eval (`evals/golden.jsonl`) proves the *tools* answer correctly. This eval
measures the thing users actually care about: an **agent** (Claude Code headless) gets the
same 50 natural-language kernel questions twice — once with the gtags MCP attached, once
without (grep/read only) — and both arms are graded against the same verified ground truth.

## Layout

| file | purpose |
|---|---|
| `questions.jsonl` | 50 questions, simple → complex, with deterministic checks; header records the kernel SHA the truth was verified on |
| `mcp-gtags.json` | arm A config: gtags MCP over stdio (edit `--root`/`--directory` paths for your machine) |
| `mcp-none.json` | arm B config: no MCP servers |
| `run_ab.py` | runner; invokes `claude -p` per question × arm, saves transcripts + parsed records |
| `grade.py` | grader; emits `scores.csv` + `report.md` per run |
| `results/<run-id>/` | local only (gitignored): `meta.json`, per-case parsed `.json`, raw transcripts, `scores.csv`, `report.md` |

## Run it

```bash
# dry run: a 10-question subset
python3 run_ab.py --run-id dry1 --tier simple --kernel-root /path/to/linux

# full run (sequential, ~2-4 h, both arms; Sonnet)
python3 run_ab.py --run-id full --kernel-root /path/to/linux

# grade (free to rerun; never rerun the agent to fix a grader bug)
python3 grade.py --run-id full
```

Requirements: `claude` CLI logged in (auto-detected from PATH or the VS Code extension
bundle; override with `--claude-bin`), a kernel checkout, and for arm A a working
`mcp-gtags-server` install (the config launches it via `uv --directory <this repo>`).

## Methodology notes

- **Isolation:** both arms run with `--strict-mcp-config`, so no user/project MCP servers
  leak in; arm B is guaranteed MCP-free (verify: `grep -r mcp__gtags results/<run>/none/`
  must be empty).
- **Grading is deterministic** — substring/regex/yes-no checks against an enforced final
  `ANSWER:` line. No LLM judge. A wrong ground truth would bias both arms equally.
- **Truth provenance:** each question's `truth_source` is `golden:<id>` (CI-verified set),
  `server-tool` (validated server, spot-checked), or `manual-grep` (verified by hand).
- **Headline number:** the paired flip counts (gtags-pass/none-fail vs. the reverse), not
  the raw pass rates — at n=50 the pairing is what makes the comparison honest.
- Arms are interleaved per question so time-of-day/model drift affects both equally.
- Timeout 300 s/run counts as a failure for that arm (flagged `timed_out`).
