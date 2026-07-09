# mcp-gtags-server

> **Stop letting your AI agent grep. Give it an index.**

[![PyPI](https://img.shields.io/pypi/v/mcp-gtags-server)](https://pypi.org/project/mcp-gtags-server/)
[![CI](https://github.com/harshithsunku/mcp-gtags-server/actions/workflows/ci.yml/badge.svg)](https://github.com/harshithsunku/mcp-gtags-server/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/protocol-MCP-8A2BE2)](https://modelcontextprotocol.io/)
[![Powered by GNU Global](https://img.shields.io/badge/powered%20by-GNU%20Global-orange)](https://www.gnu.org/software/global/)

```bash
curl -fsSL https://raw.githubusercontent.com/harshithsunku/mcp-gtags-server/main/scripts/install.sh | bash
```

*One command, no sudo, everything in user space — when it finishes, an MCP server is running and the client config is on your screen.*

Every AI coding agent — Claude Code, Cursor, Codex, you name it — answers *"where is this function defined?"* the same way: **grep the entire tree**. On a million-line C/C++ codebase that's a full scan per question, and the output is a firehose: every comment, string literal, and unrelated match, dumped straight into the model's context window.

**mcp-gtags-server** replaces those scans with indexed lookups powered by [GNU Global (gtags)](https://www.gnu.org/software/global/) — the same tags engine kernel and systems developers have trusted for decades — exposed to agents over the [Model Context Protocol](https://modelcontextprotocol.io/). Built for the codebases LSP-based tools can't handle: kernel-scale C/C++, trees that don't currently compile, machines you can't sudo on.

- **~100× faster per query** — milliseconds instead of seconds, at any codebase size
- **Radically less noise** — the definition, not 7,873 lines of matches
- **Zero index management** — first query builds the index, every query auto-refreshes it
- **Works everywhere MCP does** — Claude Code, Claude Desktop, Cursor, any MCP client

## The numbers (real Linux kernel, not a toy)

Measured on a full Linux kernel checkout — **65,163 C/C++ files, 37.1 million lines** — warm page cache:

| Question an agent asks | `grep -rn` | gtags (this server) | Context consumed |
|---|---|---|---|
| Where is `tcp_v4_rcv` defined? | 1.40 s | **0.01 s** | 8 lines → **1 line** |
| Where is `kmalloc` defined? | 1.62 s | **0.01 s** | 7,873 lines → **5 lines** |
| Who references `kmalloc`? | 1.62 s | **0.10 s** | 7,873 noisy lines → 2,744 real sites (or a **ranked per-file summary**) |
| Show me `tcp_v4_rcv`'s implementation | *read a 3,500-line file* | **`get_symbol_body`** | **exactly the 271-line function** |
| Who calls `ext4_mark_inode_dirty`? | 245 raw match lines | **`find_callers`** | **62 deduped caller functions, with counts** |

One-time index build: **66 s** for the whole kernel. Incremental refresh after edits: well under a second. Reproduce it yourself with [`scripts/benchmark.sh`](scripts/benchmark.sh):

```bash
./scripts/benchmark.sh /path/to/linux tcp_v4_rcv kmalloc ext4_readdir
```

The speed is nice. The real win is **precision**: an agent that gets 5 exact lines instead of 7,873 noisy ones keeps its context window for actual reasoning.

## Quick start (60 seconds)

**One command. No sudo. Works everywhere** — restricted corporate machines, containers, build servers:

```bash
curl -fsSL https://raw.githubusercontent.com/harshithsunku/mcp-gtags-server/main/scripts/install.sh | bash
```

Everything lands in your home directory — the server (via `uv`), GNU Global, universal-ctags, and Pygments (in `~/.gtags-mcp`). When it finishes, an MCP server is **already running in the background** and the exact client configuration is printed to your console:

```text
==> All set! Connect your tools with the configuration below:

MCP client configuration (HTTP transport):

  Claude Code (once per device, all repos):
      claude mcp add --scope user --transport http gtags http://127.0.0.1:8383/mcp

  Cursor / any MCP client — global settings or .mcp.json:
      {
        "mcpServers": {
          "gtags": { "url": "http://127.0.0.1:8383/mcp" }
        }
      }
```

**Re-run the same command any time:**

- Up to date? → *"Already installed and up to date — nothing to install"*, and the config is printed again.
- New release on GitHub/PyPI? → the package updates, an outdated gtags toolchain is wiped and reinstalled automatically, and the background server restarts on the new version.

Prefer stdio (client-launched processes) over a background server? That works too — `GTAGS_MCP_NO_SERVER=1` skips the server, and any client can use:

```json
{
  "mcpServers": {
    "gtags": {
      "command": "mcp-gtags-server"
    }
  }
}
```

That's it. No indexing step, no configuration, **no per-repo setup** — 20 repos need zero extra installs. Ask your agent *"who calls `tcp_v4_rcv`?"* — the first query in any repo builds that repo's index automatically, and every query after that is answered in milliseconds. Run `mcp-gtags-server doctor` any time to see what the server detects, or `mcp-gtags-server config` to re-print the client configuration.

<details>
<summary><b>Background server details</b> (network access, port, lifecycle)</summary>

The installer runs `mcp-gtags-server --transport http --host 127.0.0.1 --port 8383` in the background (pid: `~/.gtags-mcp/server.pid`, log: `~/.gtags-mcp/server.log`). Environment overrides for the installer:

| Variable | Default | Meaning |
|---|---|---|
| `GTAGS_MCP_PORT` | `8383` | HTTP port |
| `GTAGS_MCP_HOST` | `127.0.0.1` | Bind address — set `0.0.0.0` to reach the server from other devices at `http://<machine-ip>:8383/mcp` |
| `GTAGS_MCP_NO_SERVER` | unset | `1` = don't start a background server |

**Security note:** the HTTP endpoint is unauthenticated. It binds localhost by default; only bind `0.0.0.0` on networks you trust.

</details>

<details>
<summary><b>Manual install</b> (prefer system packages, or already have Global)</summary>

```bash
# 1. GNU Global — EITHER user-space (no sudo):
mcp-gtags-server setup
#    OR a system package:
sudo apt install global      # Debian/Ubuntu
sudo dnf install global      # Fedora
brew install global          # macOS

# 2. The server:
uv tool install mcp-gtags-server        # or: pip install mcp-gtags-server
```

The server finds binaries in this order: `--bin-dir`/`GTAGS_MCP_BIN_DIR`/config `bin_dir` → `~/.gtags-mcp/bin` → `PATH` → `~/.local/bin`.

</details>

<details>
<summary><b>Claude Desktop</b> config</summary>

Add to `claude_desktop_config.json` (pin the project since Desktop doesn't launch in your repo):

```json
{
  "mcpServers": {
    "gtags": {
      "command": "mcp-gtags-server",
      "args": ["--root", "/absolute/path/to/your/project"]
    }
  }
}
```

</details>

<details>
<summary><b>Pin a project root</b> explicitly</summary>

By default the server auto-detects the project root by walking up from its working directory to the nearest `.git` or existing `GTAGS` — so queries from anywhere inside a monorepo resolve to the repo root. Override with `--root /path`, the `GTAGS_MCP_ROOT` env var, or `root` in a config file — or pass `project_root` on any individual tool call to query a different tree.

</details>

<details>
<summary><b>Config files</b> — per-project and per-user defaults</summary>

Every setting can also live in a TOML file, so teams share defaults through the repo (like `.editorconfig`):

- **Project**: `.gtags-mcp.toml` at the project root
- **User**: `~/.config/gtags-mcp/config.toml`

```toml
# .gtags-mcp.toml
label = "native-pygments"     # force a GTAGSLABEL parser label
bin_dir = "/opt/tools/bin"    # extra directory searched for gtags/global/ctags
skip_globs = ["*.gen.c"]      # never index paths/basenames matching these globs
respect_gitignore = true      # default: index only what `git ls-files` reports
enrich = true                 # default: ctags kind/signature/scope on results
# root = "/abs/path"          # default project root (user config)
```

Precedence: tool-call argument > CLI flag > environment variable > project config > user config > built-in default.

</details>

## The tools

### Symbol-level tools — the noise killers

**Give the agent the symbol, not the file.**

| Tool | What the agent gets |
|---|---|
| `symbol_info` | **A one-shot overview card** — definitions (with kind, signature, and scope), reference count, hottest files, and which tool to use next. The best first query for any unfamiliar symbol. |
| `get_symbol_body` | **Just the source of a definition.** The 271-line `tcp_v4_rcv` function — not the 3,500-line file it lives in. Handles functions, structs, and multi-line macros. |
| `find_callers` | **The call graph, deduplicated.** Every reference mapped to its enclosing function with call counts: 245 raw lines for `ext4_mark_inode_dirty` collapse to 62 callers. |
| `call_hierarchy` | **Multi-level impact analysis.** Who calls X, who calls *those*, up to 5 levels — a cycle-safe, capped tree instead of N rounds of grep. |
| `find_callees` | **The outgoing call graph.** What does this function call? Body-extracted call sites, each verified against the index, split into in-tree (with locations) and external. |
| `summarize_references` | **A ranked per-file count.** The cheap first move for hot symbols — `kmalloc`'s 2,744 references become one screen of "where usage concentrates". |
| `project_overview` | **Orientation in an unfamiliar repo** — file counts by top-level directory and language, straight from the index. |
| `find_dead_symbols` | **Dead-code candidates** — every symbol a file defines that nothing references. |
| `find_includers` | **Header blast radius** — every file that `#include`s a header, matched by basename. |

A two-level `call_hierarchy` on the kernel's `ext4_mark_inode_dirty` — 87 compact lines instead of dozens of grep rounds:

```text
ext4_mark_inode_dirty  (definition: fs/ext4/ext4_jbd2.h:138)
├─ ext4_rename  fs/ext4/namei.c  (6 sites)
│  └─ ext4_rename2  fs/ext4/namei.c  (1 site)
├─ swap_inode_boot_loader  fs/ext4/ioctl.c  (5 sites)
│  └─ __ext4_ioctl  fs/ext4/ioctl.c  (1 site)
├─ ext4_mkdir  fs/ext4/namei.c  (3 sites)
│  └─ ext4_rename2  fs/ext4/namei.c  (1 site)
...
```

### Core lookups

| Tool | What it does | Underlying command |
|---|---|---|
| `find_definition` | Where is this symbol defined? | `global -x` |
| `find_references` | Raw reference lines for a symbol | `global -rx` |
| `find_symbol_usages` | Usages of symbols with no in-tree definition (libc calls etc.) | `global -sx` |
| `grep_project` | Regex search across indexed files | `global -gx` |
| `list_file_symbols` | A file's API surface — every symbol it defines | `global -fx` |
| `complete_symbol` | Symbols starting with a prefix | `global -c` |
| `find_files` | Indexed files whose path matches a regex | `global -P` |
| `index_project` / `update_index` | Force rebuild / refresh (rarely needed — it's automatic) | `gtags` / `global -u` |

Every query tool supports `limit`/`offset` pagination, long-line truncation, and (where it makes sense) `case_insensitive` — output is *engineered* to never flood a context window.

### Structured output (JSON by default)

Since v0.8.0 every tool returns a **machine-readable JSON envelope by default** (pass `format="text"` for the previous human-readable rendering — a breaking change if you parsed the old text):

```json
{
  "tool": "find_definition",
  "root": "/abs/project/root",
  "results": [
    {"symbol": "tcp_v4_rcv", "path": "net/ipv4/tcp_ipv4.c", "line": 2001,
     "col": 5, "kind": "function", "typeref": "int", "scope": null,
     "signature": "(struct sk_buff * skb)", "guard": null,
     "snippet": "int tcp_v4_rcv(struct sk_buff *skb)"}
  ],
  "total": 1, "offset": 0, "truncated": false,
  "next_tools": ["get_symbol_body", "find_callers", "symbol_info"],
  "warning": null
}
```

- Symbol locations always use the stable record schema `{symbol, path, line, col, kind, typeref, scope, signature, guard, snippet}` with repo-relative paths. Keys are only ever added, never renamed or removed — parsers never need to change shape. `guard` (`#ifdef` stack) is reserved for an upcoming milestone and currently `null`.
- **`kind` / `typeref` / `scope` / `signature` say *what* a symbol is** (since v0.8.1): function vs. macro vs. struct vs. typedef vs. enum constant, its return/target type, its enclosing scope (`enum:color`, `struct:item`), and its parameter list — extracted per file by universal-ctags with **no build and no compile database**, cached, and filled on definition-shaped results (`find_definition`, `symbol_info`, `list_file_symbols`). When universal-ctags isn't available the fields are simply `null`; disable explicitly with `--no-enrich`, `GTAGS_MCP_ENRICH=0`, or `enrich = false` in `.gtags-mcp.toml`.
- `next_tools` tells the agent the highest-value follow-up call for what was (or wasn't) found.
- `total`/`offset`/`truncated` replace the text continuation footer; errors keep the envelope with an `error` field.
- Composite tools return tool-shaped `results` (e.g. `call_hierarchy` a nested caller tree, `find_callees` `{in_tree, external}`, `symbol_info` an overview object) inside the same envelope.

### What gets indexed (junk stays out)

Indexing feeds `gtags` an explicit file list instead of letting it walk the tree:

- In a git repository the list comes from `git ls-files` — **`.gitignore` is respected exactly**, so build output, vendored blobs, and generated files never pollute the index. Disable with `respect_gitignore = false` in `.gtags-mcp.toml`.
- Outside git, the tree is walked minus well-known junk directories (`.git`, `node_modules`, `build`, `dist`, `.venv`, ...).
- `skip_globs` in `.gtags-mcp.toml` drops anything else you never want indexed.

Incremental refreshes recollect the list, so newly ignored files drop out of the index and new files appear — automatically.

### The flow that saves your context window

```text
0. project_overview()                       → orient in an unfamiliar repo (12 lines)
1. symbol_info("kmalloc")                   → definitions + usage spread + next step (12 lines)
2. call_hierarchy("ext4_mark_inode_dirty")  → multi-level impact tree (1 line/caller)
3. get_symbol_body("tcp_v4_rcv")            → read the ONE function that matters
4. find_callees("tcp_v4_rcv")               → what it depends on, with locations
```

A few hundred lines of context total — versus tens of thousands for the grep-and-read-files equivalent.

## Multi-language projects (C + Python + more)

Real projects mix languages — a C core with Python tooling, JS frontends, Go services. The server handles this automatically:

- **Native languages** (C, C++, Java, PHP, Yacc, assembly) use GNU Global's fast built-in parser.
- **Everything else** (Python, Go, Rust, JavaScript, TypeScript, Ruby, ... ~150 languages) is indexed through Global's **ctags + Pygments plugin parsers** — same index, same tools, same queries.

The one-line installer (and `mcp-gtags-server setup`) enables this automatically — it installs universal-ctags and Pygments into user space, and the server switches to the `native-pygments` parser label on its own. Prefer system packages? Those work too:

```bash
sudo apt install exuberant-ctags python3-pygments   # Debian/Ubuntu
sudo dnf install ctags python3-pygments             # Fedora
brew install ctags && pip install pygments          # macOS
```

Now `find_definition("py_util")`, `get_symbol_body` (indentation-aware for Python), `find_callees`, `call_hierarchy` — all work across every language in the tree, in one index.

Force a specific parser label with `--label`, `GTAGS_MCP_LABEL`, or `label` in `.gtags-mcp.toml` (e.g. `default` for native-only, `pygments` for plugin-everything).

**Honest caveats:** for plugin-parsed languages, *definitions* are as accurate as ctags, but *references* are token-based — every occurrence of the name counts, without C-grade semantic reference tracking or local-scope awareness. For C/C++ nothing changes: the native parser still does that part.

## How it works

```text
agent question ──► MCP tool ──► GTAGS index (built once, ~66s for the kernel;
                                    │        .gitignore-aware file list)
                 background auto-refresh (gtags -i, adaptive debounce)
                                    │
                       narrow JSON answer ──► agent context
```

- **First query on a tree?** The index is built automatically (the only operation that ever blocks — and only once).
- **Files changed?** A debounced incremental refresh runs **in the background**: queries always answer instantly from the current index while `gtags -i` catches up behind the scenes. Measured on the kernel: queries return in 0.02s while the 25s freshness check runs invisibly. Staleness is bounded by the debounce window; call `update_index` for a synchronous, guaranteed-fresh barrier right after edits.
- **Huge result?** Pagination footers tell the agent exactly how to fetch the next page — or the tool itself suggests a narrower one (`find_callers` on a symbol used in 500+ files points to `summarize_references`).

## FAQ

**Why gtags instead of a language server (LSP)?**
LSP servers give richer semantics but need a working build configuration, per-editor setup, and serious warm-up time on large trees. gtags indexes 37M lines in about a minute with *zero* configuration, handles the kernel-scale codebases LSPs choke on, and its fuzzy parser doesn't care whether the code currently compiles. For C/C++ navigation questions — definition, references, callers — it's the pragmatic sweet spot.

**What languages?**
C, C++, Yacc, Java, PHP, and assembly natively — plus Python, Go, Rust, JS/TS, Ruby, and ~150 others via the ctags/Pygments plugin parsers (see [Multi-language projects](#multi-language-projects-c--python--more)).

**Does the agent have to manage the index?**
No. That's the point. Build-on-first-query, background refresh with adaptive debounce, zero blocking — queries never wait for index maintenance. The explicit `index_project`/`update_index` tools exist only as escape hatches (`update_index` doubles as a synchronous freshness barrier after edits).

**Will it fight my agent's built-in tools?**
The tool descriptions are written to steer the model: they say *when* to use indexed lookups instead of grep. In practice agents pick the faster, narrower tool naturally.

## Development

```bash
git clone https://github.com/harshithsunku/mcp-gtags-server
cd mcp-gtags-server
uv run --extra dev pytest       # 61 tests; e2e tests auto-skip if GNU Global is absent
npx @modelcontextprotocol/inspector mcp-gtags-server    # poke at it interactively
```

Tests build a real C project in a temp dir and exercise auto-indexing, auto-refresh, caller mapping, body extraction, pagination, user-space binary discovery, and config layering end-to-end.

Release flow: bump `version` in `pyproject.toml`, tag `vX.Y.Z`, push — CI publishes to PyPI and users pick the update up on their next installer re-run. Prebuilt GNU Global binaries are rebuilt by tagging `global-v<version>`.

## Roadmap

See [ROADMAP.md](ROADMAP.md) — structured JSON output landed in v0.8.0 and ctags
metadata enrichment (kind/signature/scope on every definition) in v0.8.1; next up are
`#ifdef`/config-guard awareness (the headline capability for kernel and firmware
trees), macro-family symbol resolution, and a correctness eval harness.

Contributions welcome — open an issue or PR.

## License

[MIT](LICENSE) © Harshith Sunku
