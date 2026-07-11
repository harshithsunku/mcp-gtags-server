# Navigating the Linux kernel with no build: what resolves, and how well

`mcp-gtags-server` gives AI agents symbol-level navigation of C/C++ trees that
**don't compile on your machine** — kernels, vendor BSPs, cross-compiled
firmware. No `compile_commands.json`, no toolchain, no elevated privileges:
index once (~35 s for the whole kernel on a laptop), then every query below
answers in milliseconds from the index.

This page is the measured version of that claim.

## The eval

`mcp-gtags-server eval --golden evals/golden.jsonl --root <kernel-tree>` runs
a 65-case golden set — covering **all 11 tools** — against a real kernel
checkout (CI pins one tag and publishes the score on every push to main — see
[eval.yml](../.github/workflows/eval.yml)). Cases assert expected paths,
callers, definition bodies, callees, per-file counts, guard variants,
suggestions, fallbacks, and reachability outcomes; expectations are
path-level so they hold across kernel versions.

Current scores:

```
local, mid-2026 master snapshot: 65/65 = 100.0% recall, 15/15 = 100.0% precision@1, 14s
CI, kernel v6.16 (pinned):       published on every push
```

One case (`def-mutex_lock`) was a deliberate known-fail from v1.0.0 through
v1.3.1: recent kernels added sparse `__acquires()` annotations to mutex
forward declarations, and GNU Global's C parser derails on them, missing the
real `mutex_lock` definition. v1.4.0 closes it with **ctags export
recovery** (see "What resolves"), and the case now also asserts the recovery
mechanism itself.

## What resolves

**Plain symbols** — functions, structs, macros, typedefs — resolve to their
definition sites with ctags metadata (kind, signature, scope) attached, no
build needed.

**`#ifdef` alternates** (the headline): kernel code defines the same symbol
multiple times and lets the config pick one. Every definition carries its
enclosing `#if`/`#ifdef` guard stack, multiply-defined symbols are reported
as "N definitions under M distinct guards", and passing a `.config` (or a
macro list) drops the definitions your config can't compile:

```
Symbol: kmap
  2 definitions under 2 distinct guards:
  [CONFIG_HIGHMEM]  include/linux/highmem-internal.h:40
  [!CONFIG_HIGHMEM] include/linux/highmem-internal.h:170
```

**Macro-generated symbols** — the best-known gap of every tagging tool on
the kernel. `sys_read`, `trace_sched_switch`, and `css_set_lock` have no
literal definition anywhere; they are minted by token-pasting macros. The
server resolves them from the index alone and flags the mechanism:

| query | resolves to | via |
|---|---|---|
| `sys_read` | `fs/read_write.c` `SYSCALL_DEFINE3(read, ...)` | `macro:SYSCALL_DEFINE` |
| `__x64_sys_openat` | `fs/open.c` `SYSCALL_DEFINE4(openat, ...)` | arch wrapper mapped back |
| `compat_sys_ioctl` | `fs/ioctl.c` `COMPAT_SYSCALL_DEFINE3(ioctl, ...)` | `macro:COMPAT_SYSCALL_DEFINE` |
| `trace_sched_switch` | `include/trace/events/sched.h` `TRACE_EVENT(sched_switch, ...)` | `macro:TRACE_EVENT` |
| `css_set_lock` | `kernel/cgroup/cgroup.c` `DEFINE_SPINLOCK(css_set_lock);` | `macro:DEFINE_SPINLOCK` |
| `runqueues` | `kernel/sched/core.c` `DEFINE_PER_CPU_SHARED_ALIGNED(struct rq, runqueues);` | ranked above its `DECLARE_*` |

Resolution costs 20–75 ms per query on the kernel; a miss costs ~9 ms.
`symbol_info` also reports which `EXPORT_SYMBOL*` variant exports a symbol.

**Parser-missed definitions recover via their export site** (v1.4.0). When
GNU Global's parser derails mid-file (sparse annotations like
`__acquires(lock)` on a forward declaration are the known trigger), real
definitions below that point vanish from the index. But `EXPORT_SYMBOL*(sym)`
always sits in the `.c` file that defines `sym` — so when an export site's
file has no definition record, a (cached) Universal Ctags scan of that file
confirms and restores the definition, ranked first and flagged
`resolved_via: "ctags:EXPORT_SYMBOL"`:

| query | gtags alone | with export recovery |
|---|---|---|
| `mutex_lock` | 7 records, none the real one | `kernel/locking/mutex.c:314` first, kind `function` |

The recovered definition flows through the whole tool surface —
`get_symbol_body` reads its real body, `find_callees` analyzes it — and
costs ~0.25 s once (then cached). Disabled together with enrichment
(`--no-enrich`).

**Call-graph questions** collapse into single calls:

- `reachability(ksys_read, rw_verify_area)` → the shortest chain, with the
  file:line of every call site, in 0.6 s;
  `reachability(do_sys_openat2, security_file_open)` finds a 4-hop chain in
  1.3 s.
- `blast_radius(git_ref)` maps a `git diff` to its enclosing functions and
  ranks callers by distance (a probe edit inside `ksys_read` radiates to its
  `SYSCALL_DEFINE3` sites in 0.1 s).

## Operational profile (measured)

[scripts/stability_exercise.py](../scripts/stability_exercise.py) runs a
28-call matrix over all 11 tools against a real kernel tree and records
latency and response size (`--json` for diffing between runs). Measured on
the 37M-line mid-2026 snapshot, 8 GB machine, warm index — **28/28 sane,
zero anomalies**:

| call | warm latency | response size |
|---|---|---|
| `find_definition` (indexed hit) | 4–45 ms | 0.5–1.7 KB |
| `find_definition` (macro-generated, e.g. `sys_read`) | ~40 ms | 0.8 KB |
| `find_definition` (parser-missed, ctags-recovered: `mutex_lock`) | ~0.22 s | 2.6 KB |
| `find_references` (`kmalloc`, 2 744 refs, one page) | ~52 ms | 23 KB |
| `get_symbol_body` (function / struct / macro) | 4–120 ms | 0.6–7.5 KB |
| `find_callers` (typical, `-fx` cache warm) | 2–3 ms | 0.5–5 KB |
| `find_callers` (>500 files: guard refuses gracefully) | ~54 ms | 0.3 KB |
| `summarize_references` (`kmalloc`, 1 553 files) | ~52 ms | 0.8 KB |
| `find_callees` | 17–45 ms | 0.9–1.9 KB |
| `symbol_info` | 5–48 ms | 1.4 KB |
| `reachability` (3-hop chain found) | ~46 ms warm (~0.35 s cold) | 0.6 KB |
| `list_file_symbols` (80-symbol file) | ~4 ms | 27 KB |
| `blast_radius` (real one-line edit, depth 1) | ~0.1 s | 2.5 KB |
| `update_index` (synchronous incremental, 37M lines) | 6–10 s | 0.3 KB |

The caller-graph tools share a per-file definition cache keyed by index
generation (v1.4.0): a reachability walk that re-visits the same hot files
went from ~0.55 s to ~46 ms warm, and repeated `find_callers` calls in one
agent session cost single-digit milliseconds. Every response stays
comfortably under typical MCP client timeouts, wide queries are bounded by
pagination and breadth guards, and the one deliberately slow call
(`update_index`) is the explicit freshness barrier — background refresh
keeps normal queries off that path.

## Known limitations (measured, not hidden)

- **Function pointers are invisible to static tagging.** `vfs_read` never
  statically reaches `ext4_file_read_iter` — the route is `f_op->read_iter`.
  `reachability` says so explicitly rather than returning a wrong path; the
  golden set asserts this honest "no" (`reach-fnptr-honesty`).
- **Upstream parser gaps are recovered, not fixed.** GNU Global's C parser
  derails on the sparse annotation kernels newer than ~v6.16 put on forward
  declarations
  (`static void __sched __mutex_lock_slowpath(...) __acquires(lock);`) and
  misses the real `mutex_lock` definition. Since v1.4.0, export recovery
  restores such definitions from their `EXPORT_SYMBOL*` site via ctags — but
  a parser-missed definition that is *not* exported (static helpers below
  the derail point) stays invisible until the upstream parser is fixed.
- **Prototypes rank alongside definitions.** `find_definition` can return a
  header prototype before the `.c` definition (both are index "definitions");
  the ctags `kind` field distinguishes them.
- **C++ templates/overloads** are weaker than C — inherent to tagging.

## Reproducing

```bash
pip install mcp-gtags-server && mcp-gtags-server setup   # user-space toolchain
git clone --depth 1 --branch v6.16 https://github.com/torvalds/linux.git
mcp-gtags-server eval --golden evals/golden.jsonl --root ./linux
```

The first query indexes the tree automatically; the eval prints the same
report as CI and exits non-zero below the threshold (default 90%).
