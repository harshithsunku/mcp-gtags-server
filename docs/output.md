# Output format

Since v2.0.0 tools answer with **compact text** by default and the JSON envelope on
request. Both are rendered from the same envelope, so they can never disagree.

## Compact text (default)

```text
kmap: 4 definitions under 3 #if variants · 261 refs in 65 files (top: tools/perf/util/machine.c 26, …)
include/linux/highmem-internal.h:40: static inline void *kmap(struct page *page)  [function; #if CONFIG_HIGHMEM]
include/linux/highmem-internal.h:170: static inline void *kmap(struct page *page)  [function; #if !CONFIG_HIGHMEM]
[1-2 of 4 · offset=2 for more]
next: get_symbol_body, find_callers, find_references
```

| Part | Meaning |
|---|---|
| Summary line | Only where one helps: definition count, guard variants, reference and file counts, hottest files, `EXPORT_SYMBOL*` status. |
| `path:line: source` | Repository-relative path, line number, and the source line itself. |
| `[function; #if CONFIG_HIGHMEM]` | Tags: the ctags kind, and the enclosing `#if`/`#ifdef` stack. Unconditional symbols carry no guard tag. |
| `(resolved via …)` | The result came from macro resolution or `EXPORT_SYMBOL` recovery, not a literal index match. |
| `[1-2 of 4 · offset=2 for more]` | Pagination: what you got, what exists, and the offset for the next page. |
| `next: …` | The most useful follow-up tools for this result. |

`find_callers` adds each call site's own source line, so there is nothing left to
re-grep:

```text
ext4_rename  fs/ext4/namei.c:3931: retval = ext4_mark_inode_dirty(handle, whiteout);  (+5 more: 3955, 3986, …)
```

Measured against v1.x's JSON default over seven representative kernel questions,
compact text costs **20% fewer tokens** overall (up to 68% on single lookups).
`find_callers` is about 9% larger on purpose — those call lines replace a
follow-up grep.

## JSON envelope

Pass `format="json"` for machine-readable output:

```json
{
  "tool": "find_definition",
  "root": "/home/ai/linux",
  "results": [
    {"symbol": "vfs_read", "path": "fs/read_write.c", "line": 554, "col": 9,
     "kind": "function", "typeref": "ssize_t", "scope": null,
     "signature": "(struct file * file,char __user * buf,size_t count,loff_t * pos)",
     "guard": [], "snippet": "ssize_t vfs_read(struct file *file, char __user *buf, size_t count, loff_t *pos)"}
  ],
  "total": 1, "offset": 0, "truncated": false,
  "symbol": "vfs_read",
  "definition_count": 1, "guard_variants": 1,
  "reference_count": 4, "file_count": 3,
  "top_files": [{"path": "fs/read_write.c", "count": 2}, {"path": "fs/exec.c", "count": 1}],
  "exported": null,
  "next_tools": ["get_symbol_body", "find_callers", "find_references"],
  "warning": null
}
```

### Envelope fields

| Field | Meaning |
|---|---|
| `tool`, `root` | Which tool answered, and the resolved project root. |
| `results` | Tool-shaped: a list of records, or an object (`find_callees`, `reachability`, `update_index`). |
| `total`, `offset`, `truncated` | Pagination state. |
| `next_tools` | Suggested follow-ups. |
| `warning` | Out-of-band notice, e.g. a background refresh failed or a corrupt index was rebuilt. |
| `message` | Why a result set is empty. |
| `error` | Present instead of `results` when the call failed. |

### Record schema

Symbol locations always use one schema: `symbol`, `path`, `line`, `col`, `kind`,
`typeref`, `scope`, `signature`, `guard`, `snippet`.

- `kind` / `typeref` / `scope` / `signature` come from universal-ctags, with no
  build and no compile database. They are `null` when ctags is unavailable or
  cannot parse the file.
- `guard` is the enclosing `#if`/`#ifdef` stack, outermost first. `[]` means the
  file was scanned and the symbol is unconditional; `null` means guard scanning is
  disabled or the file was unreadable.
- `resolved_via` appears on definition-shaped envelopes when a result came from
  macro resolution (`"macro:SYSCALL_DEFINE"`, `"fuzzy:vfs_read"`) or export
  recovery (`"ctags:EXPORT_SYMBOL"`).

Within a major version, JSON keys are only ever **added** — never renamed or
removed — so parsers keep working.

## Errors

Failures keep the envelope, replace `results` with `error`, and are flagged
`isError` at the protocol level so the model can correct itself:

```text
Error: active_config file not found: /nope/.config
next: find_references
```

Transient states use the same channel, for example the first query on a large
repository while the index is still building:

```text
Indexing /home/ai/linux for the first time (20s elapsed; a Linux-kernel-sized tree
takes about a minute). Retry this call shortly.
```

## Tool metadata

Every tool declares MCP annotations that clients act on:

| Annotation | Value | Effect |
|---|---|---|
| `readOnlyHint` | `true` except `update_index` | Clients auto-approve and parallelize read-only calls. |
| `openWorldHint` | `false` | Nothing reaches outside your machine. |
| `destructiveHint` / `idempotentHint` | on `update_index` | Non-destructive, repeatable. |
| `_meta["anthropic/alwaysLoad"]` | `find_definition`, `find_callers`, `get_symbol_body` | Skips Claude Code's deferred tool search, removing a round-trip before the first call. |

Responses carry exactly one copy of the answer: there is no duplicate
`structuredContent` echo.

## Keeping results small

- Pages default to 100 results; the footer names the next offset.
- Long source lines are truncated at 200 characters.
- Bodies are capped at 300 lines.
- Hot symbols group by file automatically; `path_prefix` narrows them.
- `find_callers` refuses symbols referenced in more than 500 files and points at
  `find_references` instead.
