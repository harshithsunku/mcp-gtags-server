"""MCP server that wraps GNU Global (gtags) for C/C++ code navigation.

Designed as a drop-in replacement for grep-based code search in AI coding
agents: instead of scanning the whole tree on every question, queries hit a
gtags index and return a narrow, precise set of lines. The server manages the
index automatically — it builds it on first query and incrementally refreshes
it before each query — so agents never have to think about indexing.

GNU Global (``gtags``/``global``) is resolved through user-space locations
first (see :mod:`gtags_mcp.toolchain`); ``mcp-gtags-server setup`` installs it
without root when it is missing.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP

from . import config as config_module
from . import enrich
from . import fileset
from . import guards
from . import macros
from . import output
from . import toolchain

mcp = FastMCP(
    "gtags-code-navigator",
    instructions=(
        "Indexed C/C++ code navigation backed by GNU Global (gtags). "
        "ALWAYS prefer these tools over grep/text search for code questions: "
        "they answer from a prebuilt index in milliseconds and return only "
        "the relevant lines, even on codebases with millions of lines. "
        "Start with symbol_info for any unfamiliar symbol, or "
        "project_overview for an unfamiliar repo. Then: get_symbol_body to "
        "read an implementation, find_callers / call_hierarchy for impact "
        "analysis, find_callees to see what a function depends on, and "
        "summarize_references first for very widely used symbols. "
        "The index is built and refreshed automatically — never worry about it. "
        "Every tool returns machine-readable JSON by default; pass "
        "format='text' for the human-readable rendering."
    ),
)

Format = Literal["json", "text"]

DEFAULT_LIMIT = 100
MAX_LINE_CHARS = 200
MAX_BODY_LINES = 300
# symbol_info guard-scans at most this many definitions of one symbol.
MAX_GUARDED_DEFS = 50
QUERY_TIMEOUT_SECONDS = 120
INDEX_TIMEOUT_SECONDS = 600
# Skip the incremental freshness check when the same root was updated this
# recently — agent turns often fire many queries back to back. The window is
# adaptive: at least this many seconds, and at least 10x the measured cost of
# the last update (capped below), so a tree where the up-to-date check itself
# takes tens of seconds doesn't re-pay it on every burst of queries.
UPDATE_DEBOUNCE_SECONDS = 5.0
UPDATE_DEBOUNCE_MAX_SECONDS = 300.0

# Default root set by --root / GTAGS_MCP_ROOT; falls back to the server cwd.
_default_root: str | None = None
_last_update: dict[Path, float] = {}
_update_cost: dict[Path, float] = {}
# Background refresh bookkeeping, guarded by _refresh_lock.
_refresh_lock = threading.Lock()
_refresh_threads: dict[Path, threading.Thread] = {}
_refresh_errors: dict[Path, str] = {}
# Parser label forced by --label / GTAGS_MCP_LABEL; None = auto-detect.
_forced_label: str | None = None
_auto_label: str | None = None
_auto_label_resolved = False
# Extra binary directory forced by --bin-dir / GTAGS_MCP_BIN_DIR.
_bin_dir: str | None = None
# ctags metadata enrichment disabled by --no-enrich.
_no_enrich = False
# #ifdef guard scanning disabled by --no-guards.
_no_guards = False
# Macro-family symbol resolution disabled by --no-macro-resolve.
_no_macro_resolve = False

# File extensions GNU Global's built-in parser understands.
_NATIVE_EXTENSIONS = frozenset(
    ".c .h .cc .cpp .cxx .hh .hpp .hxx .java .php .php3 .phtml .y .s .S .asm".split()
)


def _plugin_deps_available() -> bool:
    """True when the ctags + Pygments plugin-parser dependencies are usable."""
    return toolchain.find_ctags(_bin_dir) is not None and toolchain.pygments_available()


def _gtags_label(root: Path | None = None) -> str | None:
    """Resolve GTAGSLABEL: forced label > config file > auto-detected > native."""
    global _auto_label, _auto_label_resolved
    if _forced_label:
        return _forced_label
    if configured := config_module.get_setting("label", root):
        return configured
    if not _auto_label_resolved:
        _auto_label = "native-pygments" if _plugin_deps_available() else None
        _auto_label_resolved = True
    return _auto_label


def _check_global_installed() -> str | None:
    if toolchain.find_global(_bin_dir) is None or toolchain.find_gtags(_bin_dir) is None:
        return (
            "Error: GNU Global (gtags/global) was not found. "
            "Install it into user space with `mcp-gtags-server setup` (no sudo needed), "
            "or via a system package (`apt install global`, `brew install global`), "
            "or point GTAGS_MCP_BIN_DIR / --bin-dir / `bin_dir` in .gtags-mcp.toml "
            "at a directory containing the binaries."
        )
    return None


# Index files (GTAGS/GRTAGS/GPATH) live in this folder inside the project
# root, so they never clutter the user's tree. A pre-existing root-level
# GTAGS (older versions, or the user's own gtags run) is respected as-is.
INDEX_DIR_NAME = ".gtags-mcp"


def _db_dir(root: Path) -> Path:
    """Where root's index database lives: legacy root-level wins, else .gtags-mcp/."""
    if (root / "GTAGS").is_file():
        return root
    return root / INDEX_DIR_NAME


def _detect_root(start: Path) -> Path:
    """Walk up from start to the nearest directory holding an index or .git.

    Monorepo/subdirectory support: a server launched (or queried) from deep
    inside a tree still indexes and reports paths from the project root.
    Falls back to start itself when no marker is found.
    """
    for candidate in (start, *start.parents):
        if (
            (candidate / "GTAGS").is_file()
            or (candidate / INDEX_DIR_NAME / "GTAGS").is_file()
            or (candidate / ".git").exists()
        ):
            return candidate
    return start


def _effective_root(project_root: str | None) -> tuple[Path | None, str | None]:
    """Resolve the project root: explicit arg > --root/env default > config
    > auto-detected (walk up from cwd to GTAGS/.git) > cwd."""
    raw = project_root or _default_root or config_module.get_setting("root")
    if raw is None:
        return _detect_root(Path(os.getcwd()).resolve()), None
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        return None, f"Error: project_root is not a directory: {raw}"
    return root, None


def _run(
    args: list[str],
    cwd: Path,
    timeout: int = QUERY_TIMEOUT_SECONDS,
    input_text: str | None = None,
) -> tuple[str, str, int]:
    """Run a gtags/global command (resolved to user-space binaries) in cwd."""
    resolver = {"global": toolchain.find_global, "gtags": toolchain.find_gtags}
    exe = args[0]
    if resolve := resolver.get(exe):
        exe = resolve(_bin_dir, cwd) or exe
    env = os.environ.copy()
    env.update(toolchain.runtime_env(exe))
    if os.sep in exe:
        # gtags spawns helpers (plugin parsers, python3 shim) via PATH; make
        # sure the resolved user-space bin directory is visible to them too.
        env["PATH"] = str(Path(exe).parent) + os.pathsep + env.get("PATH", "")
    label = _gtags_label(cwd)
    if label:
        env["GTAGSLABEL"] = label
    if args[0] == "global" and (db_dir := _db_dir(cwd)) != cwd:
        # The index lives in .gtags-mcp/, not the root global searches by
        # default; the env pair points queries at it (paths stay root-relative).
        env["GTAGSROOT"] = str(cwd)
        env["GTAGSDBPATH"] = str(db_dir)
    proc = subprocess.run(
        [exe, *args[1:]],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        input=input_text,
    )
    return proc.stdout, proc.stderr, proc.returncode


def _run_index(root: Path, incremental: bool) -> tuple[str, str, int]:
    """(Re)build root's index from an explicit, junk-free file list.

    The list comes from `git ls-files` (exact .gitignore semantics) or a
    junk-aware walk, minus `skip_globs` — see :mod:`gtags_mcp.fileset` — and
    is fed to ``gtags -f -`` so vendored/build files never enter the index.
    Incremental mode (`gtags -i`) adds new files and drops deleted or newly
    ignored ones, which plain ``global -u`` cannot do for a list-built index.
    """
    files = fileset.collect_files(
        root,
        skip_globs=config_module.get_list_setting("skip_globs", root),
        respect_gitignore=config_module.get_bool_setting(
            "respect_gitignore", root, default=True
        ),
    )
    db_dir = _db_dir(root)
    args = ["gtags"] + (["-i"] if incremental else []) + ["--skip-unreadable", "-f", "-"]
    if db_dir != root:
        db_dir.mkdir(exist_ok=True)
        # Self-ignoring dir (cargo-style): git never sees the index files and
        # the user's .gitignore stays untouched.
        gitignore = db_dir / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("*\n")
        args.append(str(db_dir))
    return _run(
        args,
        root,
        timeout=INDEX_TIMEOUT_SECONDS,
        input_text="".join(f"{f}\n" for f in files),
    )


def _refresh_in_background(root: Path) -> None:
    """Thread body: refresh the index incrementally, record outcome under the lock."""
    started = time.monotonic()
    try:
        _, stderr, code = _run_index(root, incremental=True)
        error = (
            f"Warning: background index refresh failed (gtags -i exited {code}): "
            f"{stderr.strip()}"
            if code != 0
            else None
        )
    except Exception as exc:  # noqa: BLE001 — must never kill the thread silently
        error = f"Warning: background index refresh failed: {exc}"
    with _refresh_lock:
        _last_update[root] = time.monotonic()
        _update_cost[root] = _last_update[root] - started
        if error:
            _refresh_errors[root] = error
        _refresh_threads.pop(root, None)


def _wait_for_refresh(root: Path, timeout: float = INDEX_TIMEOUT_SECONDS) -> None:
    """Block until any in-flight background refresh for root has finished."""
    with _refresh_lock:
        thread = _refresh_threads.get(root)
    if thread is not None:
        thread.join(timeout)


def _ensure_index(root: Path) -> str | None:
    """Build the index if missing; else kick a non-blocking background refresh.

    Queries are never blocked by refresh: they answer from the current index
    while `gtags -i` catches up in a daemon thread. Staleness is bounded by
    the adaptive debounce window plus the refresh duration; the update_index
    tool is the synchronous barrier when guaranteed freshness is needed.
    Returns an error string only for fatal conditions (failed full build);
    a failed *background* refresh is surfaced as a warning on the next query.
    """
    if not (_db_dir(root) / "GTAGS").is_file():
        stdout, stderr, code = _run_index(root, incremental=False)
        if code != 0:
            return (
                f"Error: automatic indexing failed (gtags exited {code}): "
                f"{stderr.strip() or stdout.strip()}"
            )
        _last_update[root] = time.monotonic()
        return None

    with _refresh_lock:
        now = time.monotonic()
        window = min(
            UPDATE_DEBOUNCE_MAX_SECONDS,
            max(UPDATE_DEBOUNCE_SECONDS, 10.0 * _update_cost.get(root, 0.0)),
        )
        refresh_due = now - _last_update.get(root, 0.0) >= window
        if refresh_due and root not in _refresh_threads:
            thread = threading.Thread(
                target=_refresh_in_background, args=(root,), daemon=True
            )
            _refresh_threads[root] = thread
            thread.start()
    return None


def _pop_refresh_warning(root: Path | None) -> str | None:
    """Consume a pending background-refresh failure warning for root, if any."""
    if root is None:
        return None
    with _refresh_lock:
        return _refresh_errors.pop(root, None)


def _paginate(text: str, limit: int, offset: int) -> str:
    lines = [
        line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS] + " ..."
        for line in text.splitlines()
    ]
    total = len(lines)
    limit = max(1, limit)
    offset = max(0, offset)
    page = lines[offset : offset + limit]
    if not page:
        return f"No results in range: offset {offset} is past the last of {total} matches."
    body = "\n".join(page)
    end = offset + len(page)
    if offset == 0 and end == total:
        return body
    footer = f"— showing {offset + 1}-{end} of {total} matches"
    if end < total:
        footer += f"; pass offset={end} to continue"
    return f"{body}\n{footer}"


def _raw_global(
    flags: list[str], project_root: str | None
) -> tuple[str | None, Path | None, str | None]:
    """Resolve root, ensure the index, run `global`. Returns (stdout, root, error)."""
    if err := _check_global_installed():
        return None, None, err
    root, err = _effective_root(project_root)
    if err:
        return None, None, err
    if err := _ensure_index(root):
        return None, root, err
    stdout, stderr, code = _run(["global", *flags], cwd=root)
    # `global` exits non-zero both for real errors and for "no match found";
    # only the former writes to stderr.
    if code != 0 and stderr.strip():
        return None, root, f"Error: global exited with code {code}: {stderr.strip()}"
    return stdout, root, None


def _query_global(
    tool: str,
    flags: list[str],
    project_root: str | None,
    empty_message: str,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    format: Format = "json",
    parse: str = "cxref",  # cxref | symbol | path — shape of `global` stdout
    record_symbol: str | None = None,
    enrich_records: bool = False,  # ctags metadata on definition-shaped results
    guard_records: bool = False,  # #ifdef guard stacks on cxref results
    active_config: str | None = None,  # .config / macro list to filter guards by
    macro_symbol: str | None = None,  # macro-family fallback for this symbol
) -> str:
    """Shared plumbing for all read-only `global` queries."""
    stdout, root, err = _raw_global(flags, project_root)
    if err:
        return output.error(tool, err, root) if format == "json" else err
    warning = _pop_refresh_warning(root)
    if format == "text":
        suffix = f"\n\n{warning}" if warning else ""
        if not stdout.strip():
            if macro_symbol:
                hits, via = _macro_resolve(macro_symbol, project_root, root, True)
                if hits:
                    text = "\n".join(
                        f"{s:<16} {lineno:>4} {path} {src}" for s, lineno, path, src in hits
                    )
                    return _paginate(text, limit, offset) + f"\n(resolved via {via})" + suffix
            return empty_message + suffix
        return _paginate(stdout.rstrip(), limit, offset) + suffix
    if parse == "cxref":
        items = [
            output.record(record_symbol or r[0], r[2], r[1], r[3])
            for line in stdout.splitlines()
            if (r := _parse_cxref(line))
        ]
    elif parse == "symbol":
        items = [{"symbol": line.strip()} for line in stdout.splitlines() if line.strip()]
    else:  # path
        items = [
            {"path": p[2:] if p.startswith("./") else p}
            for line in stdout.splitlines()
            if (p := line.strip())
        ]
    extra: dict = {}
    if macro_symbol and parse == "cxref":
        hits, via = _macro_resolve(macro_symbol, project_root, root, not items)
        if hits:
            known = {(rec["path"], rec["line"]) for rec in items}
            resolved = [output.record(s, path, lineno, src) for s, lineno, path, src in hits]
            # Generator sites come FIRST: for a macro-generated name they are
            # the real answer; same-named textual matches (test helpers,
            # tools/) are shadows.
            items = [
                rec for rec in resolved if (rec["path"], rec["line"]) not in known
            ] + items
            extra["resolved_via"] = via
    if active_config and parse == "cxref":
        # Explicit intent must not fail silently: bad specs and disabled
        # guard scanning are errors, not quietly-unfiltered results.
        if not _guards_enabled(root):
            return output.error(
                tool,
                "Error: active_config needs guard scanning, which is disabled "
                "(--no-guards / GTAGS_MCP_GUARDS / `guards = false`).",
                root,
            )
        cfg, cfg_err = guards.load_active_config(active_config, root)
        if cfg_err:
            return output.error(tool, cfg_err, root)
        # Filtering changes totals, so it must run BEFORE pagination. This
        # scans every result file (not just one page) — opt-in cost, cached.
        items, dropped = _maybe_guard(items, root, cfg)
        extra["config_filtered"] = dropped
    page, total, truncated = output.paginate(items, limit, offset)
    if enrich_records and parse == "cxref":
        _maybe_enrich(page, root)  # after pagination: only one page pays ctags
    if guard_records and parse == "cxref" and not active_config:
        _maybe_guard(page, root)  # guards already filled when config-filtered
    if not items:
        extra["message"] = empty_message
    return output.envelope(
        tool,
        root,
        page,
        total=total,
        offset=offset,
        truncated=truncated,
        warning=warning,
        **extra,
    )


def _parse_cxref(line: str) -> tuple[str, int, str, str] | None:
    """Parse one `global -x` output line: symbol, line-number, path, source."""
    parts = line.split(None, 3)
    if len(parts) < 3 or not parts[1].isdigit():
        return None
    symbol, lineno, path = parts[0], int(parts[1]), parts[2]
    source = parts[3] if len(parts) == 4 else ""
    return symbol, lineno, path, source


def _enrichment_enabled(root: Path | None) -> bool:
    """ctags metadata enrichment: --no-enrich > GTAGS_MCP_ENRICH > config > on."""
    if _no_enrich:
        return False
    if env := os.environ.get("GTAGS_MCP_ENRICH"):
        return env.strip().lower() not in ("0", "false", "no", "off")
    return config_module.get_bool_setting("enrich", root, default=True)


def _guards_enabled(root: Path | None) -> bool:
    """#ifdef guard scanning: --no-guards > GTAGS_MCP_GUARDS > config > on."""
    if _no_guards:
        return False
    if env := os.environ.get("GTAGS_MCP_GUARDS"):
        return env.strip().lower() not in ("0", "false", "no", "off")
    return config_module.get_bool_setting("guards", root, default=True)


def _macro_resolve_enabled(root: Path | None) -> bool:
    """Macro-family resolution: --no-macro-resolve > GTAGS_MCP_MACRO_RESOLVE
    > config > on."""
    if _no_macro_resolve:
        return False
    if env := os.environ.get("GTAGS_MCP_MACRO_RESOLVE"):
        return env.strip().lower() not in ("0", "false", "no", "off")
    return config_module.get_bool_setting("macro_resolve", root, default=True)


def _cxrefs(flags: list[str], project_root: str | None) -> list[macros.Cxref]:
    """Run one `global` query and return parsed cxref tuples ([] on error)."""
    stdout, _, err = _raw_global(flags, project_root)
    if err or not stdout:
        return []
    return [r for line in stdout.splitlines() if (r := _parse_cxref(line))]


def _macro_resolve(
    symbol: str, project_root: str | None, root: Path | None, direct_empty: bool
) -> tuple[list[macros.Cxref], str | None]:
    """Macro-family fallback for a definition lookup (see gtags_mcp.macros)."""
    if not _macro_resolve_enabled(root):
        return [], None
    return macros.resolve(
        symbol, lambda flags: _cxrefs(flags, project_root), direct_empty=direct_empty
    )


def _maybe_guard(
    records: list[dict],
    root: Path | None,
    cfg: guards.ActiveConfig | None = None,
) -> tuple[list[dict], int]:
    """Fill the ``guard`` field on records in place; optionally config-filter.

    With *cfg*, records whose guard stack is DEFINITELY false under it are
    dropped (unknown macros never drop anything). Returns (kept records,
    dropped count). Best-effort: unreadable files leave ``guard`` null.
    """
    if not records or root is None or not _guards_enabled(root):
        return records, 0
    kept: list[dict] = []
    dropped = 0
    for rec in records:
        table = guards.guards_for_file(root / rec["path"])
        satisfiable: bool | None = None
        if table is not None:
            stack = table.stack_at(rec["line"])
            rec["guard"] = guards.guard_list(stack)
            if cfg is not None:
                satisfiable = guards.stack_satisfiable(stack, cfg)
        if satisfiable is False:
            dropped += 1
            continue
        kept.append(rec)
    return kept, dropped


def _maybe_enrich(records: list[dict], root: Path | None) -> None:
    """Fill kind/typeref/scope/signature on definition-shaped records in place.

    Best-effort: without Universal Ctags (+json), or for files ctags cannot
    parse, records keep their null metadata. One ctags run per distinct file,
    cached in :mod:`gtags_mcp.enrich`.
    """
    if not records or root is None or not _enrichment_enabled(root):
        return
    if not enrich.available(_bin_dir):
        return
    by_path: dict[str, list[dict]] = {}
    for rec in records:
        by_path.setdefault(rec["path"], []).append(rec)
    for path, recs in by_path.items():
        tags = enrich.tags_for_file(root / path, _bin_dir)
        if not tags:
            continue
        for rec in recs:
            if tag := enrich.best_tag(tags, rec["symbol"], rec["line"]):
                rec["kind"] = tag["kind"]
                rec["typeref"] = tag["typeref"]
                rec["scope"] = tag["scope"]
                rec["signature"] = tag["signature"]


def _extract_python_body(lines: list[str], i: int) -> list[str]:
    """Extract an indentation-delimited Python def/class body starting at lines[i]."""
    out = [lines[i]]
    base_indent = len(lines[i]) - len(lines[i].lstrip())
    pending_blanks: list[str] = []
    j = i + 1
    while j < len(lines) and len(out) < MAX_BODY_LINES:
        line = lines[j]
        if not line.strip():
            pending_blanks.append(line)
            j += 1
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= base_indent:
            break
        out.extend(pending_blanks)
        pending_blanks = []
        out.append(line)
        j += 1
    if len(out) >= MAX_BODY_LINES:
        out.append(f"... body truncated at {MAX_BODY_LINES} lines ...")
    return out


def _extract_body(file: Path, start_line: int) -> list[str]:
    """Extract a definition body starting at start_line (1-based).

    C-family files use a brace-counting heuristic: read until the block
    opened by the first `{` closes; prototypes, typedefs, and macros without
    a block end at the first line not continued by a backslash that ends in
    `;` (or after a short window if no block ever opens). Python files use
    indentation instead.
    """
    lines = file.read_text(errors="replace").splitlines()
    i = start_line - 1
    if i < 0 or i >= len(lines):
        return []
    if file.suffix in (".py", ".pyi"):
        return _extract_python_body(lines, i)
    out: list[str] = []
    depth = 0
    seen_brace = False
    j = i
    while j < len(lines) and len(out) < MAX_BODY_LINES:
        line = lines[j]
        out.append(line)
        depth += line.count("{") - line.count("}")
        if "{" in line:
            seen_brace = True
        if seen_brace and depth <= 0:
            break
        if not seen_brace:
            stripped = line.rstrip()
            if stripped.endswith("\\"):  # continuation (macro or split declaration)
                j += 1
                continue
            if out[0].lstrip().startswith("#"):
                break  # preprocessor directive ends when continuations stop
            if stripped.endswith(";") or (j - i) >= 20:
                break  # prototype/typedef/one-liner, or no block in sight
        j += 1
    else:
        if len(out) >= MAX_BODY_LINES:
            out.append(f"... body truncated at {MAX_BODY_LINES} lines ...")
    return out


def _callers_of(
    symbol: str, project_root: str | None
) -> tuple[dict[tuple[str, str], list[int]] | None, str | None]:
    """Map every reference to `symbol` to its enclosing function.

    Returns ({(caller, path): [ref lines]}, None) — empty dict when there are
    no references — or (None, error message).
    """
    stdout, _, err = _raw_global(["-rx", "--", symbol], project_root)
    if err:
        return None, err
    refs = [r for line in stdout.splitlines() if (r := _parse_cxref(line))]
    if not refs:
        return {}, None

    by_file: dict[str, list[int]] = {}
    for _, lineno, path, _ in refs:
        by_file.setdefault(path, []).append(lineno)
    if len(by_file) > 500:
        return None, (
            f"'{symbol}' is referenced in {len(by_file)} files ({len(refs)} sites) — "
            "too broad for caller analysis. Use summarize_references to see the "
            "per-file distribution, then narrow down."
        )

    callers: dict[tuple[str, str], list[int]] = {}
    for path, ref_lines in by_file.items():
        defs_out, _, def_err = _raw_global(["-fx", "--", path], project_root)
        defs: list[tuple[int, str]] = []
        if defs_out and not def_err:
            defs = sorted(
                (d[1], d[0])
                for line in defs_out.splitlines()
                if (d := _parse_cxref(line))
            )
        for ref_line in sorted(ref_lines):
            enclosing = "(file scope)"
            for def_line, def_sym in defs:
                if def_line <= ref_line:
                    enclosing = def_sym
                else:
                    break
            callers.setdefault((enclosing, path), []).append(ref_line)
    return callers, None


# Identifiers that look like calls in C or Python source but never are.
_NON_CALLS = frozenset(
    "if else for while do switch return sizeof defined typeof alignof offsetof "
    "case goto break continue struct union enum static const volatile inline "
    "unsigned signed int char long short float double void "
    "def class lambda elif except raise yield assert del pass with not and or "
    "in is print len range super self str list dict set tuple type isinstance".split()
)


@mcp.tool()
def find_definition(
    symbol: str,
    project_root: str | None = None,
    case_insensitive: bool = False,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    format: Format = "json",
    active_config: str | None = None,
) -> str:
    """Find where a C/C++ symbol (function, struct, macro, typedef, enum) is defined.

    Use this INSTEAD of grep or text search whenever you need a symbol's
    definition — it is an indexed lookup that returns only the definition
    site(s), not every textual occurrence, and stays fast on codebases with
    millions of lines. The index is built and refreshed automatically.
    Multiply-defined symbols (kernel/firmware `#ifdef` alternates) come back
    with each definition's guard stack, so you can tell which one applies.
    Macro-generated symbols resolve too: querying "sys_read",
    "trace_sched_switch", or a DEFINE_SPINLOCK/DEFINE_PER_CPU/module_param
    name returns the generator invocation site (SYSCALL_DEFINE3(read, ...)),
    flagged by a "resolved_via" field in the envelope — no preprocessor or
    build needed.

    Args:
        symbol: Exact symbol name, e.g. "tcp_v4_rcv" or "list_head".
        project_root: Project directory. Omit to use the server's default
            (auto-detected by walking up from its working directory).
        case_insensitive: Match the symbol ignoring case.
        limit: Maximum result lines to return (default 100).
        offset: Skip this many result lines (for pagination).
        format: "json" (default) for a structured envelope with
            {symbol, path, line, col, kind, typeref, scope, signature,
            guard, snippet} records — kind/typeref/scope/signature carry
            ctags metadata (what the symbol is, its type/return type, its
            enclosing scope, its parameter list) when available; guard is
            the enclosing #if/#ifdef stack (outermost first, [] = none);
            "text" for lines of: symbol line-number file source-line.
        active_config: Path to a kernel .config (absolute or root-relative),
            or a comma-separated macro list like "CONFIG_SMP,BITS_PER_LONG=64"
            (prefix ! for known-undefined). Definitions whose guard stack is
            DEFINITELY false under it are dropped; unknown macros never drop
            anything. The envelope reports the drop count as config_filtered.
    """
    flags = ["-x"] + (["-i"] if case_insensitive else []) + ["--", symbol]
    return _query_global(
        "find_definition",
        flags,
        project_root,
        f"No definition found for symbol '{symbol}'.",
        limit,
        offset,
        format,
        enrich_records=True,
        guard_records=True,
        active_config=active_config,
        macro_symbol=symbol,
    )


@mcp.tool()
def find_references(
    symbol: str,
    project_root: str | None = None,
    case_insensitive: bool = False,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    format: Format = "json",
    active_config: str | None = None,
) -> str:
    """Find all call/usage sites of a defined C/C++ symbol.

    Use this INSTEAD of grep when you need who calls a function or uses a
    type — grep returns every textual match including comments and strings,
    while this returns only real reference sites from the index, instantly
    even on huge trees. Each reference carries its #if/#ifdef guard stack,
    so you can see which call sites are conditional.

    Args:
        symbol: Exact symbol name whose call/usage sites you want.
        project_root: Project directory. Omit to use the server's default.
        case_insensitive: Match the symbol ignoring case.
        limit: Maximum result lines to return (default 100).
        offset: Skip this many result lines (for pagination).
        format: "json" (default) for structured records (guard = enclosing
            #if/#ifdef stack); "text" for lines of:
            symbol line-number file source-line.
        active_config: Kernel .config path or comma-separated macro list;
            drops references whose guard stack is definitely false under it
            (reported as config_filtered). Unknown macros never drop anything.
    """
    flags = ["-rx"] + (["-i"] if case_insensitive else []) + ["--", symbol]
    return _query_global(
        "find_references",
        flags,
        project_root,
        f"No references found for symbol '{symbol}'.",
        limit,
        offset,
        format,
        guard_records=True,
        active_config=active_config,
    )


@mcp.tool()
def get_symbol_body(
    symbol: str,
    project_root: str | None = None,
    max_definitions: int = 3,
    format: Format = "json",
) -> str:
    """Return the full source code of a symbol's definition — just the body.

    Use this INSTEAD of reading a whole file when you need to see how a
    function, struct, or macro is implemented. It jumps straight to the
    definition via the index and extracts only that definition's lines, so
    a one-screen function never costs you a 5000-line file read.

    Args:
        symbol: Exact symbol name, e.g. "tcp_v4_rcv".
        project_root: Project directory. Omit to use the server's default.
        max_definitions: If the symbol has multiple definitions, return at
            most this many bodies (default 3).
        format: "json" (default) for {path, line, body} items; "text" for
            "=== path:line ===" separated bodies.
    """
    stdout, root, err = _raw_global(["-x", "--", symbol], project_root)
    if err:
        return output.error("get_symbol_body", err, root) if format == "json" else err
    refs = [r for line in stdout.splitlines() if (r := _parse_cxref(line))]
    if not refs:
        message = f"No definition found for symbol '{symbol}'."
        if format == "json":
            return output.envelope("get_symbol_body", root, [], message=message)
        return message
    omitted = max(0, len(refs) - max_definitions)
    if format == "json":
        items = [
            {
                "path": path,
                "line": lineno,
                "body": "\n".join(_extract_body(root / path, lineno)),
            }
            for _, lineno, path, _ in refs[:max_definitions]
        ]
        return output.envelope(
            "get_symbol_body",
            root,
            items,
            total=len(refs),
            truncated=omitted > 0,
            omitted_definitions=omitted,
        )
    chunks: list[str] = []
    for _, lineno, path, _ in refs[:max_definitions]:
        body = _extract_body(root / path, lineno)
        chunks.append(f"=== {path}:{lineno} ===\n" + "\n".join(body))
    if omitted:
        chunks.append(
            f"... {omitted} more definition(s) not shown; "
            "use find_definition to list them all."
        )
    return "\n\n".join(chunks)


@mcp.tool()
def find_callers(
    symbol: str,
    project_root: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    format: Format = "json",
) -> str:
    """Find the FUNCTIONS that call a symbol, deduplicated, with call counts.

    Use this INSTEAD of find_references when you want the call graph rather
    than raw match lines: each reference site is mapped to its enclosing
    function, so 100 call sites inside one loop-heavy caller collapse to a
    single result line. This is the highest signal-to-noise view of "who
    uses this?" on a large codebase.

    Args:
        symbol: Exact symbol name whose callers you want.
        project_root: Project directory. Omit to use the server's default.
        limit: Maximum result lines to return (default 100).
        offset: Skip this many result lines (for pagination).
        format: "json" (default) for {caller, path, sites} items; "text" for
            lines of: caller-function  file  N call site(s) at lines ...
    """
    root, _ = _effective_root(project_root)
    callers, err = _callers_of(symbol, project_root)
    if err:
        if format == "json":
            hints = ["summarize_references"] if "too broad" in err else None
            return output.error("find_callers", err, root, hints=hints)
        return err
    ranked = sorted(callers.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    if format == "json":
        items = [
            {"caller": caller, "path": path, "sites": sites}
            for (caller, path), sites in ranked
        ]
        page, total, truncated = output.paginate(items, limit, offset)
        extra = {} if items else {"message": f"No references found for symbol '{symbol}'."}
        return output.envelope(
            "find_callers",
            root,
            page,
            total=total,
            offset=offset,
            truncated=truncated,
            **extra,
        )
    if not callers:
        return f"No references found for symbol '{symbol}'."
    rows = []
    for (caller, path), sites in ranked:
        shown = ", ".join(str(n) for n in sites[:5])
        more = f", +{len(sites) - 5} more" if len(sites) > 5 else ""
        plural = "s" if len(sites) != 1 else ""
        rows.append(f"{caller}  {path}  {len(sites)} call site{plural} at line(s) {shown}{more}")
    return _paginate("\n".join(rows), limit, offset)


@mcp.tool()
def summarize_references(
    symbol: str,
    project_root: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    format: Format = "json",
) -> str:
    """Per-file reference counts for a symbol — the cheapest wide view.

    Use this FIRST for very widely used symbols (thousands of references):
    it collapses the result to one line per file, sorted by count, so you
    can see where usage concentrates and then drill into a specific file
    with find_references or find_callers. Never floods the context window.

    Args:
        symbol: Exact symbol name.
        project_root: Project directory. Omit to use the server's default.
        limit: Maximum result lines to return (default 100).
        offset: Skip this many result lines (for pagination).
        format: "json" (default) for {path, count} items plus
            total_references; "text" for lines of: count  file.
    """
    stdout, root, err = _raw_global(["-rx", "--", symbol], project_root)
    if err:
        return output.error("summarize_references", err, root) if format == "json" else err
    refs = [r for line in stdout.splitlines() if (r := _parse_cxref(line))]
    counts: dict[str, int] = {}
    for _, _, path, _ in refs:
        counts[path] = counts.get(path, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    if format == "json":
        items = [{"path": path, "count": count} for path, count in ranked]
        page, total, truncated = output.paginate(items, limit, offset)
        extra = {} if items else {"message": f"No references found for symbol '{symbol}'."}
        return output.envelope(
            "summarize_references",
            root,
            page,
            total=total,
            offset=offset,
            truncated=truncated,
            total_references=len(refs),
            **extra,
        )
    if not refs:
        return f"No references found for symbol '{symbol}'."
    rows = [f"{count:6d}  {path}" for path, count in ranked]
    header = f"{len(refs)} references across {len(counts)} files:"
    return header + "\n" + _paginate("\n".join(rows), limit, offset)


@mcp.tool()
def call_hierarchy(
    symbol: str,
    project_root: str | None = None,
    depth: int = 2,
    format: Format = "json",
) -> str:
    """Multi-level callers tree: who calls X, who calls THOSE, and so on.

    Use this for impact analysis — "if I change this function, what code
    paths are affected?" — instead of running find_references over and over.
    Each caller is expanded recursively up to `depth` levels, deduplicated,
    cycle-safe, and capped so the output stays compact even on huge trees.

    Args:
        symbol: Exact symbol name at the root of the tree.
        project_root: Project directory. Omit to use the server's default.
        depth: How many caller levels to expand (1-5, default 2).
        format: "json" (default) for a nested {caller, path, site_count,
            callers} tree; "text" for a box-drawing rendering.
    """
    depth = max(1, min(depth, 5))
    stdout, root, err = _raw_global(["-x", "--", symbol], project_root)
    if err:
        return output.error("call_hierarchy", err, root) if format == "json" else err
    defs = [r for line in stdout.splitlines() if (r := _parse_cxref(line))]

    visited = {symbol}
    state = {"nodes": 0, "capped": False}
    MAX_NODES = 150
    MAX_PER_NODE = 25

    def expand(sym: str, level: int) -> list[dict]:
        callers, cerr = _callers_of(sym, project_root)
        if cerr:
            return [{"note": f"({cerr})"}]
        if not callers:
            return []
        children: list[dict] = []
        items = sorted(callers.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        for (caller, path), sites in items[:MAX_PER_NODE]:
            if state["nodes"] >= MAX_NODES:
                if not state["capped"]:
                    children.append(
                        {
                            "note": f"... tree capped at {MAX_NODES} nodes; "
                            "rerun with a smaller depth or start from a deeper symbol."
                        }
                    )
                    state["capped"] = True
                return children
            child: dict = {"caller": caller, "path": path, "site_count": len(sites)}
            expandable = caller not in ("(file scope)",) and level < depth
            if caller == sym:
                child["recursive"] = True
                expandable = False
            elif caller in visited:
                child["repeated"] = True  # already shown elsewhere in the tree
                expandable = False
            state["nodes"] += 1
            if expandable:
                visited.add(caller)
                child["callers"] = expand(caller, level + 1)
            children.append(child)
        if len(items) > MAX_PER_NODE:
            children.append(
                {
                    "note": f"... {len(items) - MAX_PER_NODE} more callers not shown "
                    f"(use find_callers('{sym}') with offset to page through them)"
                }
            )
        return children

    callers_tree = expand(symbol, 1)

    if format == "json":
        tree = {
            "symbol": symbol,
            "definition": {"path": defs[0][2], "line": defs[0][1]} if defs else None,
            "callers": callers_tree,
        }
        extra = {} if callers_tree else {"message": f"No references found for '{symbol}'."}
        return output.envelope(
            "call_hierarchy",
            root,
            tree,
            total=state["nodes"],
            truncated=state["capped"],
            hints=output.next_tools("call_hierarchy", bool(callers_tree)),
            **extra,
        )

    if defs:
        lines = [f"{symbol}  (definition: {defs[0][2]}:{defs[0][1]})"]
    else:
        lines = [f"{symbol}  (no in-tree definition)"]

    def render(children: list[dict], prefix: str) -> None:
        for i, child in enumerate(children):
            last = i == len(children) - 1
            branch = "└─ " if last else "├─ "
            if "note" in child:
                lines.append(f"{prefix}└─ {child['note']}")
                continue
            plural = "s" if child["site_count"] != 1 else ""
            label = f"{child['caller']}  {child['path']}  ({child['site_count']} site{plural})"
            if child.get("recursive"):
                label += "  (recursive)"
            elif child.get("repeated"):
                label += "  (already shown above)"
            lines.append(f"{prefix}{branch}{label}")
            if child.get("callers"):
                render(child["callers"], prefix + ("   " if last else "│  "))

    render(callers_tree, "")
    if len(lines) == 1:
        lines.append(f"(no references found for '{symbol}')")
    return "\n".join(lines)


@mcp.tool()
def find_callees(
    symbol: str,
    project_root: str | None = None,
    format: Format = "json",
) -> str:
    """What functions does this function CALL? (the outgoing call graph)

    Use this to understand a function's dependencies without reading any
    file: it extracts the function's body, detects call sites, and verifies
    each against the index. In-tree callees come back with their definition
    locations (ready for get_symbol_body); external names (libc etc.) are
    listed separately.

    Args:
        symbol: Exact name of the function to analyze.
        project_root: Project directory. Omit to use the server's default.
        format: "json" (default) for {in_tree: [{symbol, path, line}],
            external: [names]}; "text" for a sectioned listing.
    """
    stdout, root, err = _raw_global(["-x", "--", symbol], project_root)
    if err:
        return output.error("find_callees", err, root) if format == "json" else err
    defs = [r for line in stdout.splitlines() if (r := _parse_cxref(line))]
    if not defs:
        message = f"No definition found for symbol '{symbol}'."
        if format == "json":
            return output.envelope(
                "find_callees",
                root,
                {"in_tree": [], "external": []},
                total=0,
                hints=output.next_tools("get_symbol_body", False),
                message=message,
            )
        return message
    _, lineno, path, _ = defs[0]
    body = "\n".join(_extract_body(root / path, lineno))

    seen: set[str] = set()
    candidates: list[str] = []
    for name in re.findall(r"\b([A-Za-z_]\w*)\s*\(", body):
        if name != symbol and name not in _NON_CALLS and name not in seen:
            seen.add(name)
            candidates.append(name)

    capped = max(0, len(candidates) - 40)
    candidates = candidates[:40]

    in_tree: list[dict] = []
    external: list[str] = []
    for name in candidates:
        out, _, cerr = _raw_global(["-x", "--", name], project_root)
        target = _parse_cxref(out.splitlines()[0]) if out and out.strip() else None
        if target and not cerr:
            in_tree.append({"symbol": name, "path": target[2], "line": target[1]})
        else:
            external.append(name)

    if format == "json":
        extra = {} if candidates else {
            "message": f"{symbol} ({path}:{lineno}) makes no detectable calls."
        }
        return output.envelope(
            "find_callees",
            root,
            {"in_tree": in_tree, "external": external},
            total=len(in_tree) + len(external),
            truncated=capped > 0,
            definition={"path": path, "line": lineno},
            capped_call_targets=capped,
            **extra,
        )
    if not candidates:
        return f"{symbol} ({path}:{lineno}) makes no detectable calls."
    note = (
        f"\n(analysis capped at 40 of {len(candidates) + capped} distinct call targets)"
        if capped
        else ""
    )
    sections = [f"Callees of {symbol} ({path}:{lineno}):"]
    if in_tree:
        sections.append("In-tree (use get_symbol_body to read them):")
        sections.extend(f"  {c['symbol']}  {c['path']}:{c['line']}" for c in in_tree)
    if external:
        sections.append(f"External/unresolved: {', '.join(external)}")
    return "\n".join(sections) + note


# Caller-graph walks (reachability / blast_radius) expand at most this many
# functions per call — beyond that the answer is "too widely connected".
MAX_GRAPH_EXPANSIONS = 200

# An all-caps "caller" is a macro-invocation name (SYSCALL_DEFINE3,
# TRACE_EVENT, ...) shared by hundreds of unrelated sites — expanding it by
# name would pull every other invocation into the walk. List it, never
# expand through it.
_MACROISH_RE = re.compile(r"[A-Z][A-Z0-9_]*")


@mcp.tool()
def reachability(
    from_symbol: str,
    to_symbol: str,
    project_root: str | None = None,
    max_depth: int = 8,
    format: Format = "json",
) -> str:
    """Does FROM transitively call TO — and through which call chain?

    Use this instead of chaining find_callers/find_callees rounds when the
    question is "can this function end up in that one?" (does a syscall
    reach this driver? can this cleanup path hit this allocator?). It
    breadth-first-searches the caller graph upward from `to_symbol` and
    returns the SHORTEST call chain, each hop with the file:line of the
    call site — one call answers what would otherwise take many.

    Args:
        from_symbol: The caller end of the question ("can this reach ...").
        to_symbol: The callee end ("... this function?").
        project_root: Project directory. Omit to use the server's default.
        max_depth: Longest chain to consider, in calls (1-12, default 8).
        format: "json" (default) for {path_found, hops, depth,
            nodes_explored}; "text" for the chain rendering. Hops run from
            from_symbol to to_symbol; each hop's path/line is the call site
            of the NEXT hop (the last hop is to_symbol at its definition).
    """
    max_depth = max(1, min(max_depth, 12))
    stdout, root, err = _raw_global(["-x", "--", to_symbol], project_root)
    if err:
        return output.error("reachability", err, root) if format == "json" else err
    defs = [r for line in stdout.splitlines() if (r := _parse_cxref(line))]
    to_location = {"path": defs[0][2], "line": defs[0][1]} if defs else {}

    # BFS upward from to_symbol. link[caller] = (callee, path, sites) is the
    # tree edge that discovered `caller`, i.e. where caller calls callee.
    link: dict[str, tuple[str, str, list[int]]] = {}
    seen = {to_symbol}
    frontier = [to_symbol]
    explored = 0
    skipped_broad = 0
    found = from_symbol == to_symbol
    depth_walked = 0
    while frontier and not found and depth_walked < max_depth:
        depth_walked += 1
        next_frontier: list[str] = []
        for node in frontier:
            if explored >= MAX_GRAPH_EXPANSIONS:
                break
            explored += 1
            callers, cerr = _callers_of(node, project_root)
            if cerr:
                skipped_broad += 1  # too widely referenced to expand
                continue
            for (caller, path), sites in sorted(callers.items()):
                if caller in seen or caller == "(file scope)":
                    continue
                seen.add(caller)
                link[caller] = (node, path, sites)
                if caller == from_symbol:
                    found = True
                    break
                if not _MACROISH_RE.fullmatch(caller):
                    next_frontier.append(caller)
            if found:
                break
        if explored >= MAX_GRAPH_EXPANSIONS:
            break
        frontier = next_frontier

    hops: list[dict] = []
    if found:
        current = from_symbol
        while current != to_symbol:
            callee, path, sites = link[current]
            hops.append(
                {
                    "symbol": current,
                    "path": path,
                    "line": sorted(sites)[0],
                    "calls": callee,
                    "call_sites": len(sites),
                }
            )
            current = callee
        hops.append(
            {"symbol": to_symbol, "path": None, "line": None, "calls": None}
            | to_location
        )

    message = None
    if not found:
        reason = (
            f"stopped at the {MAX_GRAPH_EXPANSIONS}-function exploration budget"
            if explored >= MAX_GRAPH_EXPANSIONS
            else f"within {max_depth} call levels"
        )
        message = (
            f"No call path from '{from_symbol}' to '{to_symbol}' found "
            f"({reason}; {explored} function(s) explored"
            + (f", {skipped_broad} too widely referenced to expand" if skipped_broad else "")
            + "). Static analysis cannot follow function pointers, so an "
            "indirect path (ops structs, callbacks) may still exist."
        )

    if format == "json":
        result = {
            "from": from_symbol,
            "to": to_symbol,
            "path_found": found,
            "hops": hops,
            "depth": len(hops) - 1 if found else None,
            "nodes_explored": explored,
        }
        extra = {"message": message} if message else {}
        return output.envelope(
            "reachability",
            root,
            result,
            total=len(hops),
            hints=output.next_tools("reachability", found),
            **extra,
        )

    if not found:
        return message
    chain = " -> ".join(hop["symbol"] for hop in hops)
    lines = [f"reachable in {len(hops) - 1} call(s): {chain}"]
    for hop in hops[:-1]:
        plural = "s" if hop["call_sites"] != 1 else ""
        lines.append(
            f"  {hop['symbol']} calls {hop['calls']} at {hop['path']}:{hop['line']}"
            f" ({hop['call_sites']} site{plural})"
        )
    return "\n".join(lines)


@mcp.tool()
def blast_radius(
    git_ref: str = "HEAD",
    project_root: str | None = None,
    depth: int = 1,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    format: Format = "json",
) -> str:
    """Which functions are impacted by a change? (refactoring blast radius)

    Use this after editing code, or before merging, to see how far a change
    reaches: it takes the `git diff` against `git_ref`, maps every changed
    line to its enclosing function via the index, then walks the caller
    graph outward. Results are ranked by distance — the changed functions
    themselves first (distance 0), their direct callers next (distance 1),
    and so on — so the top of the list is always what to re-check first.

    Args:
        git_ref: Diff base understood by `git diff` — a commit, branch, or
            range (default HEAD = uncommitted changes; use "HEAD~1" for the
            last commit, "main..." for a whole branch).
        project_root: Project directory (must be a git work tree). Omit to
            use the server's default.
        depth: Caller levels to expand beyond the changed functions (0-3,
            default 1; 0 = just list the changed functions).
        limit: Maximum results to return (default 100).
        offset: Skip this many results (for pagination).
        format: "json" (default) for records {symbol, path, line, distance,
            via, call_sites} — `via` is the already-impacted function this
            caller reaches the change through; "text" for a ranked listing.
    """
    depth = max(0, min(depth, 3))
    if git_ref.startswith("-"):
        msg = f"Error: invalid git_ref: {git_ref!r}"
        return output.error("blast_radius", msg) if format == "json" else msg
    if err := _check_global_installed():
        return output.error("blast_radius", err) if format == "json" else err
    root, err = _effective_root(project_root)
    if err:
        return output.error("blast_radius", err) if format == "json" else err
    if err := _ensure_index(root):
        return output.error("blast_radius", err, root) if format == "json" else err
    diff_out, diff_err, code = _run(
        ["git", "diff", "--no-color", "--unified=0", git_ref, "--"], root
    )
    if code != 0:
        msg = f"Error: git diff {git_ref} failed: {diff_err.strip() or 'unknown error'}"
        return output.error("blast_radius", msg, root) if format == "json" else msg

    # Changed line ranges per (new-side) file, from the unified-diff hunks.
    changed_ranges: dict[str, list[tuple[int, int]]] = {}
    current_file: str | None = None
    for line in diff_out.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            current_file = None if target == "/dev/null" else target.removeprefix("b/")
        elif line.startswith("@@") and current_file:
            hunk = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            if hunk:
                start = int(hunk.group(1))
                count = int(hunk.group(2)) if hunk.group(2) is not None else 1
                # A pure deletion (count 0) still touches the code around
                # `start`; treat it as a one-line change there.
                changed_ranges.setdefault(current_file, []).append(
                    (start, start + max(count, 1) - 1)
                )

    # Map each changed range to the definitions whose span intersects it.
    impacted: dict[str, dict] = {}
    for path, ranges in sorted(changed_ranges.items()):
        defs_out, _, derr = _raw_global(["-fx", "--", path], project_root)
        if derr or not defs_out or not defs_out.strip():
            continue  # deleted, unindexed, or non-source file
        defs = sorted(
            (d[1], d[0])
            for line in defs_out.splitlines()
            if (d := _parse_cxref(line))
        )
        for start, end in ranges:
            for i, (def_line, def_sym) in enumerate(defs):
                next_line = defs[i + 1][0] if i + 1 < len(defs) else float("inf")
                if def_line <= end and start < next_line:
                    impacted.setdefault(
                        def_sym,
                        {
                            "symbol": def_sym,
                            "path": path,
                            "line": def_line,
                            "distance": 0,
                            "via": None,
                            "call_sites": None,
                        },
                    )

    results = sorted(impacted.values(), key=lambda r: (r["path"], r["line"]))
    seen = set(impacted)
    frontier = [r["symbol"] for r in results]
    explored = 0
    skipped_broad = 0
    capped = False
    for distance in range(1, depth + 1):
        next_frontier: list[str] = []
        for sym in frontier:
            if explored >= MAX_GRAPH_EXPANSIONS:
                capped = True
                break
            explored += 1
            callers, cerr = _callers_of(sym, project_root)
            if cerr:
                skipped_broad += 1
                continue
            for (caller, path), sites in sorted(callers.items()):
                if caller in seen or caller == "(file scope)":
                    continue
                seen.add(caller)
                results.append(
                    {
                        "symbol": caller,
                        "path": path,
                        "line": sorted(sites)[0],
                        "distance": distance,
                        "via": sym,
                        "call_sites": len(sites),
                    }
                )
                if not _MACROISH_RE.fullmatch(caller):
                    next_frontier.append(caller)
        if capped:
            break
        frontier = next_frontier

    message = None
    if not changed_ranges:
        message = f"git diff {git_ref} reports no changes."
    elif not impacted:
        message = (
            f"No indexed definitions overlap the changes against {git_ref} "
            "(non-source files, or files not in the index)."
        )

    if format == "json":
        page, total, truncated = output.paginate(results, limit, offset)
        extra = {"message": message} if message else {}
        return output.envelope(
            "blast_radius",
            root,
            page,
            total=total,
            offset=offset,
            truncated=truncated or capped,
            git_ref=git_ref,
            changed_files=len(changed_ranges),
            changed_functions=len(impacted),
            skipped_broad=skipped_broad,
            hints=output.next_tools("blast_radius", bool(results)),
            **extra,
        )

    if message:
        return message
    lines = [
        f"Blast radius of `git diff {git_ref}`: {len(impacted)} changed "
        f"function(s), {len(results)} impacted within {depth} caller level(s):"
    ]
    for rec in results[offset : offset + max(1, limit)]:
        if rec["distance"] == 0:
            lines.append(f"  [changed] {rec['symbol']}  {rec['path']}:{rec['line']}")
        else:
            plural = "s" if rec["call_sites"] != 1 else ""
            lines.append(
                f"  [d={rec['distance']}] {rec['symbol']}  {rec['path']}:{rec['line']}"
                f"  via {rec['via']} ({rec['call_sites']} site{plural})"
            )
    if skipped_broad:
        lines.append(
            f"  ({skipped_broad} function(s) too widely referenced to expand — "
            "use summarize_references on them)"
        )
    return "\n".join(lines)


def _describe_definition(rec: dict, source: str) -> str:
    """Render one enriched definition record for the symbol_info text card.

    Falls back to the raw source line when no ctags metadata matched.
    """
    kind = rec.get("kind")
    if not kind:
        return source.strip()
    name = rec["symbol"]
    typeref = rec.get("typeref")
    scope = rec.get("scope")
    signature = rec.get("signature")
    if kind in ("function", "prototype"):
        desc = f"{kind} {name}{signature or '()'}"
        return f"{desc} -> {typeref}" if typeref else desc
    if kind == "macro":
        return f"macro {name}{signature or ''}"
    if kind == "typedef":
        return f"typedef {name} = {typeref}" if typeref else f"typedef {name}"
    if kind == "member":
        desc = f"member {name}" + (f": {typeref}" if typeref else "")
        return desc + (f" ({scope})" if scope else "")
    # enumerator, struct, union, enum, class, variable, ...
    desc = f"{kind} {name}"
    if typeref:
        desc += f": {typeref}"
    if scope:
        desc += f" ({scope})"
    return desc


@mcp.tool()
def symbol_info(
    symbol: str,
    project_root: str | None = None,
    format: Format = "json",
    active_config: str | None = None,
) -> str:
    """One-shot overview card for a symbol — the best FIRST query.

    Use this before anything else when you encounter an unfamiliar symbol:
    one call returns where it's defined, WHAT it is (function/macro/struct/
    typedef/enum-constant, with signature and enclosing scope when
    available), under WHICH #ifdef guards each definition lives (kernel and
    firmware code defines symbols multiple times under different configs —
    guard_variants > 1 tells you the flat list is really a config choice),
    how widely it's used, which files use it most, and which tool to reach
    for next. Cheaper than any combination of grep and file reads.
    Macro-generated symbols (sys_*, trace_*, DEFINE_SPINLOCK/DEFINE_PER_CPU
    names) resolve to their generator invocation site, flagged by
    resolved_via; kernel symbols report their EXPORT_SYMBOL* variant in
    "exported".

    Args:
        symbol: Exact symbol name.
        project_root: Project directory. Omit to use the server's default.
        format: "json" (default) for {definitions, definition_count,
            guard_variants, resolved_via, exported, reference_count,
            file_count, top_files}; "text" for the overview card.
        active_config: Kernel .config path or comma-separated macro list
            (e.g. "CONFIG_SMP,!CONFIG_DEBUG"); definitions whose guard stack
            is definitely false under it are dropped and reported as
            config_filtered. Unknown macros never drop anything.
    """
    defs_out, root, err = _raw_global(["-x", "--", symbol], project_root)
    if err:
        return output.error("symbol_info", err, root) if format == "json" else err
    defs = [r for line in defs_out.splitlines() if (r := _parse_cxref(line))]

    resolved, resolved_via = _macro_resolve(symbol, project_root, root, not defs)
    if resolved:
        known = {(path, lineno) for _, lineno, path, _ in defs}
        defs = [h for h in resolved if (h[2], h[1]) not in known] + defs

    refs_out, _, rerr = _raw_global(["-rx", "--", symbol], project_root)
    refs = (
        [r for line in refs_out.splitlines() if (r := _parse_cxref(line))]
        if refs_out and not rerr
        else []
    )
    exported = macros.exported_via(symbol, (src for _, _, _, src in refs))
    counts: dict[str, int] = {}
    for _, _, path, _ in refs:
        counts[path] = counts.get(path, 0) + 1
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]

    if defs and refs:
        if len(counts) > 50:
            hint = "summarize_references (usage is very widespread), then find_callers on hot files"
            hint_tools = ["summarize_references", "find_callers"]
        else:
            hint = "get_symbol_body to read it; find_callers or call_hierarchy for impact"
            hint_tools = ["get_symbol_body", "find_callers", "call_hierarchy"]
    elif defs:
        hint = "get_symbol_body to read it"
        hint_tools = ["get_symbol_body"]
    else:
        hint = "find_symbol_usages to see usage sites"
        hint_tools = ["find_symbol_usages"]

    # Guard-scan EVERY definition (bounded), not just the first three: "N
    # definitions under M distinct guards" must describe the whole set, and
    # active_config must be able to drop any of them.
    all_records = [
        output.record(sym, path, lineno, source)
        for sym, lineno, path, source in defs[:MAX_GUARDED_DEFS]
    ]
    cfg = None
    if active_config:
        if not _guards_enabled(root):
            msg = (
                "Error: active_config needs guard scanning, which is disabled "
                "(--no-guards / GTAGS_MCP_GUARDS / `guards = false`)."
            )
            return output.error("symbol_info", msg, root) if format == "json" else msg
        cfg, cfg_err = guards.load_active_config(active_config, root)
        if cfg_err:
            return (
                output.error("symbol_info", cfg_err, root)
                if format == "json"
                else cfg_err
            )
    kept, config_filtered = _maybe_guard(all_records, root, cfg)
    live_defs = len(kept) + max(0, len(defs) - MAX_GUARDED_DEFS)
    distinct = {tuple(rec["guard"]) for rec in kept if rec["guard"] is not None}
    guard_variants = len(distinct) if distinct else None

    def_records = kept[:3]
    _maybe_enrich(def_records, root)

    if format == "json":
        info = {
            "symbol": symbol,
            "definitions": def_records,
            "definition_count": live_defs,
            "guard_variants": guard_variants,
            "resolved_via": resolved_via,
            "exported": exported,
            "reference_count": len(refs),
            "file_count": len(counts),
            "top_files": [{"path": path, "count": count} for path, count in top],
        }
        if active_config:
            info["config_filtered"] = config_filtered
        return output.envelope(
            "symbol_info",
            root,
            info,
            total=live_defs + len(refs),
            hints=hint_tools,
        )

    lines = [f"Symbol: {symbol}"]
    if resolved_via:
        lines.append(f"  macro-generated symbol (resolved via {resolved_via})")
    if exported:
        lines.append(f"  exported via {exported}")
    if def_records:
        if guard_variants is not None and guard_variants > 1:
            lines.append(
                f"  {live_defs} definitions under {guard_variants} distinct guards:"
            )
        for rec in def_records:
            prefix = f"[{' && '.join(rec['guard'])}] " if rec["guard"] else ""
            lines.append(
                f"  {prefix}defined at {rec['path']}:{rec['line']} — "
                f"{_describe_definition(rec, rec['snippet'])}"
            )
        if live_defs > 3:
            lines.append(f"  ... {live_defs - 3} more definition(s)")
        if config_filtered:
            lines.append(
                f"  ({config_filtered} definition(s) filtered out by active_config)"
            )
    elif defs:
        lines.append(
            f"  no definition is live under active_config "
            f"({config_filtered} filtered out)"
        )
    else:
        lines.append("  no in-tree definition (external symbol? try find_symbol_usages)")
    if refs:
        lines.append(f"  referenced {len(refs)} time(s) across {len(counts)} file(s); top files:")
        lines.extend(f"    {count:5d}  {path}" for path, count in top)
    else:
        lines.append("  no references found")
    lines.append(f"  next: {hint}")
    return "\n".join(lines)


@mcp.tool()
def project_overview(
    project_root: str | None = None,
    top: int = 15,
    format: Format = "json",
) -> str:
    """High-level map of the indexed tree: size, structure, and languages.

    Use this FIRST in an unfamiliar repository to orient yourself before
    drilling into symbols — it shows where the code mass lives without
    reading a single file.

    Args:
        project_root: Project directory. Omit to use the server's default.
        top: How many top-level directories to list (default 15).
        format: "json" (default) for {file_count, directories, file_types};
            "text" for the summary card.
    """
    stdout, root, err = _raw_global(["-P"], project_root)
    if err:
        return output.error("project_overview", err, root) if format == "json" else err
    paths = [p for p in stdout.splitlines() if p.strip()]

    dir_counts: dict[str, int] = {}
    ext_counts: dict[str, int] = {}
    for p in paths:
        clean = p[2:] if p.startswith("./") else p
        head = clean.split("/", 1)[0] if "/" in clean else "(top level)"
        dir_counts[head] = dir_counts.get(head, 0) + 1
        ext = ("." + clean.rsplit(".", 1)[1]) if "." in clean.rsplit("/", 1)[-1] else "(none)"
        ext_counts[ext] = ext_counts.get(ext, 0) + 1
    ranked = sorted(dir_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    top_exts = sorted(ext_counts.items(), key=lambda kv: -kv[1])[:8]

    if format == "json":
        overview = {
            "file_count": len(paths),
            "directories": [
                {"name": name, "files": count} for name, count in ranked[:top]
            ],
            "more_directories": max(0, len(ranked) - top),
            "file_types": [{"ext": ext, "files": count} for ext, count in top_exts],
        }
        extra = {} if paths else {"message": "The index contains no files."}
        return output.envelope(
            "project_overview", root, overview, total=len(paths), **extra
        )
    if not paths:
        return "The index contains no files."
    lines = [f"Project: {root} — {len(paths)} indexed source files"]
    lines.append("Top-level directories by file count:")
    lines.extend(f"  {count:6d}  {name}/" for name, count in ranked[:top])
    if len(ranked) > top:
        lines.append(f"  ... {len(ranked) - top} more directories")
    lines.append("File types: " + ", ".join(f"{ext} ({count})" for ext, count in top_exts))
    return "\n".join(lines)


@mcp.tool()
def find_dead_symbols(
    file_path: str,
    project_root: str | None = None,
    format: Format = "json",
) -> str:
    """List symbols defined in a file that have ZERO references anywhere.

    Use this for cleanup and refactoring tasks: instead of checking each
    function by hand, one call reports every dead-code candidate in the
    file. Caveat: entry points (main), exported APIs, and functions only
    referenced via pointers or macro tricks can be false positives — treat
    results as candidates, not verdicts.

    Args:
        file_path: Source file to audit, relative to the project root or absolute.
        project_root: Project directory. Omit to use the server's default.
        format: "json" (default) for {symbol, path, line} items; "text"
            for a summary listing.
    """
    defs_out, root, err = _raw_global(["-fx", "--", file_path], project_root)
    if err:
        return output.error("find_dead_symbols", err, root) if format == "json" else err
    defs = [r for line in defs_out.splitlines() if (r := _parse_cxref(line))]
    if not defs:
        message = f"No symbols defined in '{file_path}'."
        if format == "json":
            return output.envelope("find_dead_symbols", root, [], message=message)
        return message

    capped = len(defs) > 100
    total_defs = len(defs)
    defs = defs[:100]

    dead: list[dict] = []
    for sym, lineno, path, _ in defs:
        refs_out, _, rerr = _raw_global(["-rx", "--", sym], project_root)
        if not rerr and (not refs_out or not refs_out.strip()):
            dead.append({"symbol": sym, "path": path, "line": lineno})

    if format == "json":
        extra = {} if dead else {
            "message": f"All {len(defs)} symbols defined in '{file_path}' "
            "are referenced somewhere."
        }
        return output.envelope(
            "find_dead_symbols",
            root,
            dead,
            truncated=capped,
            checked_definitions=len(defs),
            total_definitions=total_defs,
            **extra,
        )
    note = f"\n(audit capped at the first 100 of {total_defs} definitions)" if capped else ""
    if not dead:
        return f"All {len(defs)} symbols defined in '{file_path}' are referenced somewhere." + note
    rows = "\n".join(f"  {d['symbol']}  {d['path']}:{d['line']}" for d in dead)
    return (
        f"{len(dead)} of {len(defs)} symbols defined in '{file_path}' have no references "
        "(dead-code candidates):\n" + rows + note
    )


@mcp.tool()
def find_includers(
    header_name: str,
    project_root: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    format: Format = "json",
) -> str:
    """Which files #include this header? (C/C++ include-graph impact)

    Use this when changing a header to see the blast radius: every file
    that includes it, matched by basename so both "util.h" and
    <net/tcp.h>-style paths are found.

    Args:
        header_name: Header file name, e.g. "tcp.h" or "net/tcp.h".
        project_root: Project directory. Omit to use the server's default.
        limit: Maximum result lines to return (default 100).
        offset: Skip this many result lines (for pagination).
        format: "json" (default) for structured records; "text" for raw lines.
    """
    base_name = header_name.rsplit("/", 1)[-1]
    pattern = f'#[[:space:]]*include[[:space:]]*["<]([^">]*/)?{re.escape(base_name)}[">]'
    return _query_global(
        "find_includers",
        ["-gx", "--", pattern],
        project_root,
        f"No files include '{header_name}'.",
        limit,
        offset,
        format,
        record_symbol=base_name,
    )


@mcp.tool()
def find_symbol_usages(
    symbol: str,
    project_root: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    format: Format = "json",
) -> str:
    """Find usages of symbols that have no definition inside the project.

    Use this when find_definition returns nothing — typically external or
    library identifiers (e.g. printf, malloc) and variables gtags did not
    record as definitions. Still an indexed lookup, not a scan.

    Args:
        symbol: Exact symbol name.
        project_root: Project directory. Omit to use the server's default.
        limit: Maximum result lines to return (default 100).
        offset: Skip this many result lines (for pagination).
        format: "json" (default) for structured records; "text" for raw lines.
    """
    return _query_global(
        "find_symbol_usages",
        ["-sx", "--", symbol],
        project_root,
        f"No usages found for undefined symbol '{symbol}'.",
        limit,
        offset,
        format,
    )


@mcp.tool()
def grep_project(
    pattern: str,
    project_root: str | None = None,
    case_insensitive: bool = False,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    format: Format = "json",
) -> str:
    """Regex-search all indexed source files (POSIX extended regex).

    Prefer find_definition / find_references for symbol questions — they are
    indexed and far narrower. Use this only for arbitrary text that is not a
    symbol name (comments, string literals, TODO markers). It still beats
    plain grep: it searches only files the index knows about.

    Args:
        pattern: Regex to search for, e.g. "TODO|FIXME".
        project_root: Project directory. Omit to use the server's default.
        case_insensitive: Match the pattern ignoring case.
        limit: Maximum result lines to return (default 100).
        offset: Skip this many result lines (for pagination).
        format: "json" (default) for structured records; "text" for raw lines.
    """
    flags = ["-gx"] + (["-i"] if case_insensitive else []) + ["--", pattern]
    return _query_global(
        "grep_project",
        flags,
        project_root,
        f"No matches for pattern '{pattern}'.",
        limit,
        offset,
        format,
    )


@mcp.tool()
def list_file_symbols(
    file_path: str,
    project_root: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    format: Format = "json",
) -> str:
    """List every symbol defined in one source file.

    Use this INSTEAD of reading a whole file when you only need its API
    surface — functions, structs, macros it defines — as a compact list.

    Args:
        file_path: Path to the source file, relative to the project root or absolute.
        project_root: Project directory. Omit to use the server's default.
        limit: Maximum result lines to return (default 100).
        offset: Skip this many result lines (for pagination).
        format: "json" (default) for structured records with ctags metadata
            (kind/typeref/scope/signature) and #ifdef guard stacks when
            available; "text" for raw lines.
    """
    return _query_global(
        "list_file_symbols",
        ["-fx", "--", file_path],
        project_root,
        f"No symbols found in '{file_path}' (is it inside the indexed tree?).",
        limit,
        offset,
        format,
        enrich_records=True,
        guard_records=True,
    )


@mcp.tool()
def complete_symbol(
    prefix: str,
    project_root: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    format: Format = "json",
) -> str:
    """List defined symbols that start with the given prefix.

    Use this when you know roughly what a function is called but not its
    exact name — then follow up with find_definition on the right match.

    Args:
        prefix: Symbol name prefix, e.g. "tcp_" or "init".
        project_root: Project directory. Omit to use the server's default.
        limit: Maximum result lines to return (default 100).
        offset: Skip this many result lines (for pagination).
        format: "json" (default) for {symbol} items; "text" for one name per line.
    """
    return _query_global(
        "complete_symbol",
        ["-c", "--", prefix],
        project_root,
        f"No symbols starting with '{prefix}'.",
        limit,
        offset,
        format,
        parse="symbol",
    )


@mcp.tool()
def find_files(
    pattern: str,
    project_root: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    format: Format = "json",
) -> str:
    """Find indexed source files whose path matches a regex pattern.

    Use this INSTEAD of `find` or glob scans to locate files in a large
    tree — it queries the index rather than walking the filesystem.

    Args:
        pattern: Regex matched against file paths, e.g. "net/.*\\.c$".
        project_root: Project directory. Omit to use the server's default.
        limit: Maximum result lines to return (default 100).
        offset: Skip this many result lines (for pagination).
        format: "json" (default) for {path} items; "text" for one path per line.
    """
    return _query_global(
        "find_files",
        ["-P", "--", pattern],
        project_root,
        f"No indexed files match '{pattern}'.",
        limit,
        offset,
        format,
        parse="path",
    )


@mcp.tool()
def index_project(project_root: str | None = None, format: Format = "json") -> str:
    """Force a full (re)build of the gtags index.

    Normally unnecessary — every query tool indexes automatically on first
    use and refreshes incrementally. Call this only to force a from-scratch
    rebuild (e.g. after a large branch switch or if the index seems corrupt).

    Args:
        project_root: Project directory. Omit to use the server's default.
        format: "json" (default) for {status, message}; "text" for a sentence.
    """
    def _respond(message: str, error: bool = False) -> str:
        if format != "json":
            return message
        if error:
            return output.error("index_project", message, root)
        return output.envelope(
            "index_project", root, {"status": "ok", "message": message}, total=None
        )

    root = None
    if err := _check_global_installed():
        return _respond(err, error=True)
    root, err = _effective_root(project_root)
    if err:
        return _respond(err, error=True)
    stdout, stderr, code = _run_index(root, incremental=False)
    if code != 0:
        return _respond(
            f"Error: gtags exited with code {code}: {stderr.strip() or stdout.strip()}",
            error=True,
        )
    _last_update[root] = time.monotonic()
    db_dir = _db_dir(root)
    where = str(db_dir) if db_dir != root else f"{root} (legacy root-level index)"
    label = _gtags_label(root)
    if label:
        return _respond(
            f"Indexed {root} (database in {where}) "
            f"using parser label '{label}' (multi-language)."
        )
    message = f"Indexed {root} (database in {where}) using the native parser."
    non_native = set()
    try:
        from itertools import islice

        for entry in islice(root.rglob("*"), 20000):
            suffix = entry.suffix
            if suffix in (".py", ".go", ".rs", ".js", ".ts", ".rb") and entry.is_file():
                non_native.add(suffix)
    except OSError:
        pass
    if non_native:
        message += (
            f" Note: {', '.join(sorted(non_native))} files were NOT indexed — "
            "run `mcp-gtags-server setup` (installs ctags + Pygments into user space, "
            "no sudo) to enable multi-language indexing."
        )
    return _respond(message)


@mcp.tool()
def update_index(project_root: str | None = None, format: Format = "json") -> str:
    """Synchronously refresh the index — the guaranteed-freshness barrier.

    Query tools refresh the index automatically in the BACKGROUND, so their
    results can lag very recent edits by a few seconds. Call this when you
    just edited files and need the very next query to see the changes: it
    blocks until the refresh is complete.

    Args:
        project_root: Project directory. Omit to use the server's default.
        format: "json" (default) for {status, message}; "text" for a sentence.
    """
    def _respond(message: str, error: bool = False) -> str:
        if format != "json":
            return message
        if error:
            return output.error("update_index", message, root)
        return output.envelope(
            "update_index", root, {"status": "ok", "message": message}, total=None
        )

    root = None
    if err := _check_global_installed():
        return _respond(err, error=True)
    root, err = _effective_root(project_root)
    if err:
        return _respond(err, error=True)
    if not (_db_dir(root) / "GTAGS").is_file():
        return _respond(
            f"Error: no GTAGS index found for {root}. Run index_project first.",
            error=True,
        )
    _wait_for_refresh(root)  # don't race an in-flight background refresh
    started = time.monotonic()
    _, stderr, code = _run_index(root, incremental=True)
    if code != 0:
        return _respond(
            f"Error: gtags -i exited with code {code}: {stderr.strip()}", error=True
        )
    with _refresh_lock:
        _last_update[root] = time.monotonic()
        _update_cost[root] = _last_update[root] - started
    return _respond(f"Index updated for {root} (synchronous — results are now current).")


def _package_version() -> str:
    try:
        from importlib.metadata import version

        return version("mcp-gtags-server")
    except Exception:  # noqa: BLE001 — editable/dev installs may lack metadata
        return "unknown"


def _client_config_text(transport: str, host: str, port: int) -> str:
    """Ready-to-paste MCP client configuration for this server."""
    if transport == "http":
        display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        url = f"http://{display_host}:{port}/mcp"
        lines = [
            "MCP client configuration (HTTP transport):",
            "",
            "  Claude Code (once per device, all repos):",
            f"      claude mcp add --scope user --transport http gtags {url}",
            "",
            "  Cursor / any MCP client — global settings or .mcp.json:",
            "      {",
            '        "mcpServers": {',
            f'          "gtags": {{ "url": "{url}" }}',
            "        }",
            "      }",
        ]
        if host in ("0.0.0.0", "::"):
            lines += [
                "",
                "  Listening on ALL interfaces — reachable on the network at",
                f"      http://<this-machine-ip>:{port}/mcp",
                "  WARNING: the endpoint is unauthenticated; only expose it on",
                "  networks you trust.",
            ]
        return "\n".join(lines)
    return "\n".join(
        [
            "MCP client configuration (stdio transport):",
            "",
            "  Claude Code (once per device, all repos):",
            "      claude mcp add --scope user gtags -- mcp-gtags-server",
            "",
            "  Cursor / any MCP client — global settings or .mcp.json:",
            "      {",
            '        "mcpServers": {',
            '          "gtags": { "command": "mcp-gtags-server" }',
            "        }",
            "      }",
        ]
    )


def main() -> None:
    """Entry point: serve MCP over stdio/HTTP, or run a maintenance subcommand."""
    global _default_root, _forced_label, _bin_dir, _no_enrich, _no_guards
    global _no_macro_resolve
    parser = argparse.ArgumentParser(
        prog="mcp-gtags-server",
        description=(
            "MCP server exposing GNU Global (gtags) code navigation. "
            "Subcommands: 'setup' installs the toolchain into user space "
            "(no sudo); 'doctor' reports toolchain and config status; "
            "'config' prints ready-to-paste MCP client configuration."
        ),
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["serve", "setup", "doctor", "config", "help"],
        default="serve",
        help="serve (default): run the MCP server; setup: install the "
        "gtags/ctags/Pygments toolchain into ~/.gtags-mcp without root; "
        "doctor: print what the server detects on this machine; "
        "config: print MCP client configuration for this server.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_package_version()}",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default=os.environ.get("GTAGS_MCP_TRANSPORT", "stdio"),
        help="serve/config: MCP transport. 'stdio' (default) for direct "
        "client launch; 'http' runs a background-friendly streamable-HTTP "
        "server that many clients and devices can share.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("GTAGS_MCP_HOST", "127.0.0.1"),
        help="serve/config with --transport http: bind address "
        "(default 127.0.0.1; 0.0.0.0 exposes the server on the network).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("GTAGS_MCP_PORT", "8383")),
        help="serve/config with --transport http: TCP port (default 8383).",
    )
    parser.add_argument(
        "--root",
        default=os.environ.get("GTAGS_MCP_ROOT"),
        help="Default project root for all tools (overrides GTAGS_MCP_ROOT; "
        "falls back to config files, then the current working directory).",
    )
    parser.add_argument(
        "--label",
        default=os.environ.get("GTAGS_MCP_LABEL"),
        help="GTAGSLABEL parser label to force (e.g. 'native-pygments', "
        "'pygments', 'default'). By default the server auto-selects "
        "'native-pygments' when ctags and Pygments are installed, enabling "
        "multi-language indexing.",
    )
    parser.add_argument(
        "--bin-dir",
        default=os.environ.get("GTAGS_MCP_BIN_DIR"),
        help="Extra directory searched first for the gtags/global/ctags "
        "binaries (overrides GTAGS_MCP_BIN_DIR).",
    )
    parser.add_argument(
        "--no-enrich",
        action="store_true",
        help="Disable ctags metadata enrichment — results keep null "
        "kind/typeref/scope/signature fields (also: GTAGS_MCP_ENRICH=0, "
        "or `enrich = false` in .gtags-mcp.toml).",
    )
    parser.add_argument(
        "--no-guards",
        action="store_true",
        help="Disable #ifdef guard scanning — results keep a null guard "
        "field and active_config filtering is rejected (also: "
        "GTAGS_MCP_GUARDS=0, or `guards = false` in .gtags-mcp.toml).",
    )
    parser.add_argument(
        "--no-macro-resolve",
        action="store_true",
        help="Disable macro-family symbol resolution — macro-generated "
        "symbols (sys_*, trace_*, DEFINE_* names) no longer fall back to "
        "their generator invocation site (also: GTAGS_MCP_MACRO_RESOLVE=0, "
        "or `macro_resolve = false` in .gtags-mcp.toml).",
    )
    parser.add_argument(
        "--no-ctags",
        action="store_true",
        help="setup only: skip installing universal-ctags and Pygments "
        "(disables multi-language indexing).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="setup only: reinstall even when a toolchain is already present.",
    )
    args = parser.parse_args()
    _default_root = args.root
    _forced_label = args.label
    _bin_dir = args.bin_dir
    _no_enrich = args.no_enrich
    _no_guards = args.no_guards
    _no_macro_resolve = args.no_macro_resolve
    if args.bin_dir:
        os.environ["GTAGS_MCP_BIN_DIR"] = args.bin_dir

    if args.command == "help":
        parser.print_help()
        return
    if args.command == "setup":
        sys.exit(toolchain.run_setup(with_ctags=not args.no_ctags, force=args.force))
    if args.command == "doctor":
        root, _ = _effective_root(args.root)
        print(f"mcp-gtags-server doctor (v{_package_version()})")
        print(toolchain.doctor_report(project_root=root))
        label = _gtags_label(root)
        print(f"  parser label : {label or 'native (C/C++/Java/PHP/asm only)'}")
        print(f"  {enrich.status_line(_bin_dir)}")
        if not enrich.available(_bin_dir):
            print(
                "    re-run `mcp-gtags-server setup` to install universal-ctags "
                "into user space (no sudo needed)."
            )
        guard_state = "active (built-in)" if _guards_enabled(root) else "disabled"
        print(f"  guard scanning: {guard_state}")
        if root:
            db_dir = _db_dir(root)
            built = "" if (db_dir / "GTAGS").is_file() else "  (not built yet)"
            legacy = "  (legacy root-level index)" if db_dir == root else ""
            print(f"  index location: {db_dir}{legacy}{built}")
        return
    if args.command == "config":
        print(_client_config_text(args.transport, args.host, args.port))
        return
    if args.transport == "http":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        print(
            f"mcp-gtags-server v{_package_version()} — streamable HTTP server on "
            f"{args.host}:{args.port}\n",
            flush=True,
        )
        print(_client_config_text("http", args.host, args.port), flush=True)
        try:
            mcp.run(transport="streamable-http")
        except KeyboardInterrupt:
            pass
        return
    if sys.stdin.isatty():
        print(
            "mcp-gtags-server: serving MCP over stdio — this mode is meant to be "
            "launched by an MCP client (Claude Code, Cursor, ...), not typed "
            "into.\n"
            "  Maintenance commands:  mcp-gtags-server doctor | config | setup | help\n"
            "  Shared background server:  mcp-gtags-server --transport http\n"
            "  Press Ctrl+C to exit.",
            file=sys.stderr,
            flush=True,
        )
    try:
        mcp.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
