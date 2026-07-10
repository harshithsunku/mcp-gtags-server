# Navigating the Linux kernel with no build: what resolves, and how well

`mcp-gtags-server` gives AI agents symbol-level navigation of C/C++ trees that
**don't compile on your machine** — kernels, vendor BSPs, cross-compiled
firmware. No `compile_commands.json`, no toolchain, no elevated privileges:
index once (~35 s for the whole kernel on a laptop), then every query below
answers in milliseconds from the index.

This page is the measured version of that claim.

## The eval

`mcp-gtags-server eval --golden evals/golden.jsonl --root <kernel-tree>` runs
a 50-case golden set against a real kernel checkout (CI pins one tag and
publishes the score on every push to main — see
[eval.yml](../.github/workflows/eval.yml)). Cases assert expected paths,
callers, counts, guard variants, and reachability outcomes; expectations are
path-level so they hold across kernel versions.

Current scores:

```
CI, kernel v6.16 (pinned):      50/50 = 100.0% recall, 14/14 = 100.0% precision@1, 41s
local, mid-2026 master snapshot: 49/50 =  98.0% recall, 14/14 = 100.0% precision@1, 3.1s
```

The one failure on newer kernels is kept in the set deliberately: recent
kernels added sparse `__acquires()` annotations to mutex forward
declarations, and GNU Global's C parser derails on them — the eval caught a
real upstream parser regression triggered by a new kernel annotation style
(see "Known limitations").

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

**Call-graph questions** collapse into single calls:

- `reachability(ksys_read, rw_verify_area)` → the shortest chain, with the
  file:line of every call site, in 0.6 s;
  `reachability(do_sys_openat2, security_file_open)` finds a 4-hop chain in
  1.3 s.
- `blast_radius(git_ref)` maps a `git diff` to its enclosing functions and
  ranks callers by distance (a probe edit inside `ksys_read` radiates to its
  `SYSCALL_DEFINE3` sites in 0.1 s).

## Known limitations (measured, not hidden)

- **Function pointers are invisible to static tagging.** `vfs_read` never
  statically reaches `ext4_file_read_iter` — the route is `f_op->read_iter`.
  `reachability` says so explicitly rather than returning a wrong path; the
  golden set asserts this honest "no" (`reach-fnptr-honesty`).
- **One upstream parser gap, kept as a failing eval case:** GNU Global's C
  parser derails on the sparse annotation kernels newer than ~v6.16 put on
  forward declarations
  (`static void __sched __mutex_lock_slowpath(...) __acquires(lock);`) and
  misses the real `mutex_lock` definition at `kernel/locking/mutex.c:314`.
  The `CONFIG_DEBUG_LOCK_ALLOC` macro variant and the `PREEMPT_RT` variant
  are still found, and older kernels are unaffected (hence 100% in CI on
  v6.16 vs 98% on a 2026 master snapshot).
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
