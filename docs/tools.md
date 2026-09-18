# Tools reference

Eight tools. Every one takes `project_root` and `format`; list-shaped ones take
`limit` and `offset`. All examples below are real output from a Linux kernel
checkout.

| Tool | Answers | Read-only |
|---|---|---|
| [`find_definition`](#find_definition) | Where is this defined, what is it, how widely used? | yes |
| [`find_references`](#find_references) | Every usage site, or a per-file distribution | yes |
| [`get_symbol_body`](#get_symbol_body) | Show me the implementation | yes |
| [`find_callers`](#find_callers) | Who calls this? (incoming calls) | yes |
| [`find_callees`](#find_callees) | What does this call? (outgoing calls) | yes |
| [`reachability`](#reachability) | Can A reach B, through what? | yes |
| [`list_file_symbols`](#list_file_symbols) | What does this file define? | yes |
| [`update_index`](#update_index) | Make my edits visible right now | no |

## Choosing a tool

- **Unfamiliar symbol** → `find_definition`. It doubles as the overview: guard
  variants, reference and file counts, hottest files, `EXPORT_SYMBOL*` status.
- **Impact of a change** → `find_callers` on each changed function. For a diff, use
  the [`/gtags:impact`](plugin.md#slash-commands) command.
- **A symbol used thousands of times** → `find_references` groups by file
  automatically; narrow with `path_prefix`.
- **Strings, comments, Kconfig, Makefiles** → keep using grep. These tools index
  C/C++ symbols, not arbitrary text.

## Shared parameters

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `project_root` | string | auto | Absolute path of the repository to query. Falls back to the client's workspace root, then the server's working directory. |
| `format` | `"text"` \| `"json"` | `"text"` | Compact text, or the [JSON envelope](output.md#json-envelope). |
| `limit` | integer | 100 | Page size for list results. |
| `offset` | integer | 0 | Page offset; result footers tell the agent the next one. |

---

## find_definition

Go to definition, plus a usage summary. Definitions carry their `#ifdef` guard
stack and ctags metadata; macro-generated names and parser-missed definitions
resolve too.

| Parameter | Type | Default |
|---|---|---|
| `symbol` *(required)* | string | — |
| `case_insensitive` | boolean | `false` (skips the usage summary) |
| `active_config` | string | none — a kernel `.config` path or macro list |
| `limit`, `offset`, `project_root`, `format` | | |

```text
kmap: 4 definitions under 3 #if variants · 261 refs in 65 files (top: tools/perf/util/machine.c 26, …)
include/linux/highmem-internal.h:40: static inline void *kmap(struct page *page)  [function; #if CONFIG_HIGHMEM]
include/linux/highmem-internal.h:170: static inline void *kmap(struct page *page)  [function; #if !CONFIG_HIGHMEM]
tools/perf/util/maps.h:17: struct kmap {  [struct]
tools/testing/scatterlist/linux/mm.h:78: static inline void *kmap(struct page *page)  [function]
next: find_references, find_callers
```

A macro-generated name resolves to its generator site and says so:

```text
sys_read: 2 definitions · 4 refs in 4 files (top: include/linux/syscalls.h 1, …)
fs/read_write.c:723: SYSCALL_DEFINE3(read, unsigned int, fd, char __user *, buf, size_t, count)
tools/testing/selftests/proc/proc-self-syscall.c:25: static inline ssize_t sys_read(int fd, void *buf, size_t len)  [function]
(resolved via macro:SYSCALL_DEFINE)
```

A miss returns prefix suggestions instead of nothing. See
[Kernel & C features](kernel.md) for `active_config`.

---

## find_references

Every reference site, each with its guard stack. Above 200 references the result
switches to a per-file distribution — see where usage concentrates, then drill in.

| Parameter | Type | Default |
|---|---|---|
| `symbol` *(required)* | string | — |
| `group_by` | `"auto"` \| `"line"` \| `"file"` | `"auto"` (file above 200 refs) |
| `path_prefix` | string | none — e.g. `"fs/ext4"` |
| `case_insensitive`, `active_config`, `limit`, `offset` | | |

```text
mutex_lock: 22840 references in 5290 files, grouped by file (pass path_prefix=<dir> to list the sites, or group_by='line')
   124  drivers/usb/gadget/function/uvc_configfs.c
   103  drivers/gpu/drm/amd/pm/amdgpu_dpm.c
    65  fs/ceph/mds_client.c
[1-3 of 5290 · offset=3 for more]
```

`path_prefix` is pushed down into GNU Global (`global -S`), so narrowing is cheap —
8 ms instead of 200 ms for the query above:

```text
mutex_lock: 18 references under fs/ext4/
fs/ext4/ext4.h:1900: mutex_lock(&EXT4_SB(sb)->s_fc_lock);  [#if __KERNEL__]
fs/ext4/mballoc.c:3617: mutex_lock(&ext4_grpinfo_slab_create_mutex);
```

Symbols with no in-tree definition (libc calls, some variables) fall back to
symbol-usage records, flagged `"fallback": "symbol_usages"`.

---

## get_symbol_body

The definition's source lines only — a one-screen function never costs a
5,000-line file read.

| Parameter | Type | Default |
|---|---|---|
| `symbol` *(required)* | string | — |
| `max_definitions` | integer | 3 |

```text
== fs/read_write.c:453 ==
int rw_verify_area(int read_write, struct file *file, const loff_t *ppos, size_t count)
{
	int mask = read_write == READ ? MAY_READ : MAY_WRITE;
	…
```

Bodies are capped at 300 lines. Macro-generated and `EXPORT_SYMBOL`-recovered
definitions resolve here too.

---

## find_callers

Who calls this function — references mapped to their enclosing function,
deduplicated, ranked by call count, each with the source line of its first call
site.

| Parameter | Type | Default |
|---|---|---|
| `symbol` *(required)* | string | — |
| `limit`, `offset` | | |

```text
ext4_mark_inode_dirty: 62 calling functions
ext4_rename  fs/ext4/namei.c:3931: retval = ext4_mark_inode_dirty(handle, whiteout);  (+5 more: 3955, 3986, 3991, 4020, 4034)
swap_inode_boot_loader  fs/ext4/ioctl.c:476: err = ext4_mark_inode_dirty(handle, inode);  (+4 more: 484, 492, 513, 514)
[1-2 of 62 · offset=2 for more]
```

A symbol referenced in more than 500 files is refused with a pointer to
`find_references` — caller analysis on `kmalloc` would be noise, not an answer.

---

## find_callees

The outgoing call graph of one function: call sites detected in its body and
verified against the index, split into in-tree (with locations) and
external/unresolved.

```text
ksys_read (fs/read_write.c:705) calls:
  CLASS  include/linux/cleanup.h:301
  fd_empty  include/linux/file.h:45
  file_ppos  fs/read_write.c:700
  vfs_read  fs/read_write.c:554
```

Analysis is capped at 40 distinct call targets per call.

---

## reachability

Does A transitively call B, and through which chain? Breadth-first over the caller
graph, returning the **shortest** chain with each call site.

| Parameter | Type | Default |
|---|---|---|
| `from_symbol`, `to_symbol` *(required)* | string | — |
| `max_depth` | integer | 8 (clamped to 1–12) |

```text
ksys_read reaches rw_verify_area in 2 calls:
  ksys_read (fs/read_write.c:716) calls vfs_read
  vfs_read (fs/read_write.c:565) calls rw_verify_area
  rw_verify_area (fs/read_write.c:453)
```

When there is no static path it says so honestly rather than inventing one:

```text
No call path from 'vfs_read' to 'ext4_file_read_iter' found (within 4 call levels;
6 function(s) explored). Static analysis cannot follow function pointers, so an
indirect path (ops structs, callbacks) may still exist.
```

---

## list_file_symbols

A file's API surface: every symbol it defines, with kind, signature and guards.

| Parameter | Type | Default |
|---|---|---|
| `file_path` *(required)* | string | relative to the project root, or absolute |
| `limit`, `offset` | | |

```text
fs/read_write.c: 80 symbols
39: function unsigned_offsets(struct file * file)
57: function vfs_setpos_cookie(struct file * file,loff_t offset,loff_t maxsize,u64 * cookie)
85: function vfs_setpos(struct file * file,loff_t offset,loff_t maxsize)
```

---

## update_index

The synchronous freshness barrier. Queries refresh the index in the background, so
results can lag very recent edits by a few seconds; call this when the next query
must see them.

| Parameter | Type | Default |
|---|---|---|
| `full` | boolean | `false` — `true` rebuilds from scratch |

```text
Index updated for /home/ai/linux (synchronous — results are now current).
```

This is the only tool that is not marked read-only. `full=true` is rarely needed:
a large branch switch, or a suspected corrupt database (which the server also
detects and repairs on its own).
