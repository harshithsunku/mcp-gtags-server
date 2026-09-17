# CLAUDE.md

Context for AI assistants working on this repository.

## What this project is

`mcp-gtags-server` — an MCP (Model Context Protocol) server that exposes GNU Global
(gtags) code navigation for C/C++ codebases, tuned for Linux-kernel-scale trees
(37M+ lines). Published to PyPI, the MCP Registry (`io.github.harshithsunku/mcp-gtags-server`),
and as a Claude Desktop `.mcpb` bundle. Python ≥ 3.10, packaged with `uv`.

The pitch: indexed symbol lookups instead of grep, with kernel-specific smarts that
plain gtags lacks — macro-generated symbol resolution (`SYSCALL_DEFINE3`,
`DEFINE_SPINLOCK`, …), recovery of definitions the Global parser missed (via their
`EXPORT_SYMBOL*` site + ctags), `#ifdef` guard stacks on every result, and
kernel-.config-aware filtering (`active_config`).

## Layout

```
src/gtags_mcp/
  server.py      (~2000 lines) MCP tools + prompts, root detection, index lifecycle, CLI main()
  toolchain.py   toolchain install: managed GNU Global/ctags/Pygments in ~/.gtags-mcp
  guards.py      #if/#ifdef guard-stack scanner + active_config evaluation
  macros.py      macro-generated symbol resolution (SYSCALL_DEFINE etc.), EXPORT_SYMBOL detection
  enrich.py      ctags metadata enrichment (kind/typeref/scope/signature); probe_binary + cache
  config.py      settings: CLI flag > env var > project .gtags-mcp.toml > user config > default
                 user config: ${XDG_CONFIG_HOME:-~/.config}/gtags-mcp/config.toml
  output.py      JSON envelope {tool, root, results, total, truncated, hints, ...} AND
                 render_text(): the compact text agents get by default
  fileset.py     file listing helpers
  evalharness.py golden-set eval runner (see Evals below)
tests/           pytest suite (325 tests, ~7s, no network; heavy use of tmp fixture projects)
  test_contract.py        client-facing budgets (2KB descriptions/instructions, 512-char
                          Codex head), tool metadata, prompts, plugin manifests, version sync
plugin/          cross-client plugin: Agent Plugins 1.0.0 (plugin.json + mcp.json, for
                 Codex/Cursor) AND Claude Code (.claude-plugin/plugin.json + .mcp.json),
                 sharing skills/c-code-navigation/SKILL.md. Marketplaces at the repo root:
                 .claude-plugin/, .agents/plugins/, .cursor-plugin/
evals/golden.jsonl        64-case golden set run against a real kernel tree
evals/agent/              agent A/B eval harness (run_ab.py, grade.py, questions.jsonl)
scripts/stability_exercise.py  operational latency/size matrix against a real tree
docs/capability.md        capability matrix; docs/bringup-hurdles.md
mcpb/manifest.json        Claude Desktop bundle manifest
.github/workflows/        ci.yml, eval.yml, publish.yml, publish-registry.yml, release-binaries.yml
```

## The 8 MCP tools (all in server.py, decorated `@_gtags_tool("Title")`)

`find_definition` (definitions + usage summary; absorbed symbol_info),
`find_references` (auto-groups by file above 200 refs; absorbed
summarize_references), `get_symbol_body`, `find_callers`, `find_callees`,
`reachability`, `list_file_symbols`, `update_index`.

Plus two MCP **prompts** (slash commands, no schema cost): `impact` (the former
blast_radius workflow: git diff → find_callers) and `explain`.

Conventions shared by all tools:
- Docstrings ARE the MCP tool descriptions — agent-facing prompt text whose
  first sentence carries the words tool search matches ("who calls", "usages",
  "go to definition"). Schema context cost matters; the surface was
  deliberately consolidated 20 → 12 → 11 → 8 (v1.2.0/v1.3.0/v2.0.0). Don't add
  tools or bloat docstrings casually; test_contract.py pins the budgets.
- **Tool bodies return the JSON envelope only.** `_gtags_tool` wraps each body
  in a sync function with the `format` parameter (default "text") that renders
  through `output.render_text()`, so text and JSON can never drift. Never add a
  second text code path (v1.x had one, and it silently skipped guards and
  active_config).
- Every tool takes `project_root`, `format` ("text" default | "json"), and most take
  `limit` (default 100) / `offset`.
- Results are best-effort-never-fail: enrichment/guards/macro-resolution degrade to
  null fields rather than erroring. Opt-out knobs exist for each
  (`--no-enrich/--no-guards/--no-macro-resolve`, env vars, config keys).
- Index freshness: queries auto-refresh in the background (may lag a few seconds);
  `update_index` is the synchronous barrier. Index lives in `<root>/.gtags-mcp/`
  unless legacy root-level GTAGS files exist (`_db_dir` respects those).
- **Full builds are single-flight per root** (`_build_index`/`_builds`). Parallel
  first queries used to start one `gtags` each over the same database →
  "GTAGS not found", "chmod(2) failed", corruption (reproduced on v1.5.0; clients
  dispatch read-only tools concurrently). Live MCP calls wait
  `INDEX_BUILD_WAIT_SECONDS` then return "indexing … retry shortly" (Codex's tool
  timeout is 60 s); direct calls block. A query whose database vanishes mid-rebuild
  joins the build and retries once (`_index_vanished`).
- Errors: `output.error()` envelopes are returned as `CallToolResult(is_error=True)`
  by the async wrapper. Do NOT raise ToolError — the SDK rewrites the message and
  logs a failure line.
- Tool metadata: every tool has a `title` and `ToolAnnotations` (`readOnlyHint`
  true except `update_index`, `openWorldHint` false), and `structured_output=False`
  (without it the SDK derives a `{"result": str}` outputSchema and sends each
  response twice). find_definition/find_callers/get_symbol_body also carry
  `_meta["anthropic/alwaysLoad"]`, which skips Claude Code's deferred tool search
  (the A/B transcripts showed a ToolSearch round-trip before 40 of 50 first calls).

## MCP SDK (2.x, `mcp>=2.2,<3`)

- `MCPServer` (FastMCP is gone in 2.x). There is no `get_context()`:
  `_gtags_tool` grafts a keyword-only `ctx: Context` onto the async wrapper's
  `__signature__` and `__annotations__`, since `functools.wraps` points both at
  the sync body. The SDK injects it and keeps it out of the input schema. The
  module attribute stays the plain sync fn, which is what tests and eval call.
- Roots: 2.x builds a new `ServerSession` per request, so the roots cache is
  keyed by `session._connection` (private; no public accessor). Protocol
  2026-07-28 deprecated roots (SEP-2577) and has no back-channel, so
  `can_send_request` gates the fetch and those clients fall through to cwd.
  The module-level warnings filter silences `MCPDeprecationWarning` (pytest
  needs its own filter in pyproject).
- Tests use `mcp.Client(server.mcp, mode="legacy" | "2026-07-28")` in-process.
  Legacy mode is required for any roots test.
- HTTP: host/port go to `mcp.run(transport="streamable-http", host=, port=)`,
  not to settings.

## CLI

Entry points: `mcp-gtags-server` (alias `gtags-mcp`) → `gtags_mcp.server:main`.
Subcommands: `serve` (default; stdio or `--transport http`), `setup` (user-space
toolchain install, no sudo), `doctor`, `config` (prints client config), `eval`.

Env vars: `GTAGS_MCP_ROOT`, `GTAGS_MCP_HOME` (managed home, default `~/.gtags-mcp`),
`GTAGS_MCP_AUTO_SETUP`, `GTAGS_MCP_TRANSPORT/HOST/PORT`, `GTAGS_MCP_LABEL`,
`GTAGS_MCP_BIN_DIR`, `GTAGS_MCP_ENRICH/GUARDS/MACRO_RESOLVE`.

## Toolchain management (toolchain.py)

- Pins GNU Global `GLOBAL_VERSION` (currently 6.6.15). Install order: prebuilt
  binaries from this repo's `global-v<version>` GitHub release → source build
  fallback. Prebuilts are relocated by patching an embedded 200-char placeholder
  prefix (`PLACEHOLDER_PREFIX`) — binary patching, lengths must match.
- **glibc baseline (v1.4.2, load-bearing):** Linux prebuilts are built in
  manylinux_2_28 containers (RHEL 8 era). `PREBUILT_MIN_GLIBC = (2, 28)`;
  release-binaries.yml asserts no shipped ELF needs newer glibc symbols. Setup
  probes every downloaded binary by executing it (`_verify_managed_global`,
  ctags `probe_binary`) and wipes + falls back to source on failure. GLIBC loader
  errors at query time get a "run setup --force" remediation hint (`_loader_hint`).
- ctags: enrichment and export recovery REQUIRE Universal Ctags with +json.
  `_CTAGS_NAMES` order (v1.4.1): `universal-ctags, ctags-universal, ctags,
  ctags-exuberant` — universal flavours first, or dual-flavour machines silently
  lose enrichment. Exuberant is still last because Debian's stock gtags.conf
  pygments label hardcodes `ctags-exuberant` as plugin-parser backend.
- Multi-language (non-C) indexing = ctags + Pygments via GTAGSLABEL
  `native-pygments`, auto-selected when available.

## Commands

```bash
uv run pytest -q                                   # full suite, ~4s, must stay green
uv run mcp-gtags-server eval \
  --golden evals/golden.jsonl --root /home/ai/linux # kernel golden set: expect 65/65
python scripts/stability_exercise.py \
  --root /home/ai/linux                             # 26-call latency/size matrix, zero anomalies
uv run mcp-gtags-server doctor                      # what the server detects here
```

## Local machine specifics (this dev box)

- Toolchain installed at `~/.gtags-mcp`; kernel test clone at `/home/ai/linux`
  (~1.1G shallow master snapshot mid-2026, depth 1 — `HEAD~1` does NOT exist there;
  index at root level, rebuilds in ~33s). Reuse it, never re-clone.
- 8GB RAM; `/tmp` is tmpfs — never put large clones or indexes there. No `bc`,
  no docker/podman (RHEL 8 smoke tests impossible locally — verify prebuilt assets
  with `objdump -T | grep GLIBC_` on downloaded tarballs instead).
- Kernel-scale verification against `/home/ai/linux` is the REQUIRED final gate
  before any release push.

## Golden set / eval rules (hard-won, do not relearn)

- `evals/golden.jsonl` expectations must hold on EVERY kernel version
  simultaneously: local runs use the mid-2026 master snapshot, CI (eval.yml) pins
  kernel v6.16. Keep expectations **path-level, never line-level, never
  mechanism-specific** (e.g. don't assert `resolved_via: "ctags:EXPORT_SYMBOL"` —
  v6.16's parser doesn't derail on mutex_lock, so recovery never fires there).
  Assert mechanisms deterministically in pytest fixtures instead
  (see `export_gap_project` in tests/test_server.py). A local eval pass does NOT
  prove the CI eval passes.
- CI installs BOTH ctags flavours (`universal-ctags exuberant-ctags`) — see the
  ctags section above for why both are needed.
- Headline numbers as of v2.0.0: eval 64/64 recall, 14/14 precision@1;
  warm latencies: reachability ~46ms, find_callers ~2-3ms, recovered
  mutex_lock lookup ~0.22s.

## Release flow (authorized to run without per-release confirmation)

1. Verify: full pytest + kernel eval (65/65) against `/home/ai/linux`.
2. Bump version in **FIVE files** (missing one has bitten before):
   `pyproject.toml`, `src/gtags_mcp/__init__.py`, `server.json` (two spots),
   `plugin/plugin.json`, `plugin/.claude-plugin/plugin.json`.
   `test_version_is_in_sync_across_every_manifest` catches a miss.
3. Update README/ROADMAP in the same commit when behavior changed.
4. Commit (detailed message), tag `vX.Y.Z`, push commits AND tags.
5. Tag push triggers publish.yml: tests → PyPI (trusted publishing) → GitHub
   release → dependent jobs `publish-registry` + `build-mcpb`. Watch with
   `gh run watch` **per job** — a run can show partial success.

Gotchas:
- `uv.lock` hides dependency drift: `uvx`/pip users resolve fresh from pyproject.
  mcp 2.0 (2026-07-28, removed `mcp.server.fastmcp`) broke every fresh install
  of ≤1.4.2 while CI stayed green. Keep upper bounds on SDK majors; the
  `fresh-deps` CI job (highest + lowest-direct, weekly cron) is the guard.
- MCP Registry rejects server.json descriptions > 100 chars (422 at publish, not
  schema-validated). Fix on main, then `gh workflow run publish-registry.yml -f version=X.Y.Z`.
- Prebuilt Global binaries live on the `global-v<GLOBAL_VERSION>` release
  (separate from vX.Y.Z tags). To heal bad assets retroactively, replace in place
  via `gh workflow run release-binaries.yml -f version=6.6.15` — all package
  versions fetch checksums.txt fresh from that same tag; a new tag would NOT heal
  old installs. Linux assets must stay ≤ GLIBC_2.28.

## Quality bar

The user expects "world-class": real-codebase (kernel) verification before
shipping, opt-out knobs for every heuristic, best-effort-never-fail semantics,
docs updated in the same commit. Comments in code should state constraints the
code can't show (see the raise-vs-return comment in `install_global_prebuilt`
for the house style).
