"""Tool output: one JSON envelope per call, and ONE compact text rendering of it.

Every tool body builds a JSON envelope::

    {
      "tool": "find_definition",
      "root": "/abs/project/root",
      "results": [ ... tool-shaped items ... ],
      "total": 12, "offset": 0, "truncated": false,
      "next_tools": ["get_symbol_body", "find_callers"],
      "warning": null
    }

Agents get :func:`render_text` of that envelope by default — grep-shaped
``path:line: source`` rows with inline tags, a one-line summary where one
helps, and a pagination/next-tools footer — and the envelope itself with
``format="json"``. Rendering from the envelope (never from a separate code
path) keeps the two formats equivalent.

Symbol-location items use one stable record schema everywhere::

    { "symbol", "path", "line", "col",
      "kind", "typeref", "scope", "signature", "guard", "snippet" }

``kind`` / ``typeref`` / ``scope`` / ``signature`` are ctags metadata
(populated on definition-shaped results when Universal Ctags with JSON
output can parse the file — see :mod:`gtags_mcp.enrich` — and ``null``
otherwise). ``guard`` is the enclosing ``#if``/``#ifdef`` conditional stack
as a list of strings, outermost first (see :mod:`gtags_mcp.guards`):
``[]`` means the file was scanned and the symbol is unconditional; ``null``
means guard scanning was disabled or the file could not be read. JSON keys
are only ever *added*, never renamed or removed, within a major version.
Paths are repo-relative. Errors replace ``results`` with an ``error`` string
but keep the envelope and ``next_tools``.

Definition-shaped envelopes (find_definition, get_symbol_body, find_callees)
gain a ``resolved_via`` field when some results were found through a
resolution tier rather than a literal index match: ``"macro:SYSCALL_DEFINE"``
/ ``"fuzzy:vfs_read"`` for macro-family resolution (see
:mod:`gtags_mcp.macros`), ``"ctags:EXPORT_SYMBOL"`` for definitions the index
parser missed, recovered from their EXPORT_SYMBOL* site via Universal Ctags.
"""

from __future__ import annotations

import json

MAX_SNIPPET_CHARS = 200

# Suggested follow-up tools per tool: (when results were found, when empty).
_NEXT_TOOLS: dict[str, tuple[list[str], list[str]]] = {
    "find_definition": (
        ["get_symbol_body", "find_callers", "find_references"],
        ["find_references"],
    ),
    "find_references": (
        ["find_callers", "find_definition"],
        ["find_definition"],
    ),
    "list_file_symbols": (["get_symbol_body", "find_references"], []),
    "get_symbol_body": (
        ["find_callees", "find_callers"],
        ["find_references"],
    ),
    "find_callers": (
        ["get_symbol_body", "find_callees"],
        ["find_references"],
    ),
    "find_callees": (["get_symbol_body", "find_callers"], []),
    "reachability": (
        ["get_symbol_body", "find_callers"],
        ["find_callers", "find_callees"],
    ),
    "update_index": (["find_definition"], []),
}


def next_tools(tool: str, has_results: bool) -> list[str]:
    on_hit, on_empty = _NEXT_TOOLS.get(tool, ([], []))
    return list(on_hit if has_results else on_empty)


def record(
    symbol: str,
    path: str,
    line: int,
    snippet: str,
    *,
    kind: str | None = None,
    typeref: str | None = None,
    scope: str | None = None,
    signature: str | None = None,
    guard: list[str] | None = None,
) -> dict:
    """One symbol-location result in the stable record schema."""
    snippet = snippet.rstrip()
    if len(snippet) > MAX_SNIPPET_CHARS:
        snippet = snippet[:MAX_SNIPPET_CHARS] + " ..."
    idx = snippet.find(symbol) if symbol else -1
    return {
        "symbol": symbol,
        "path": path[2:] if path.startswith("./") else path,
        "line": line,
        "col": idx + 1 if idx >= 0 else None,
        "kind": kind,
        "typeref": typeref,
        "scope": scope,
        "signature": signature,
        "guard": guard,
        "snippet": snippet,
    }


def paginate(items: list, limit: int, offset: int) -> tuple[list, int, bool]:
    """Slice items like the text footer does. Returns (page, total, truncated)."""
    total = len(items)
    limit = max(1, limit)
    offset = max(0, offset)
    page = items[offset : offset + limit]
    return page, total, offset > 0 or offset + len(page) < total


def envelope(
    tool: str,
    root,
    results,
    *,
    total: int | None = None,
    offset: int = 0,
    truncated: bool = False,
    hints: list[str] | None = None,
    warning: str | None = None,
    **extra,
) -> str:
    obj: dict = {"tool": tool, "root": str(root) if root else None, "results": results}
    obj["total"] = total if total is not None else (
        len(results) if isinstance(results, list) else None
    )
    obj["offset"] = offset
    obj["truncated"] = truncated
    obj.update(extra)
    obj["next_tools"] = hints if hints is not None else next_tools(tool, bool(results))
    obj["warning"] = warning
    return json.dumps(obj, ensure_ascii=False)


def error(tool: str, message: str, root=None, hints: list[str] | None = None) -> str:
    obj = {
        "tool": tool,
        "root": str(root) if root else None,
        "error": message,
        "next_tools": hints if hints is not None else next_tools(tool, False),
    }
    return json.dumps(obj, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Compact text rendering (the default tool output)
# ---------------------------------------------------------------------------

_MAX_TOP_FILES_SHOWN = 3
_MAX_EXTRA_SITES_SHOWN = 5


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _guard_tag(guard: list[str] | None) -> str | None:
    if not guard:
        return None
    return "#if " + " && ".join(f"({g})" if " " in g else g for g in guard)


def _tags(rec: dict, *, kind: bool = True) -> str:
    parts = [rec["kind"]] if kind and rec.get("kind") else []
    if guard := _guard_tag(rec.get("guard")):
        parts.append(guard)
    return f"  [{'; '.join(parts)}]" if parts else ""


def _location_row(rec: dict, *, kind: bool = True) -> str:
    return f"{rec['path']}:{rec['line']}: {rec.get('snippet', '')}{_tags(rec, kind=kind)}"


def _definition_header(data: dict) -> str | None:
    if "reference_count" not in data or not (data["results"] or data["reference_count"]):
        return None
    count = data.get("definition_count", data.get("total") or 0)
    head = f"{data.get('symbol')}: {_plural(count, 'definition')}"
    if (variants := data.get("guard_variants")) and variants > 1:
        head += f" under {variants} #if variants"
    parts = [head]
    refs = f"{_plural(data['reference_count'], 'ref')} in {_plural(data.get('file_count', 0), 'file')}"
    if top := data.get("top_files"):
        shown = ", ".join(f"{t['path']} {t['count']}" for t in top[:_MAX_TOP_FILES_SHOWN])
        refs += f" (top: {shown})"
    parts.append(refs)
    if exported := data.get("exported"):
        parts.append(f"exported via {exported}")
    return " · ".join(parts)


def _render_find_definition(data: dict) -> list[str]:
    lines = []
    if header := _definition_header(data):
        lines.append(header)
    lines.extend(_location_row(rec) for rec in data["results"])
    return lines


def _render_find_references(data: dict) -> list[str]:
    symbol = data.get("symbol", "symbol")
    where = f" under {data['path_prefix']}/" if data.get("path_prefix") else ""
    if data.get("grouped_by") == "file":
        if not data["results"]:
            return []
        return [
            f"{symbol}: {_plural(data.get('total_references', 0), 'reference')} in "
            f"{_plural(data.get('total') or 0, 'file')}{where}, grouped by file "
            "(pass path_prefix=<dir> to list the sites, or group_by='line')"
        ] + [f"{item['count']:>6}  {item['path']}" for item in data["results"]]
    lines = []
    if data["results"]:
        lines.append(f"{symbol}: {_plural(data.get('total') or 0, 'reference')}{where}")
    if data.get("fallback") == "symbol_usages":
        lines.append("(no indexed references — showing symbol usages)")
    lines.extend(_location_row(rec, kind=False) for rec in data["results"])
    return lines


def _render_list_file_symbols(data: dict) -> list[str]:
    results = data["results"]
    if not results:
        return []
    lines = [f"{results[0]['path']}: {_plural(data.get('total') or 0, 'symbol')}"]
    for rec in results:
        guard = _guard_tag(rec.get("guard"))
        tag = f"  [{guard}]" if guard else ""
        if rec.get("kind"):
            lines.append(
                f"{rec['line']}: {rec['kind']} {rec['symbol']}{rec.get('signature') or ''}{tag}"
            )
        else:
            lines.append(f"{rec['line']}: {rec.get('snippet', '')}{tag}")
    return lines


def _render_get_symbol_body(data: dict) -> list[str]:
    lines = []
    for item in data["results"]:
        lines.append(f"== {item['path']}:{item['line']} ==")
        lines.append(item["body"])
    if omitted := data.get("omitted_definitions"):
        lines.append(f"({omitted} more definition(s) not shown; find_definition lists them all)")
    return lines


def _render_find_callers(data: dict) -> list[str]:
    results = data["results"]
    if not results:
        return []
    lines = [
        f"{data.get('symbol', 'symbol')}: {_plural(data.get('total') or 0, 'calling function')}"
    ]
    for item in results:
        sites = item["sites"]
        row = f"{item['caller']}  {item['path']}:{sites[0]}: {item.get('call', '')}"
        if len(sites) > 1:
            more = ", ".join(str(n) for n in sites[1 : 1 + _MAX_EXTRA_SITES_SHOWN])
            if len(sites) > 1 + _MAX_EXTRA_SITES_SHOWN:
                more += ", ..."
            row += f"  (+{len(sites) - 1} more: {more})"
        lines.append(row)
    return lines


def _render_find_callees(data: dict) -> list[str]:
    results = data["results"]
    definition = data.get("definition")
    if not definition:
        return []
    lines = [f"{data.get('symbol', 'symbol')} ({definition['path']}:{definition['line']}) calls:"]
    lines.extend(f"  {c['symbol']}  {c['path']}:{c['line']}" for c in results["in_tree"])
    if results["external"]:
        lines.append(f"  external/unresolved: {', '.join(results['external'])}")
    if capped := data.get("capped_call_targets"):
        lines.append(f"  (analysis capped; {capped} more call target(s) not checked)")
    return lines


def _render_reachability(data: dict) -> list[str]:
    result = data["results"]
    if not result.get("path_found"):
        return []
    hops = result["hops"]
    lines = [f"{result['from']} reaches {result['to']} in {_plural(result['depth'], 'call')}:"]
    for hop in hops[:-1]:
        lines.append(f"  {hop['symbol']} ({hop['path']}:{hop['line']}) calls {hop['calls']}")
    last = hops[-1]
    where = f" ({last['path']}:{last['line']})" if last.get("path") else ""
    lines.append(f"  {last['symbol']}{where}")
    return lines


def _render_update_index(data: dict) -> list[str]:
    return [data["results"]["message"]]


_RENDERERS = {
    "find_definition": _render_find_definition,
    "find_references": _render_find_references,
    "list_file_symbols": _render_list_file_symbols,
    "get_symbol_body": _render_get_symbol_body,
    "find_callers": _render_find_callers,
    "find_callees": _render_find_callees,
    "reachability": _render_reachability,
    "update_index": _render_update_index,
}


def _footer(data: dict) -> str | None:
    results = data.get("results")
    total = data.get("total")
    if not isinstance(results, list) or not isinstance(total, int):
        return None
    offset = data.get("offset") or 0
    shown = len(results)
    if shown == 0:
        return f"[offset {offset} is past the last of {total}]" if total and offset else None
    if not data.get("truncated") and offset == 0:
        return None
    end = offset + shown
    footer = f"[{offset + 1}-{end} of {total}"
    if end < total:
        footer += f" · offset={end} for more"
    return footer + "]"


def render_text(data: dict) -> str:
    """Compact, grep-shaped text for one tool envelope (the default output)."""
    lines: list[str] = []
    if error := data.get("error"):
        lines.append(error)
    else:
        renderer = _RENDERERS.get(data.get("tool", ""))
        if renderer is None:  # unknown shape: never lose information
            return json.dumps(data, ensure_ascii=False)
        lines.extend(renderer(data))
        if via := data.get("resolved_via"):
            lines.append(f"(resolved via {via})")
        if filtered := data.get("config_filtered"):
            lines.append(f"({filtered} filtered out by active_config)")
        if message := data.get("message"):
            lines.append(message)
        if suggestions := data.get("suggestions"):
            lines.append("similar symbols: " + ", ".join(suggestions))
        if footer := _footer(data):
            lines.append(footer)
    if warning := data.get("warning"):
        lines.append(warning)
    if hints := data.get("next_tools"):
        lines.append("next: " + ", ".join(hints))
    return "\n".join(lines)
