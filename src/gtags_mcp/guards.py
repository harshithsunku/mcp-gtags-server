"""``#ifdef`` / config-guard awareness (roadmap milestone 3).

Kernel and firmware C code defines the same symbol multiple times under
``#if`` / ``#ifdef`` conditions, and which definition is compiled depends on
the build configuration. This module makes that visible with no build and no
preprocessor:

- a per-file directive scanner that maps any line to its enclosing guard
  stack (the ``guard`` record field),
- a kernel ``.config`` / macro-list parser, and
- a tri-state (true / false / unknown) expression evaluator so results can be
  filtered down to the definitions that are actually live under a config —
  conservatively: only *definitely false* guards are ever filtered out.

Everything is best-effort and pure Python: broken or truncated files never
raise, oversized files are skipped, and results are cached per file exactly
like :mod:`gtags_mcp.enrich` (stat-validated LRU with negative caching).

Known limitation: directives are recognized when ``#`` starts a physical
line; a directive-shaped string spanning backslash-continued lines could in
principle confuse the scanner. Classic include guards (``#ifndef X`` /
``#define X`` wrapping a whole header) are detected and suppressed — they
are file-identity plumbing, not configuration.
"""

from __future__ import annotations

import re
import threading
from bisect import bisect_right
from collections import OrderedDict
from pathlib import Path
from typing import Iterator, NamedTuple

MAX_FILE_BYTES = 8 * 1024 * 1024
# Guard tables are a few KB each, and active_config filtering scans EVERY
# file a hot symbol touches (1500+ for kernel symbols like kmalloc) — a
# small cache would thrash end to end on every query.
CACHE_CAPACITY = 4096


class Frame(NamedTuple):
    """One open conditional region: the branch taken and what it excludes."""

    cond: str | None  # normalized expr of this branch; None for #else
    negations: tuple[str, ...]  # exprs of earlier branches in the chain
    display: str  # precomputed human/JSON string


class FileGuards:
    """Immutable line -> guard-stack lookup for one scanned file."""

    __slots__ = ("_lines", "_stacks")

    def __init__(self, lines: list[int], stacks: list[tuple[Frame, ...]]):
        self._lines = lines
        self._stacks = stacks

    def stack_at(self, line: int) -> tuple[Frame, ...]:
        idx = bisect_right(self._lines, line) - 1
        return self._stacks[idx] if idx >= 0 else ()


def guard_list(stack: tuple[Frame, ...]) -> list[str]:
    """Display strings for the record ``guard`` field, outermost first."""
    return [frame.display for frame in stack]


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

_COND_RE = re.compile(r"^\s*#\s*(if|ifdef|ifndef|elif|else|endif)\b(.*)$")
_ANY_DIRECTIVE_RE = re.compile(r"^\s*#\s*(\w+)\b(.*)$")
_NAME_RE = re.compile(r"[A-Za-z_]\w*")
_DEFINED_RE = re.compile(r"^defined\s*\(?\s*([A-Za-z_]\w*)\s*\)?$")
_NOT_DEFINED_RE = re.compile(r"^!\s*defined\s*\(?\s*([A-Za-z_]\w*)\s*\)?$")


def _clean_lines(text: str) -> list[str]:
    """Blank out comments (and string contents' comment lookalikes are kept
    inside their quotes) while preserving line numbering."""
    out: list[str] = []
    in_block = False
    for line in text.splitlines():
        if not in_block and '"' not in line and "/" not in line:
            out.append(line)  # fast path: nothing to strip
            continue
        if in_block and "*/" not in line:
            out.append("")
            continue
        buf: list[str] = []
        in_str = False  # strings do not span physical lines in valid C
        i, n = 0, len(line)
        while i < n:
            ch = line[i]
            if in_block:
                if ch == "*" and line.startswith("*/", i):
                    in_block = False
                    i += 2
                else:
                    i += 1
                continue
            if in_str:
                buf.append(ch)
                if ch == "\\" and i + 1 < n:
                    buf.append(line[i + 1])
                    i += 2
                    continue
                if ch == '"':
                    in_str = False
                i += 1
                continue
            if ch == '"':
                in_str = True
                buf.append(ch)
                i += 1
                continue
            if ch == "/" and line.startswith("//", i):
                break
            if ch == "/" and line.startswith("/*", i):
                in_block = True
                i += 2
                continue
            buf.append(ch)
            i += 1
        out.append("".join(buf))
    return out


class _Directive(NamedTuple):
    start: int  # 1-based first physical line
    end: int  # 1-based last physical line (after continuations)
    keyword: str
    body: str  # whitespace-collapsed, continuations joined


def _directives(cleaned: list[str]) -> Iterator[_Directive]:
    """Every preprocessor directive, with backslash continuations joined."""
    i, n = 0, len(cleaned)
    while i < n:
        match = _ANY_DIRECTIVE_RE.match(cleaned[i])
        if not match:
            i += 1
            continue
        start = i
        body = match.group(2)
        while body.rstrip().endswith("\\") and i + 1 < n:
            body = body.rstrip()[:-1] + " " + cleaned[i + 1]
            i += 1
        yield _Directive(start + 1, i + 1, match.group(1), " ".join(body.split()))
        i += 1


def _render(expr: str) -> str:
    """Human form of one condition: defined(X) -> X, !defined(X) -> !X."""
    if match := _DEFINED_RE.match(expr):
        return match.group(1)
    if match := _NOT_DEFINED_RE.match(expr):
        return "!" + match.group(1)
    return expr


def _negate(expr: str) -> str:
    rendered = _render(expr)
    if re.fullmatch(r"[A-Za-z_]\w*", rendered):
        return "!" + rendered
    if re.fullmatch(r"![A-Za-z_]\w*", rendered):
        return rendered[1:]
    return f"!({rendered})"


def _frame_display(cond: str | None, negations: tuple[str, ...]) -> str:
    parts = [_negate(neg) for neg in negations]
    if cond is not None:
        parts.append(_render(cond))
    return " && ".join(parts) if parts else "1"


def _normalize_cond(keyword: str, body: str) -> str:
    if keyword in ("ifdef", "ifndef"):
        name = _NAME_RE.match(body.strip())
        macro = name.group(0) if name else "0"
        return f"defined({macro})" if keyword == "ifdef" else f"!defined({macro})"
    return body or "0"


def _include_guard_span(
    cleaned: list[str], directives: list[_Directive]
) -> tuple[int, int] | None:
    """(open_idx, close_idx) of a whole-file include guard, or None.

    Confirmed only at EOF: the first directive is ``#ifndef M`` (or
    ``#if !defined(M)``) with no non-blank content before it, the next
    directive is ``#define M``, its matching ``#endif`` is the last
    directive, and nothing but blank/comment content follows.
    """
    if len(directives) < 3:
        return None
    first = directives[0]
    if first.keyword == "ifndef":
        name = _NAME_RE.match(first.body.strip())
        macro = name.group(0) if name else None
    elif first.keyword == "if" and (match := _NOT_DEFINED_RE.match(first.body)):
        macro = match.group(1)
    else:
        return None
    if macro is None:
        return None
    if any(cleaned[i].strip() for i in range(first.start - 1)):
        return None  # real content precedes the candidate
    second = directives[1]
    if second.keyword != "define" or not second.body.startswith(macro):
        return None
    after_define = second.body[len(macro) :]
    if after_define and not after_define[0].isspace() and after_define[0] != "(":
        return None  # e.g. #define FOO_HX — different macro
    # Find the #endif that closes the candidate frame.
    depth = 0
    close_idx = None
    for idx, directive in enumerate(directives):
        if directive.keyword in ("if", "ifdef", "ifndef"):
            depth += 1
        elif directive.keyword == "endif":
            depth -= 1
            if depth == 0:
                close_idx = idx
                break
    if close_idx is None or close_idx != len(directives) - 1:
        return None  # unclosed, or more directives follow the guard
    closing = directives[close_idx]
    if any(line.strip() for line in cleaned[closing.end :]):
        return None  # real content after the final #endif
    return 0, close_idx


def scan_text(text: str) -> FileGuards:
    """Build the line -> guard-stack table for one file. Never raises."""
    cleaned = _clean_lines(text)
    directives = list(_directives(cleaned))
    guard_span = _include_guard_span(cleaned, directives)
    skip = set(guard_span) if guard_span else set()

    lines = [1]
    stacks: list[tuple[Frame, ...]] = [()]
    stack: list[Frame] = []
    for idx, directive in enumerate(directives):
        if idx in skip:
            continue
        keyword = directive.keyword
        if keyword in ("if", "ifdef", "ifndef"):
            cond = _normalize_cond(keyword, directive.body)
            stack.append(Frame(cond, (), _frame_display(cond, ())))
        elif keyword == "elif" and stack:
            prev = stack.pop()
            negations = prev.negations + (
                (prev.cond,) if prev.cond is not None else ()
            )
            cond = directive.body or "0"
            stack.append(Frame(cond, negations, _frame_display(cond, negations)))
        elif keyword == "else" and stack:
            prev = stack.pop()
            negations = prev.negations + (
                (prev.cond,) if prev.cond is not None else ()
            )
            stack.append(Frame(None, negations, _frame_display(None, negations)))
        elif keyword == "endif" and stack:
            stack.pop()
        else:
            continue  # define/include/pragma/..., or unbalanced elif/else/endif
        lines.append(directive.end + 1)
        stacks.append(tuple(stack))
    return FileGuards(lines, stacks)


# ---------------------------------------------------------------------------
# Per-file cache (same discipline as enrich.py's tags cache)
# ---------------------------------------------------------------------------

# LRU: absolute path -> ((mtime_ns, size), FileGuards | None). None caches
# unreadable/oversized files so they are not re-tried on every query.
_guards_cache: OrderedDict[str, tuple[tuple[int, int], FileGuards | None]] = (
    OrderedDict()
)
_config_cache: dict[str, tuple[tuple[int, int], "ActiveConfig"]] = {}
_lock = threading.Lock()


def reset_cache() -> None:
    """Forget cached guard tables and parsed configs (used by tests)."""
    with _lock:
        _guards_cache.clear()
        _config_cache.clear()


def guards_for_file(abs_path: Path) -> FileGuards | None:
    """Guard table for one file. Cached; never raises; None = unavailable."""
    try:
        stat = abs_path.stat()
    except OSError:
        return None
    signature = (stat.st_mtime_ns, stat.st_size)
    key = str(abs_path)
    with _lock:
        cached = _guards_cache.get(key)
        if cached and cached[0] == signature:
            _guards_cache.move_to_end(key)
            return cached[1]
    result: FileGuards | None = None
    if stat.st_size <= MAX_FILE_BYTES:
        try:
            # The scan runs outside the lock; a rare duplicate scan on a
            # concurrent query is cheaper than serializing every lookup.
            result = scan_text(abs_path.read_text(errors="replace"))
        except OSError:
            result = None
    with _lock:
        _guards_cache[key] = (signature, result)
        _guards_cache.move_to_end(key)
        while len(_guards_cache) > CACHE_CAPACITY:
            _guards_cache.popitem(last=False)
    return result


# ---------------------------------------------------------------------------
# active_config: .config / macro-list parsing
# ---------------------------------------------------------------------------


class ActiveConfig(NamedTuple):
    defined: dict[str, int | str]  # macro -> value (1 for =y / bare)
    absent: frozenset[str]  # macros explicitly known to be undefined
    closed: bool  # closed world: unknown CONFIG_* are undefined


def parse_dot_config(text: str) -> ActiveConfig:
    """Parse kernel ``.config`` content with kbuild autoconf semantics.

    ``CONFIG_X=y`` defines ``CONFIG_X``; ``CONFIG_X=m`` defines
    ``CONFIG_X_MODULE`` (NOT ``CONFIG_X`` — that is how autoconf.h works, and
    what makes ``IS_ENABLED``/``IS_MODULE`` come out right). A ``.config`` is
    a closed world: any ``CONFIG_*`` it does not set is known-undefined.
    """
    defined: dict[str, int | str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not re.fullmatch(r"CONFIG_\w+", name):
            continue
        if value == "y":
            defined[name] = 1
        elif value == "m":
            defined[name + "_MODULE"] = 1
        elif value == "n":
            continue
        elif value.startswith('"'):
            defined[name] = value.strip('"')
        else:
            try:
                defined[name] = int(value, 0)
            except ValueError:
                defined[name] = value
    return ActiveConfig(defined, frozenset(), True)


def parse_macro_list(spec: str) -> ActiveConfig:
    """Parse ``"CONFIG_SMP, BITS_PER_LONG=64, !CONFIG_DEBUG"`` (open world)."""
    defined: dict[str, int | str] = {}
    absent: set[str] = set()
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        if item.startswith("!"):
            absent.add(item[1:].strip())
            continue
        name, sep, value = item.partition("=")
        name = name.strip()
        if not sep:
            defined[name] = 1
            continue
        value = value.strip()
        try:
            defined[name] = int(value, 0)
        except ValueError:
            defined[name] = value.strip('"')
    return ActiveConfig(defined, frozenset(absent), False)


def load_active_config(
    spec: str, root: Path | None
) -> tuple[ActiveConfig | None, str | None]:
    """Resolve an ``active_config`` tool argument. Returns (config, error).

    A path to an existing file (absolute, or relative to the project root) is
    parsed as a kernel ``.config``; anything path-looking that does not exist
    is an error (explicit intent must not fail silently); everything else is
    treated as a comma-separated macro list.
    """
    candidate = Path(spec).expanduser()
    if not candidate.is_absolute() and root is not None:
        candidate = root / spec
    if candidate.is_file():
        try:
            stat = candidate.stat()
        except OSError as exc:
            return None, f"Error: cannot read active_config file {spec}: {exc}"
        signature = (stat.st_mtime_ns, stat.st_size)
        key = str(candidate)
        with _lock:
            cached = _config_cache.get(key)
            if cached and cached[0] == signature:
                return cached[1], None
        try:
            config = parse_dot_config(candidate.read_text(errors="replace"))
        except OSError as exc:
            return None, f"Error: cannot read active_config file {spec}: {exc}"
        with _lock:
            _config_cache[key] = (signature, config)
        return config, None
    if "/" in spec or spec.endswith(".config"):
        return None, f"Error: active_config file not found: {spec}"
    return parse_macro_list(spec), None


# ---------------------------------------------------------------------------
# Tri-state expression evaluator (true / false / unknown=None)
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"\s*(?:(?P<num>0[xX][0-9a-fA-F]+|\d+)[uUlL]*"
    r"|(?P<name>[A-Za-z_]\w*)"
    r"|(?P<op>&&|\|\||==|!=|<=|>=|<<|>>|[!<>()+\-*/%&|^~]))"
)

_IS_MACROS = ("IS_ENABLED", "IS_BUILTIN", "IS_MODULE", "IS_REACHABLE")


class _Unparseable(Exception):
    pass


def _tokenize(expr: str) -> list[str | int]:
    tokens: list[str | int] = []
    pos = 0
    while pos < len(expr):
        match = _TOKEN_RE.match(expr, pos)
        if not match:
            if expr[pos:].strip():
                raise _Unparseable(expr)
            break
        if match.group("num") is not None:
            tokens.append(int(match.group("num"), 0))
        else:
            tokens.append(match.group("name") or match.group("op"))
        pos = match.end()
    return tokens


class _Parser:
    """Recursive-descent C preprocessor-expression evaluator over tokens.

    Values are ``int | None``; ``None`` means unknown and propagates through
    arithmetic. Logical ``&&``/``||``/``!`` use Kleene three-valued logic so
    e.g. ``0 && UNKNOWN`` is still definitely false.
    """

    def __init__(self, tokens: list[str | int], cfg: ActiveConfig):
        self.tokens = tokens
        self.pos = 0
        self.cfg = cfg

    def peek(self) -> str | int | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self) -> str | int:
        token = self.peek()
        if token is None:
            raise _Unparseable("unexpected end")
        self.pos += 1
        return token

    def expect(self, op: str) -> None:
        if self.take() != op:
            raise _Unparseable(f"expected {op}")

    # bool | None helpers -------------------------------------------------
    @staticmethod
    def _truthy(value: int | None) -> bool | None:
        return None if value is None else bool(value)

    def parse(self) -> bool | None:
        value = self.or_expr()
        if self.peek() is not None:
            raise _Unparseable("trailing tokens")
        return self._truthy(value)

    def or_expr(self) -> int | None:
        # The raw value passes through untouched unless a logical operator
        # appears — `(FOO + 3)` must stay arithmetic inside a larger expr.
        left = self.and_expr()
        while self.peek() == "||":
            lbool = self._truthy(left)
            self.take()
            rbool = self._truthy(self.and_expr())
            if lbool is True or rbool is True:
                left = 1
            elif lbool is False and rbool is False:
                left = 0
            else:
                left = None
        return left

    def and_expr(self) -> int | None:
        left = self.bitor()
        while self.peek() == "&&":
            lbool = self._truthy(left)
            self.take()
            rbool = self._truthy(self.bitor())
            if lbool is False or rbool is False:
                left = 0
            elif lbool is True and rbool is True:
                left = 1
            else:
                left = None
        return left

    def _binary(self, sub, ops: dict) -> int | None:
        left = sub()
        while self.peek() in ops:
            op = self.take()
            right = sub()
            left = None if left is None or right is None else ops[op](left, right)
        return left

    def bitor(self) -> int | None:
        return self._binary(self.bitxor, {"|": lambda a, b: a | b})

    def bitxor(self) -> int | None:
        return self._binary(self.bitand, {"^": lambda a, b: a ^ b})

    def bitand(self) -> int | None:
        return self._binary(self.equality, {"&": lambda a, b: a & b})

    def equality(self) -> int | None:
        return self._binary(
            self.relational,
            {"==": lambda a, b: int(a == b), "!=": lambda a, b: int(a != b)},
        )

    def relational(self) -> int | None:
        return self._binary(
            self.shift,
            {
                "<": lambda a, b: int(a < b),
                "<=": lambda a, b: int(a <= b),
                ">": lambda a, b: int(a > b),
                ">=": lambda a, b: int(a >= b),
            },
        )

    def shift(self) -> int | None:
        return self._binary(
            self.additive, {"<<": lambda a, b: a << b, ">>": lambda a, b: a >> b}
        )

    def additive(self) -> int | None:
        return self._binary(
            self.multiplicative, {"+": lambda a, b: a + b, "-": lambda a, b: a - b}
        )

    def multiplicative(self) -> int | None:
        def div(a: int, b: int) -> int | None:
            return None if b == 0 else a // b

        def mod(a: int, b: int) -> int | None:
            return None if b == 0 else a % b

        left = self.unary()
        while self.peek() in ("*", "/", "%"):
            op = self.take()
            right = self.unary()
            if left is None or right is None:
                left = None
            elif op == "*":
                left = left * right
            else:
                left = div(left, right) if op == "/" else mod(left, right)
        return left

    def unary(self) -> int | None:
        token = self.peek()
        if token == "!":
            self.take()
            value = self._truthy(self.unary())
            return None if value is None else int(not value)
        if token == "-":
            self.take()
            value = self.unary()
            return None if value is None else -value
        if token == "~":
            self.take()
            value = self.unary()
            return None if value is None else ~value
        if token == "+":
            self.take()
            return self.unary()
        return self.primary()

    def primary(self) -> int | None:
        token = self.take()
        if isinstance(token, int):
            return token
        if token == "(":
            value = self.or_expr()
            self.expect(")")
            return value
        if token == "defined":
            paren = self.peek() == "("
            if paren:
                self.take()
            name = self.take()
            if not isinstance(name, str) or not name.isidentifier():
                raise _Unparseable("defined without a macro name")
            if paren:
                self.expect(")")
            return self._defined(name)
        if token in _IS_MACROS:
            self.expect("(")
            name = self.take()
            if not isinstance(name, str):
                raise _Unparseable(f"{token} without a macro name")
            self.expect(")")
            builtin = self._defined(name)
            module = self._defined(name + "_MODULE")
            if token == "IS_BUILTIN":
                return builtin
            if token == "IS_MODULE":
                return module
            # IS_ENABLED / IS_REACHABLE: builtin || module (Kleene).
            if builtin == 1 or module == 1:
                return 1
            if builtin == 0 and module == 0:
                return 0
            return None
        if isinstance(token, str) and token.isidentifier():
            value = self.cfg.defined.get(token)
            if isinstance(value, int):
                return value
            if isinstance(value, str):
                return None  # string-valued macro in arithmetic: unknown
            return 0 if self._known_absent(token) else None
        raise _Unparseable(f"unexpected token {token!r}")

    def _defined(self, name: str) -> int | None:
        if name in self.cfg.defined:
            return 1
        return 0 if self._known_absent(name) else None

    def _known_absent(self, name: str) -> bool:
        if name in self.cfg.absent:
            return True
        return self.cfg.closed and bool(re.fullmatch(r"CONFIG_\w+", name))


def eval_expr(expr: str, cfg: ActiveConfig) -> bool | None:
    """Evaluate one preprocessor condition. None = unknown (never raises)."""
    try:
        return _Parser(_tokenize(expr), cfg).parse()
    except (_Unparseable, RecursionError):
        return None


def _kleene_and(values: list[bool | None]) -> bool | None:
    if any(value is False for value in values):
        return False
    if all(value is True for value in values):
        return True
    return None


def frame_satisfiable(frame: Frame, cfg: ActiveConfig) -> bool | None:
    values: list[bool | None] = []
    if frame.cond is not None:
        values.append(eval_expr(frame.cond, cfg))
    for negation in frame.negations:
        value = eval_expr(negation, cfg)
        values.append(None if value is None else not value)
    return _kleene_and(values) if values else True


def stack_satisfiable(stack: tuple[Frame, ...], cfg: ActiveConfig) -> bool | None:
    """Is this guard stack possibly true under the config? Kleene AND."""
    return _kleene_and([frame_satisfiable(frame, cfg) for frame in stack])
