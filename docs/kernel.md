# Kernel & C features

What this server does that a plain tags index — or a language server without a
build — cannot.

## `#ifdef` guard stacks

Kernel and firmware code defines the same symbol several times and lets the build
configuration pick one. Every result carries the `#if`/`#ifdef` stack it lives
under, and multiply-defined symbols are reported as "N definitions under M
variants":

```text
kmap: 4 definitions under 3 #if variants · 261 refs in 65 files
include/linux/highmem-internal.h:40: static inline void *kmap(struct page *page)  [function; #if CONFIG_HIGHMEM]
include/linux/highmem-internal.h:170: static inline void *kmap(struct page *page)  [function; #if !CONFIG_HIGHMEM]
```

In JSON this is the `guard` list (outermost first) plus `guard_variants`.

## Filtering by a real `.config`

`active_config` accepts a kernel `.config` path **or** an inline macro list such as
`"CONFIG_SMP,BITS_PER_LONG=64,!CONFIG_DEBUG"`. Definitions whose guard stack is
*definitely false* under it are dropped, and the count is reported:

```text
kmap: 3 definitions under 2 #if variants · 261 refs in 65 files
include/linux/highmem-internal.h:40: static inline void *kmap(struct page *page)  [function; #if CONFIG_HIGHMEM]
tools/perf/util/maps.h:17: struct kmap {  [struct]
(1 filtered out by active_config)
```

Filtering is deliberately conservative: unknown macros never drop anything, so a
partial `.config` narrows results without hiding the truth. Available on
`find_definition` and `find_references`.

## Macro-generated symbols

Names minted by token-pasting macros have no literal definition anywhere. They
resolve here, flagged `resolved_via`:

| Query | Resolves to | Via |
|---|---|---|
| `sys_read` | `fs/read_write.c` `SYSCALL_DEFINE3(read, …)` | `macro:SYSCALL_DEFINE` |
| `__x64_sys_openat` | `fs/open.c` `SYSCALL_DEFINE4(openat, …)` | arch wrapper mapped back |
| `compat_sys_ioctl` | `fs/ioctl.c` `COMPAT_SYSCALL_DEFINE3(ioctl, …)` | `macro:COMPAT_SYSCALL_DEFINE` |
| `trace_sched_switch` | `include/trace/events/sched.h` `TRACE_EVENT(sched_switch, …)` | `macro:TRACE_EVENT` |
| `css_set_lock` | `kernel/cgroup/cgroup.c` `DEFINE_SPINLOCK(css_set_lock);` | `macro:DEFINE_SPINLOCK` |

Covered families include `SYSCALL_DEFINE*`, `COMPAT_SYSCALL_DEFINE*`, `TRACE_EVENT`,
`DEFINE_PER_CPU*`, `DEFINE_SPINLOCK`/`DEFINE_MUTEX`, `DECLARE_BITMAP`,
`module_param*` and other `DEFINE_`/`DECLARE_`-shaped macros, plus a last-resort
fuzzy tier. Generator sites rank ahead of same-named textual shadows in
`tools/` or tests. Costs nothing for symbols that resolve normally; disable with
`--no-macro-resolve`.

## Definitions the parser missed

GNU Global's C parser derails on some sparse annotations (for example
`__acquires(lock)` on a forward declaration), losing every definition below that
point in the file. Because `EXPORT_SYMBOL*(sym)` always sits in the `.c` file that
defines `sym`, the server recovers those definitions from the export site with a
cached ctags scan:

| Query | gtags alone | with export recovery |
|---|---|---|
| `mutex_lock` | 7 records, none the real one | `kernel/locking/mutex.c:314` first, kind `function` |

Recovered results are flagged `resolved_via: "ctags:EXPORT_SYMBOL"` and flow
through the whole tool surface. `find_definition` also reports which
`EXPORT_SYMBOL` variant exports a symbol, in `exported`.

## Very widely used symbols

`mutex_lock` has 22,840 references across 5,290 files. Raw lines would be useless,
so `find_references` groups by file above 200 references and tells the agent how to
drill in. `path_prefix` is pushed into GNU Global (`global -S`), which makes the
narrow query 25× faster than filtering afterwards.

`find_callers` refuses symbols referenced in more than 500 files rather than
producing a meaningless list, and points at `find_references`.

## What gets indexed

Indexing feeds `gtags` an explicit file list, so junk never enters the index:

- inside a git repository: `git ls-files` (exact `.gitignore` semantics);
- outside one: a junk-aware walk (skips `node_modules`, `build`, `.venv`, …);
- minus your own `skip_globs` from [configuration](configuration.md).

The database lives in `.gtags-mcp/` at the project root and self-gitignores. A
pre-existing root-level `GTAGS` (from your own `gtags` run) is respected in place.

## Multi-language projects

C, C++, Yacc, Java, PHP and assembly are native to GNU Global. When
universal-ctags and Pygments are available — the automatic toolchain installs both
— the server selects the `native-pygments` parser label, adding Python, Go, Rust,
JavaScript/TypeScript, Ruby and ~150 other languages to the same index.

## Known limitations

- **Function pointers are invisible to static tagging.** `vfs_read` never
  statically reaches `ext4_file_read_iter`; the route is `f_op->read_iter`.
  `reachability` says so explicitly instead of guessing.
- **Prototypes rank alongside definitions.** A header prototype can come before the
  `.c` definition; the ctags `kind` field distinguishes them.
- **C++ templates and overloads** are weaker than C — inherent to tagging.
- **A parser-missed definition that is not exported** stays invisible until the
  upstream parser is fixed; export recovery only works from an `EXPORT_SYMBOL*`
  site.
