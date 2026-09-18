# Troubleshooting

Start with the built-in report — it prints the resolved binaries, parser label,
guard scanning, auto-setup state and index location:

```bash
uvx mcp-gtags-server doctor
```

## Quick index

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: No module named 'mcp.server.fastmcp'` | An old release (≤ 1.4.2) with an uncapped SDK dependency | Upgrade: `uvx --refresh mcp-gtags-server` |
| `Indexing … Retry this call shortly` | First query on a repository; the index is still building | Wait a few seconds and retry; nothing is wrong |
| `Error: no project root could be determined` | The cwd fallback is off and no root was given | Pass `project_root="/abs/path"` |
| `… too broad for caller analysis` | The symbol is referenced in 500+ files | Use `find_references` (it groups by file), then `path_prefix` |
| `GLIBC_2.xx not found` | Prebuilt binaries newer than this host's glibc | `mcp-gtags-server setup --force` rebuilds from source |
| `GNU Global (gtags/global) was not found` | Auto-setup disabled or install failed | `mcp-gtags-server setup`, or install `global` from your package manager |
| Results miss a symbol you just wrote | The background refresh hasn't landed | Call `update_index`, then query again |
| MCP server "failed to connect" in Codex | Startup timed out while downloading the package | Set `startup_timeout_sec = 30` in `~/.codex/config.toml` |

## First query takes a long time

Indexing a kernel-sized tree takes roughly a minute; a normal repository is
seconds. A live MCP call waits up to 20 seconds, then answers:

```text
Indexing /home/ai/linux for the first time (20s elapsed; a Linux-kernel-sized tree
takes about a minute). Retry this call shortly.
```

The build continues in the background, so the next call usually succeeds. This
exists because clients enforce their own tool timeouts — Codex's default is 60
seconds.

## "No project root could be determined"

The server could not identify a repository: no `project_root` argument, no
`--root`/`GTAGS_MCP_ROOT`, no client workspace root, and the working directory is
not inside a git repository or indexed tree — with the cwd fallback disabled (the
plugin disables it deliberately).

Pass an absolute path:

```json
{ "symbol": "vfs_read", "project_root": "/home/me/linux" }
```

## Empty or surprising results

- **A miss** returns prefix suggestions (`similar symbols: …`) — check the spelling
  first.
- **A macro-generated name** should resolve via `resolved_via`; if it doesn't, make
  sure `--no-macro-resolve` isn't set.
- **`kind`, `signature` and guards are all `null`** — universal-ctags with `+json`
  isn't installed, or enrichment is off. Run `mcp-gtags-server setup`.
- **A definition is missing entirely** — GNU Global's parser can derail on sparse
  annotations. Exported symbols are recovered automatically from their
  `EXPORT_SYMBOL*` site; non-exported static helpers below a derail point stay
  invisible.
- **Files aren't indexed at all** — indexing respects `.gitignore` and
  `skip_globs`; check `doctor` and your [configuration](configuration.md).

## Corrupted index

An interrupted build can leave a damaged database. The server detects it (`global`
reports either "seems corrupted" or, for some query modes, "not found" while the
file exists), wipes it, rebuilds and retries once, then adds a warning to the
result:

```text
Warning: the index database was corrupted and has been rebuilt automatically.
```

To force a clean rebuild yourself: `update_index` with `full=true`, or delete
`<project>/.gtags-mcp/` and run any query.

## Two servers, duplicated tools

If the agent sees every tool twice (`mcp__gtags__find_callers` **and**
`mcp__plugin_mcp-gtags-server_gtags__find_callers`), both a manual entry and the
plugin are installed. Remove one:

```bash
claude mcp remove gtags        # keep the plugin
# or uninstall the plugin and keep the manual entry
```

## Toolchain issues

The managed toolchain lives in `~/.gtags-mcp` (override with `GTAGS_MCP_HOME`).

```bash
mcp-gtags-server setup           # install or repair
mcp-gtags-server setup --force   # reinstall, building from source if needed
rm -rf ~/.gtags-mcp              # start over
```

Prebuilt Linux binaries need glibc ≥ 2.28 (RHEL/Rocky 8+, Ubuntu 18.10+, Debian
10+). On older hosts setup compiles GNU Global from source, which needs `make` and
a C compiler. Downloaded binaries are executed once as a probe; if that fails,
setup wipes them and falls back to a source build automatically.

## Still stuck?

Open an issue with the output of `mcp-gtags-server doctor` and the failing tool
call: <https://github.com/harshithsunku/mcp-gtags-server/issues>.
