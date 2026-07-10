# Roadmap

`mcp-gtags-server` is a Model Context Protocol server that gives AI agents fast,
symbol-level navigation of large C/C++ codebases using GNU GLOBAL (gtags) —
with no build system, no compile database, and no elevated privileges required.

This is a living document. Items are ordered roughly by build sequence:
foundation first, then the differentiators, then reach. Check items off as they land.

---

## Design principles

- **No build required.** Symbol intelligence on trees that don't compile, use custom
  or cross-compilation toolchains, or that you simply can't build locally.
- **Embedded & firmware first.** The primary audience is people working on kernels,
  vendor BSPs, and cross-compiled multi-arch firmware — codebases the rest of the
  tooling ecosystem largely ignores.
- **Agent-first ergonomics.** Bounded, ranked, parseable output. Never dump a whole
  file or a thousand raw matches into a model's context.
- **Deterministic & local.** Incremental index freshness, nothing leaves the
  machine, no external embedding service.

## Non-goals

- Not a replacement for a full IDE/language server — it's the layer that works when
  one isn't available or is impractical to set up.
- Not an embedding/RAG index. Retrieval stays exact and deterministic.

---

## Milestones

### 1. Stable structured output + housekeeping ✅ (v0.8.0)
Foundation for everything below; do this first so later tools inherit it.

- [x] Add `format: "json" | "text"` (default `json`), returning a stable schema:
      `{ symbol, path, line, col, kind, guard, snippet }` with repo-relative paths.
- [x] Add a `next_tools` hint array to each response so the agent knows where to go next.
- [x] Respect `.gitignore` and a configurable `skip_globs`; skip vendored/build dirs
      (indexing feeds `gtags --skip-unreadable -f -` an explicit file list from
      `git ls-files`, or a junk-aware walk outside git).
- [x] Auto-detect project root by walking up to `.git` / an existing `GTAGS`
      (monorepo support).

**Done when:** every tool can emit parseable JSON with repo-relative paths, and indexing
a dirty tree skips junk automatically.

### 2. ctags metadata enrichment ✅ (v0.8.1)
Cheap, high-value — uses the parser already in the stack.

- [x] Surface ctags `kind`, `typeref`, `scope`, and `signature` for C/C++ symbols.
- [x] Enrich `symbol_info` cards: distinguish function / macro / struct / typedef /
      enum-constant, show signature and enclosing scope.

**Done when:** `symbol_info` shows kind + signature + scope for C symbols with no build.

### 3. `#ifdef` / config-guard awareness — headline capability ✅ (v0.9.0)
The differentiator. Firmware and kernel code is conditional-compilation soup, and
nothing else surfaces it. Worth spending real time here.

- [x] For each definition/reference, collect the enclosing
      `#if` / `#ifdef` / `#ifndef` / `#elif` stack and attach it as a `guard` field.
- [x] When a symbol has multiple definitions, report them explicitly as
      "N definitions under different guards" rather than a flat list.
- [x] Add an optional `active_config` filter (a `.config` or a list of defined macros)
      that keeps only definitions whose guard stack is satisfiable.

**Done when:** a multiply-defined symbol returns each definition tagged with its guard
stack, and passing a config narrows the result to the live definition.

### 4. Macro-family symbol resolution ✅
Closes the best-known gtags gap on the kernel without a preprocessor.

- [x] Hardcode name-transform rules for the dominant macro families
      (`SYSCALL_DEFINE*`, `EXPORT_SYMBOL*`, `DEFINE_PER_CPU`, `module_param`,
      `DECLARE_*` / `DEFINE_*`, tracepoints).
- [x] Add a "fuzzy resolve" fallback that tries derived spellings before returning empty.

**Done when:** querying a macro-generated symbol (e.g. a `SYSCALL_DEFINE` entry point)
resolves to its definition site. ✅ `find_definition("sys_read")` returns
`fs/read_write.c SYSCALL_DEFINE3(read, ...)` with `resolved_via: "macro:SYSCALL_DEFINE"`,
ranked ahead of same-named test helpers; `symbol_info` also reports `EXPORT_SYMBOL*`
status in an `exported` field.

### 5. Agent workflow tools
Fewer round-trips, higher-value calls — built on the existing call-graph data.

- [ ] `reachability(from, to)` — does A transitively call B, and by what path?
- [ ] `blast_radius(git_ref)` — take a `git diff`, find changed functions, and return
      everything that references/calls them, ranked by distance. (Refactoring-impact use case.)

**Done when:** both return bounded, ranked results tied to real git state.

### ~~6. Optional LSP escalation~~ — dropped
Rejected 2026-07: it contradicts the project's identity. The design principles say
"no build required" and the non-goals say "not a replacement for a full language
server" — a user who has a working `compile_commands.json` already has clangd, IDE
integrations, and existing clangd MCP bridges. Wrapping clangd here would be the most
complex item on this list for the least differentiated value. If demand ever
materializes it can return as a stretch goal; see "Known limitations".

### 7. Correctness eval harness
Trust through measurable quality — reported on its own terms.

- [ ] Build a golden set (~50 queries) of known symbols on a large codebase with
      expected definition locations and expected callers.
- [ ] Run it in CI; report precision/recall.
- [ ] Publish a short capability writeup: how it navigates a large codebase with no
      build, what it resolves, and its measured accuracy.

**Done when:** an eval command prints a score in CI, and the capability writeup is live.

---

## Known limitations (track honestly)

- Static tagging can't resolve function pointers / ops-struct indirection
  (e.g. `->read()` through a `file_operations`). Documented; a candidate-target
  heuristic is a future stretch goal.
- C++ templates/overloads are weaker than C. Enrichment in step 2 helps.
- Semantically-exact resolution (types, overloads, callback targets) needs a real
  compiler frontend. Deliberately out of scope (see dropped step 6): when a compile
  database exists, clangd and its ecosystem already serve that user.

---

## Distribution & adoption

Publishing (discovery) and reach (users) are two separate jobs.

### Publish
- [ ] **Official MCP Registry** (`registry.modelcontextprotocol.io`) — publish a
      `server.json` under an owned name via the `mcp-publisher` CLI. This is the upstream
      feed; do it first.
- [ ] **Claim crawled directories** — Glama (`glama.ai/mcp`) and PulseMCP auto-index
      open-source servers; claim and verify ownership to control the listing.
- [ ] **Smithery** — `smithery mcp publish <url> -n harshithsunku/mcp-gtags-server`.
- [ ] **mcp.so** — community submission.
- [ ] **awesome-mcp-servers** — open a PR (durable GitHub backlink, real browse traffic).
- [ ] **Agent connector directories** — Cursor / Cline / Claude Code, for one-click install.

### Reach
- [ ] 30-second asciinema/GIF in the README: an agent answering a hard question on a
      huge codebase in a few calls.
- [ ] Capability writeup on the personal blog, then share to Show HN, r/kernel,
      r/embedded, r/C_Programming, Lobsters, and LinkedIn.
- [ ] Get listed in niche "awesome" lists (awesome-c, awesome-embedded, kernel tooling) —
      that's where the actual users are, not the MCP directories.

The technical moat (step 3) is what makes the writeup worth publishing; the writeup is
what turns discovery into users.
