---
name: c-code-navigation
description: Navigate large C/C++ codebases (Linux kernel, firmware, vendor BSPs) with the gtags MCP tools instead of grepping. Use when asked who calls a function, what a change affects, whether one function can reach another, where a symbol is defined, which #ifdef variant applies, or what a macro-generated kernel symbol (sys_read, DEFINE_SPINLOCK names) resolves to — and when a grep for a symbol returns hundreds of lines.
license: MIT
---

# Navigating C/C++ code with an index

These tools answer from a GNU Global index built from the source itself: no
build, no `compile_commands.json`, every `#ifdef` variant of every config.
Results come back as compact `path:line: source` rows.

## Which tool for which question

| Question | Tool |
|---|---|
| Who calls this function? What would this change break? | `find_callers` (dedupes per calling function, shows each call's source line) |
| Where is this defined? What is it? How widely used? | `find_definition` (definitions + `#ifdef` guards + usage summary) |
| Every usage, including in a specific directory | `find_references` (`path_prefix="fs/ext4"`, `group_by`) |
| What does this function call? | `find_callees` |
| Can A reach B, and through what? | `reachability` |
| Show me the implementation | `get_symbol_body` (the body only, not the file) |
| What does this file define? | `list_file_symbols` |
| I just edited files and need fresh results now | `update_index` |

**Keep using grep** for string literals, comments, log messages, Kconfig and
Makefiles, and anything that is not a C/C++ symbol. Grep is one cheap call for
those; these tools win when the answer is a symbol relationship or when grep
would return hundreds of lines.

## Reading the output

`path:line: source line  [kind; #if CONFIG_X]` — the tag shows what the symbol
is and the `#ifdef` stack it lives under. A footer like
`[1-100 of 5290 · offset=100 for more]` means there is another page. Results
end with `next:` naming the most useful follow-up tool. Pass `format="json"`
if you need to parse the envelope instead.

## Rules that save round-trips

- **Pick the repository explicitly** when more than one is open, or when the
  server reports it cannot determine one: `project_root="/abs/path/to/repo"`.
- **Hot symbols**: over 200 references come back grouped by file. Narrow with
  `path_prefix` rather than paging through thousands of sites.
- **First query on a new repository** may answer "indexing in progress" — the
  index is building in the background; retry shortly.
- **Kernel specifics**: pass `active_config="/path/to/.config"` to drop
  definitions your config cannot compile; macro-generated names resolve to
  their generator site (flagged `resolved_via`).

## Change impact (what a diff affects)

1. `git diff <ref>` and note each changed C/C++ function (git's hunk headers
   usually name it).
2. `find_callers` on each changed function.
3. Report callers ranked by risk, with `path:line`; for a very widely used
   function use `find_references` to see which subsystems concentrate usage.

## Install note

Use one installation path only. If this plugin is installed, remove any manual
`gtags` MCP entry (`claude mcp remove gtags`) — otherwise two servers run and
every tool appears twice.
