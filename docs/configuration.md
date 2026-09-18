# Configuration

Nothing needs configuring: every setting has a working default. When you do want
to change one, the precedence is

```text
tool-call argument > CLI flag > environment variable > project config > user config > default
```

## Project root resolution

Each tool call resolves its repository down this ladder:

1. the `project_root` argument on the call — agents use this to target any tree;
2. `--root` / `GTAGS_MCP_ROOT`;
3. `root` in a config file *(pinning this defeats multi-repo use)*;
4. the client's workspace roots (MCP roots protocol) — IDEs that advertise their
   open folders get the right repository automatically; with several open, the
   agent is asked to pass `project_root`;
5. walking up from the server's working directory to the nearest `.git`/`GTAGS` —
   this is why a stdio server spawned inside a repository just works.

!!! note "Protocol 2026-07-28 deprecated roots"
    Clients on the newest protocol revision (SEP-2577) can't answer `roots/list`,
    so step 4 is skipped for them. Stdio servers still resolve through step 5; on a
    shared HTTP server, agents pass `project_root`.

Step 5 can be switched off with `--no-cwd-fallback` (or `GTAGS_MCP_CWD_FALLBACK=0`,
or `cwd_fallback = false` in the **user** config). Tools then return an error
asking for `project_root` instead of indexing whatever directory the server was
launched from. Plugin installs set this, because plugin clients run the server from
the plugin's own folder.

## Config files

Both are optional TOML:

- **Project** — `.gtags-mcp.toml` at the repository root, checked in like
  `.editorconfig` so a team shares it.
- **User** — `${XDG_CONFIG_HOME:-~/.config}/gtags-mcp/config.toml`, for per-machine
  defaults.

```toml
# .gtags-mcp.toml
skip_globs = ["*.gen.c", "third_party/*"]   # never index these
respect_gitignore = true                    # use `git ls-files`
label = "native-pygments"                   # force a GTAGSLABEL parser
bin_dir = "~/.gtags-mcp/bin"                # extra binary directory
enrich = true                               # ctags kind/signature/scope
guards = true                               # #ifdef guard stacks
macro_resolve = true                        # sys_*, DEFINE_*, TRACE_EVENT …
cwd_fallback = true                         # user config only, see above
# root = "/abs/path"                        # pins one project (user config)
```

Unknown keys are ignored.

## CLI flags

| Flag | Effect |
|---|---|
| `--transport {stdio,http}` | stdio (default) or a shared streamable-HTTP server |
| `--host`, `--port` | HTTP bind address and port (default `127.0.0.1:8383`) |
| `--root PATH` | default project root for every tool |
| `--label LABEL` | force a `GTAGSLABEL` parser, e.g. `native-pygments` |
| `--bin-dir DIR` | search this directory first for `gtags`/`global`/`ctags` |
| `--no-enrich` | drop ctags metadata **and** `EXPORT_SYMBOL` recovery |
| `--no-guards` | drop `#ifdef` guard stacks; `active_config` then errors |
| `--no-macro-resolve` | don't resolve macro-generated symbols |
| `--no-cwd-fallback` | never treat a non-repository cwd as the project root |
| `--no-auto-setup` | don't install the toolchain automatically |

Subcommands: `serve` (default), `setup`, `doctor`, `config`, `eval`, `help`.

## Environment variables

| Variable | Meaning |
|---|---|
| `GTAGS_MCP_ROOT` | default project root |
| `GTAGS_MCP_HOME` | managed toolchain home (default `~/.gtags-mcp`) |
| `GTAGS_MCP_BIN_DIR` | extra binary directory |
| `GTAGS_MCP_LABEL` | force a parser label |
| `GTAGS_MCP_TRANSPORT`, `GTAGS_MCP_HOST`, `GTAGS_MCP_PORT` | transport defaults |
| `GTAGS_MCP_AUTO_SETUP=0` | disable the automatic toolchain install |
| `GTAGS_MCP_ENRICH=0`, `GTAGS_MCP_GUARDS=0`, `GTAGS_MCP_MACRO_RESOLVE=0` | disable a feature |
| `GTAGS_MCP_CWD_FALLBACK=0` | require an explicit `project_root` |

## Where the index lives

`.gtags-mcp/` at the project root, containing the GNU Global database and a
`.gitignore` of its own, so git never sees it and your `.gitignore` stays
untouched. A pre-existing root-level `GTAGS` (from your own `gtags` run) is
respected in place instead.

Delete either freely — the next query rebuilds it.

## Index freshness

- The **first query** on a repository builds the index. Live MCP calls wait up to
  20 seconds and then return "indexing … retry shortly" while the build continues,
  so a client's tool timeout is never blown.
- **Later queries** answer immediately and kick a background `gtags -i` refresh,
  debounced adaptively (at least 5 s, and at least 10× the measured cost of the
  last update).
- [`update_index`](tools.md#update_index) is the synchronous barrier after edits;
  `full=true` rebuilds from scratch.
- Full rebuilds are single-flight per repository: parallel first queries join one
  build instead of starting several over the same database.

## Transports

**stdio** (default) — the client launches the server; one process per client.

**HTTP** — one shared server for every client on the machine:

```bash
mcp-gtags-server serve --transport http --host 127.0.0.1 --port 8383
```

The endpoint is unauthenticated. It binds localhost by default, and the SDK turns
on DNS-rebinding protection for localhost binds. Only bind `0.0.0.0` on networks
you trust.
