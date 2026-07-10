"""Macro-family symbol resolution — find definitions the parser can't see.

Kernel and firmware code mints linker-visible symbols with token-pasting
macros: ``SYSCALL_DEFINE3(read, ...)`` defines ``sys_read``,
``TRACE_EVENT(sched_switch, ...)`` generates ``trace_sched_switch()``, and
``DEFINE_SPINLOCK(css_set_lock)`` defines ``css_set_lock`` — but no tagging
parser expands the preprocessor, so a direct definition lookup comes back
empty. This is the best-known gtags gap on the kernel; this module closes
it without a preprocessor, using only queries the index already answers:

- **Prefix families** — the queried name embeds a generator (``sys_read``,
  ``trace_sched_switch``): scan the indexed occurrences of the family's
  macro names (``SYSCALL_DEFINE0``..``6``, ``TRACE_EVENT``, ...) for an
  invocation whose name argument matches. Invocations are always in the
  index: as *definitions* of the macro name when they look like function
  definitions (typically ``.c`` files), as *references* or "other symbols"
  otherwise (typically ``.h`` files) — all three get scanned.
- **Bare-name definers** — the queried name is a literal macro argument
  (``DEFINE_SPINLOCK(css_set_lock)``, ``DEFINE_PER_CPU(struct rq,
  runqueues)``, ``module_param(debug, ...)``): the invocation line is
  indexed under the name itself as a reference or "other symbol", so its
  own occurrence list is scanned for a definer-shaped source line.
- **Fuzzy spellings** — as a last resort, derived spellings (leading /
  trailing underscore variants) are tried as exact definition lookups.

Everything here is pure string work over cxref tuples. The caller supplies
a ``query`` callable that runs ``global`` with the given flags and returns
parsed ``(symbol, line, path, source)`` tuples, so the module needs no
process or index state and unit-tests without GNU Global installed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable

# (symbol, line-number, path, source-line) — the shape of `global -x` output.
Cxref = tuple[str, int, str, str]
Query = Callable[[list[str]], list[Cxref]]

# One resolution never returns more than this many sites; a macro-generated
# symbol with hundreds of "definitions" means the pattern matched noise.
MAX_HITS = 100

# Kernel entry-point wrappers: __x64_sys_read and friends all lead back to
# the same SYSCALL_DEFINE site as sys_read.
_ARCH_PREFIX = r"(?:__(?:x64|ia32|arm64|riscv|s390x?|se|do)_)?"


@dataclass(frozen=True)
class _PrefixFamily:
    label: str  # reported as resolved_via "macro:<label>"
    symbol_re: re.Pattern  # full-matches the queried symbol; group "name"
    macros: tuple[str, ...]  # index symbols whose occurrences to scan
    line_template: str  # source-line regex with {name} placeholder


_PREFIX_FAMILIES = (
    _PrefixFamily(
        "COMPAT_SYSCALL_DEFINE",
        re.compile(_ARCH_PREFIX + r"compat_sys_(?P<name>\w+)"),
        tuple(f"COMPAT_SYSCALL_DEFINE{n}" for n in range(7)),
        r"\bCOMPAT_SYSCALL_DEFINE[0-6]\s*\(\s*{name}\s*[,)]",
    ),
    _PrefixFamily(
        "SYSCALL_DEFINE",
        re.compile(_ARCH_PREFIX + r"sys_(?P<name>\w+)"),
        tuple(f"SYSCALL_DEFINE{n}" for n in range(7)),
        r"\bSYSCALL_DEFINE[0-6]\s*\(\s*{name}\s*[,)]",
    ),
    _PrefixFamily(
        "TRACE_EVENT",
        re.compile(r"(?:__)?trace_(?P<name>\w+)"),
        (
            "TRACE_EVENT",
            "TRACE_EVENT_FN",
            "TRACE_EVENT_CONDITION",
            "DEFINE_EVENT",
            "DEFINE_EVENT_FN",
            "DEFINE_EVENT_PRINT",
            "DEFINE_EVENT_CONDITION",
            "DECLARE_TRACE",
            "DECLARE_TRACE_CONDITION",
        ),
        # The event name is the first argument (TRACE_EVENT, DECLARE_TRACE)
        # or the second, after a class name (DEFINE_EVENT).
        r"\b(?:TRACE_EVENT|DEFINE_EVENT|DECLARE_TRACE)\w*\s*\(\s*(?:\w+\s*,\s*)?{name}\s*[,)]",
    ),
)

# Macro invocations whose argument IS the queried symbol's definition site.
# EXPORT_SYMBOL is deliberately absent: it marks a definition elsewhere, it
# is not one (see exported_via below).
_DEFINER_RE = re.compile(
    r"\b(?:DEFINE|DECLARE)_[A-Z][A-Z0-9_]*(?=\s*\()"
    r"|\bmodule_param(?:_named|_cb|_array(?:_named)?)?(?=\s*\()"
)

_EXPORT_RE = re.compile(
    r"\bEXPORT_SYMBOL(?:_GPL|_NS(?:_GPL)?|_FOR_KUNIT)?\b(?=\s*\(\s*(?P<name>\w+)\s*[,)])"
)


def _arg_re(name: str) -> re.Pattern:
    """The name as the first or second macro argument, right after '('.

    The optional leading argument may contain spaces (``DEFINE_PER_CPU(struct
    rq, runqueues)``) but never commas or parentheses.
    """
    return re.compile(r"\(\s*(?:[^,()]*,\s*)?" + re.escape(name) + r"\s*[,)\[]")


def _dedup(hits: list[Cxref]) -> list[Cxref]:
    """Drop duplicate (path, line) sites, preserving the callers' ranking."""
    seen: set[tuple[str, int]] = set()
    out: list[Cxref] = []
    for hit in hits:
        key = (hit[2], hit[1])
        if key not in seen:
            seen.add(key)
            out.append(hit)
    return out[:MAX_HITS]


def _resolve_prefix_family(symbol: str, query: Query) -> tuple[list[Cxref], str | None]:
    for family in _PREFIX_FAMILIES:
        match = family.symbol_re.fullmatch(symbol)
        if not match:
            continue
        line_re = re.compile(
            family.line_template.format(name=re.escape(match.group("name")))
        )
        hits = [
            (symbol, lineno, path, source)
            for macro in family.macros
            for flags in (["-x"], ["-rx"], ["-sx"])
            for _, lineno, path, source in query(flags + ["--", macro])
            if line_re.search(source)
        ]
        if hits:
            return _dedup(sorted(hits, key=lambda h: (h[2], h[1]))), (
                f"macro:{family.label}"
            )
    return [], None


def _resolve_definers(symbol: str, query: Query) -> tuple[list[Cxref], str | None]:
    arg_re = _arg_re(symbol)
    definers: list[tuple[int, Cxref, str]] = []
    # Occurrences of an undefined name land in the reference DB or the
    # "other symbols" DB depending on the parser's mood; scan both.
    for flags in (["-rx"], ["-sx"]):
        for _, lineno, path, source in query(flags + ["--", symbol]):
            definer = _DEFINER_RE.search(source)
            if definer and arg_re.match(source, definer.end()):
                macro = definer.group(0)
                # DEFINE_* IS the definition; DECLARE_* is the extern-style
                # counterpart — rank real definitions first.
                rank = 1 if macro.startswith("DECLARE_") else 0
                definers.append((rank, (symbol, lineno, path, source), macro))
    if not definers:
        return [], None
    definers.sort(key=lambda d: (d[0], d[1][2], d[1][1]))
    return _dedup([hit for _, hit, _ in definers]), f"macro:{definers[0][2]}"


def fuzzy_spellings(symbol: str) -> list[str]:
    """Derived spellings worth one exact lookup each before giving up."""
    stripped = symbol.lstrip("_")
    candidates = (stripped, "_" + stripped, "__" + stripped, symbol.rstrip("_"))
    out: list[str] = []
    for cand in candidates:
        if cand and cand != symbol and cand not in out:
            out.append(cand)
    return out


def exported_via(symbol: str, sources: Iterable[str]) -> str | None:
    """The EXPORT_SYMBOL* variant that exports `symbol`, scanned from its
    reference source lines; None when it is not exported (or not kernel code)."""
    for source in sources:
        match = _EXPORT_RE.search(source)
        if match and match.group("name") == symbol:
            return match.group(0)
    return None


def resolve(
    symbol: str, query: Query, direct_empty: bool = True
) -> tuple[list[Cxref], str | None]:
    """Resolve a macro-generated symbol to its generator invocation site(s).

    Returns (cxref hits, resolved_via) — ([], None) when nothing matched.
    Prefix families always run: a ``sys_*``/``trace_*`` query deserves the
    generator site even when a same-named helper exists somewhere (tests and
    tools shadow syscall names). The bare-name definer scan and the fuzzy
    tier only run when the direct lookup was empty (`direct_empty`), so
    ordinarily-defined symbols never pay for them. For fuzzy hits the
    returned tuples carry the *found* spelling, and resolved_via is
    ``"fuzzy:<spelling>"``.
    """
    if not re.fullmatch(r"\w+", symbol):
        return [], None  # regex / qualified queries are not macro-generated
    hits, via = _resolve_prefix_family(symbol, query)
    if hits or not direct_empty:
        return hits, via
    hits, via = _resolve_definers(symbol, query)
    if hits:
        return hits, via
    for spelling in fuzzy_spellings(symbol):
        found = query(["-x", "--", spelling])
        if found:
            return _dedup(found), f"fuzzy:{spelling}"
    return [], None
