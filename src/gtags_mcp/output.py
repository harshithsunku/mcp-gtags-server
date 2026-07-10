"""Structured (JSON) output shared by every MCP tool.

Each tool response is one JSON object with a stable envelope::

    {
      "tool": "find_definition",
      "root": "/abs/project/root",
      "results": [ ... tool-shaped items ... ],
      "total": 12, "offset": 0, "truncated": false,
      "next_tools": ["get_symbol_body", "find_callers"],
      "warning": null
    }

Symbol-location items use one stable record schema everywhere::

    { "symbol", "path", "line", "col",
      "kind", "typeref", "scope", "signature", "guard", "snippet" }

``kind`` / ``typeref`` / ``scope`` / ``signature`` are ctags metadata
(populated on definition-shaped results when Universal Ctags with JSON
output can parse the file — see :mod:`gtags_mcp.enrich` — and ``null``
otherwise). ``guard`` is the enclosing ``#if``/``#ifdef`` conditional stack
as a list of strings, outermost first (see :mod:`gtags_mcp.guards`):
``[]`` means the file was scanned and the symbol is unconditional; ``null``
means guard scanning was disabled or the file could not be read. Keys are
only ever *added*, never renamed or removed, so agent-side parsers keep
working. Paths are repo-relative. Errors replace ``results`` with an
``error`` string but keep the envelope and ``next_tools``.

Definition-shaped envelopes gain a ``resolved_via`` field (e.g.
``"macro:SYSCALL_DEFINE"``, ``"fuzzy:vfs_read"``) when some results were
found through macro-family resolution rather than a literal index match —
see :mod:`gtags_mcp.macros`.
"""

from __future__ import annotations

import json

MAX_SNIPPET_CHARS = 200

# Suggested follow-up tools per tool: (when results were found, when empty).
_NEXT_TOOLS: dict[str, tuple[list[str], list[str]]] = {
    "find_definition": (
        ["get_symbol_body", "find_callers", "symbol_info"],
        ["find_symbol_usages", "complete_symbol"],
    ),
    "find_references": (
        ["find_callers", "summarize_references"],
        ["find_symbol_usages", "find_definition"],
    ),
    "find_symbol_usages": (["grep_project"], ["complete_symbol", "grep_project"]),
    "grep_project": (["find_definition", "list_file_symbols"], ["find_files"]),
    "list_file_symbols": (["get_symbol_body", "find_dead_symbols"], ["find_files"]),
    "complete_symbol": (["find_definition", "symbol_info"], ["grep_project"]),
    "find_files": (["list_file_symbols"], ["project_overview"]),
    "get_symbol_body": (
        ["find_callees", "find_callers"],
        ["find_symbol_usages", "complete_symbol"],
    ),
    "find_callers": (
        ["call_hierarchy", "get_symbol_body"],
        ["find_references", "find_symbol_usages"],
    ),
    "summarize_references": (
        ["find_callers", "find_references"],
        ["find_symbol_usages"],
    ),
    "call_hierarchy": (["get_symbol_body", "find_callees"], ["find_references"]),
    "find_callees": (["get_symbol_body", "call_hierarchy"], []),
    "symbol_info": ([], []),  # computed dynamically by the tool
    "project_overview": (["find_files", "symbol_info"], []),
    "find_dead_symbols": (["find_references", "get_symbol_body"], []),
    "find_includers": (["list_file_symbols"], ["find_files"]),
    "index_project": (["project_overview", "symbol_info"], []),
    "update_index": (["symbol_info"], []),
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
