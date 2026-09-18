# Upgrading to v2

v2.0.0 is a breaking release: tools answer in **compact text** by default, and the
surface is **8 tools** instead of 11. Configuration, environment variables and the
index location are unchanged.

```bash
uvx --refresh mcp-gtags-server --version   # 2.0.0
```

## If you parse tool output

Pass `format="json"` explicitly and everything behaves as before — the envelope is
unchanged apart from **added** keys.

```python
raw = find_definition(symbol="vfs_read", format="json")
```

## Renamed and removed tools

| Gone | Use instead |
|---|---|
| `symbol_info` | `find_definition` — it now returns the usage summary too: `definition_count`, `guard_variants`, `reference_count`, `file_count`, `top_files`, `exported` |
| `summarize_references` | `find_references` — above 200 references it groups per file automatically; `group_by="file"` forces it, `path_prefix` drills into a directory |
| `blast_radius` | The [`/gtags:impact`](plugin.md#gtagsimpact-git_ref) slash command: `git diff`, then `find_callers` on each changed function |

Why: agent transcripts showed `find_definition` chosen 20 times against 2 for
`symbol_info`, and no run ever reached for `blast_radius`. Fewer, better-named
tools improve selection accuracy and cost less context.

## New in v2.0.0

- **Compact text output** — `path:line: source  [kind; #if CONFIG_X]`, with
  pagination footers and next-tool hints. 20% fewer tokens across seven
  representative kernel questions.
- **`find_callers` shows each call site's source line**, so there's nothing left to
  re-grep.
- **`find_references` gains `group_by` and `path_prefix`**; `path_prefix` is pushed
  into GNU Global, making narrow queries about 25× faster.
- **Two slash commands** (`/gtags:impact`, `/gtags:explain`) and a
  [cross-client plugin](plugin.md).
- **Failures set `isError`** so the model can self-correct.
- **`cwd_fallback`** — refuse to index a directory that isn't a repository.
- **MCP SDK 2.x** (`mcp>=2.2,<3`), speaking every protocol revision from
  2024-11-05 through 2026-07-28.

## Fixed in v2.0.0

Parallel first queries on an unindexed repository used to start one `gtags` build
each over the same database, producing `GTAGS not found`, `gtags: chmod(2) failed`
and index corruption. v1.5.0's read-only annotations made clients dispatch those
calls in parallel, so it was reachable in normal use. Full builds are now
single-flight per repository, and a query whose database vanishes mid-rebuild joins
the build and retries once.

## Version history

| Version | Highlights |
|---|---|
| **2.0.0** | Compact text default, 11 → 8 tools, plugin + prompts, single-flight builds |
| 1.5.0 | MCP SDK 2.x port, tool annotations, single-copy responses |
| 1.4.3 | Capped `mcp<2` after SDK 2.0 broke fresh installs |
| 1.4.0 | ctags `EXPORT_SYMBOL` recovery, caller-graph definition cache |
| 1.0.0 | Macro-family resolution, workflow tools, corruption recovery, eval harness |
| 0.9.0 | `#ifdef` guard stacks and `.config` filtering |

Full notes: [GitHub releases](https://github.com/harshithsunku/mcp-gtags-server/releases).
