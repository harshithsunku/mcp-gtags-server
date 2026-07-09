"""ctags metadata enrichment for symbol records (roadmap milestone 2).

Fills the ``kind`` / ``typeref`` / ``scope`` / ``signature`` record fields by
running Universal Ctags directly on the files that appear in definition-shaped
results — so ``symbol_info`` can say *what* a symbol is (function, macro,
struct, typedef, enum constant, ...) with no build and no compile database.

Everything here is best-effort by design: when Universal Ctags with JSON
output is not available, or a file cannot be parsed, records simply keep the
``null`` metadata they had before v0.8.1. No tool ever fails, slows down
noticeably, or changes shape because of enrichment.

Cost is bounded: at most one ctags run per distinct file per result page,
cached in a small LRU keyed by (mtime, size) — including negative results, so
a file ctags cannot handle is not re-parsed on every query.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
from collections import OrderedDict
from pathlib import Path

from . import toolchain

CTAGS_TIMEOUT_SECONDS = 10
# Belt and braces on top of the timeout: never feed ctags a generated blob.
MAX_FILE_BYTES = 8 * 1024 * 1024
CACHE_CAPACITY = 256

# ``--fields=*`` asks for everything ctags knows (we pick what we need, and
# stay compatible if fields are reordered); ``+px`` adds prototypes/externs so
# header declarations — which gtags may report as definitions — still match.
CTAGS_ARGS = (
    "--output-format=json",
    "--fields=*",
    "--kinds-C=+px",
    "--kinds-C++=+px",
    "-o",
    "-",
)

# When one name+line yields several tags (e.g. ``typedef struct foo foo;``
# emits both a struct and a typedef tag), prefer the kind an agent most
# likely asked about. Unlisted kinds rank last, ties break on list order.
_KIND_PRIORITY = (
    "function",
    "typedef",
    "struct",
    "union",
    "enum",
    "class",
    "macro",
    "enumerator",
    "member",
    "variable",
    "prototype",
    "externvar",
)

# ctags invents names like "__anon0416201d0103" for anonymous enums/structs;
# hash noise helps nobody — render them as "<anonymous>" instead.
_ANON_RE = re.compile(r"__anon[0-9a-fA-F]+")

# Capability probe results keyed by resolved ctags path: (usable, detail).
_probe_cache: dict[str, tuple[bool, str]] = {}
# LRU: absolute path -> ((mtime_ns, size), name -> [normalized tags]).
_tags_cache: OrderedDict[str, tuple[tuple[int, int], dict[str, list[dict]]]] = (
    OrderedDict()
)
_lock = threading.Lock()


def reset_cache() -> None:
    """Forget probe results and cached tags (used by tests)."""
    with _lock:
        _probe_cache.clear()
        _tags_cache.clear()


def probe_binary(exe: str) -> tuple[bool, str]:
    """(usable, human detail) — does this ctags emit Universal Ctags JSON?

    Cached per binary path. Also used by ``toolchain.install_ctags`` so setup
    never trusts a ctags that cannot power enrichment.
    """
    with _lock:
        if exe in _probe_cache:
            return _probe_cache[exe]
    try:
        proc = subprocess.run(
            [exe, "--version"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=CTAGS_TIMEOUT_SECONDS,
        )
        out = proc.stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    first = out.splitlines()[0].split(",")[0].strip() if out.strip() else exe
    if not out.strip():
        result = (False, f"{exe} did not run")
    elif "Universal Ctags" not in out:
        result = (False, f"{first} has no JSON output (Universal Ctags needed)")
    elif "+json" not in out:
        result = (False, f"{first} was built without +json support")
    else:
        result = (True, first)
    with _lock:
        _probe_cache[exe] = result
    return result


def available(bin_dir: str | None = None) -> bool:
    """True when a Universal Ctags with JSON output can be invoked."""
    exe = toolchain.find_ctags(bin_dir)
    return probe_binary(exe)[0] if exe else False


def status_line(bin_dir: str | None = None) -> str:
    """One human-readable line for the ``doctor`` subcommand."""
    exe = toolchain.find_ctags(bin_dir)
    if exe is None:
        return "metadata enrichment: unavailable (no ctags binary found)"
    usable, detail = probe_binary(exe)
    if usable:
        return f"metadata enrichment: active ({detail})"
    return f"metadata enrichment: unavailable ({detail})"


def _normalize_tag(obj: dict) -> dict | None:
    """One raw ctags JSON object -> {line, kind, typeref, scope, signature}."""
    line = obj.get("line")
    if obj.get("_type") != "tag" or not obj.get("name") or not isinstance(line, int):
        return None
    typeref = obj.get("typeref")
    if isinstance(typeref, str):
        if typeref.startswith("typename:"):
            # "typename:" is ctags noise; real refs like "struct:item" stay.
            typeref = typeref[len("typename:") :]
        typeref = _ANON_RE.sub("<anonymous>", typeref)
    scope = obj.get("scope")
    if isinstance(scope, str):
        if isinstance(obj.get("scopeKind"), str):
            scope = f"{obj['scopeKind']}:{scope}"
        scope = _ANON_RE.sub("<anonymous>", scope)
    return {
        "line": line,
        "kind": obj.get("kind"),
        "typeref": typeref if isinstance(typeref, str) else None,
        "scope": scope if isinstance(scope, str) else None,
        "signature": obj.get("signature") if isinstance(obj.get("signature"), str) else None,
    }


def _run_ctags(exe: str, abs_path: Path) -> dict[str, list[dict]]:
    """Run ctags on one file and index its tags by name. {} on any failure."""
    try:
        proc = subprocess.run(
            [exe, *CTAGS_ARGS, str(abs_path)],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=CTAGS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if proc.returncode != 0:
        return {}
    tags: dict[str, list[dict]] = {}
    for raw in proc.stdout.splitlines():
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict) and (tag := _normalize_tag(obj)):
            tags.setdefault(obj["name"], []).append(tag)
    return tags


def tags_for_file(abs_path: Path, bin_dir: str | None = None) -> dict[str, list[dict]]:
    """All tags ctags finds in one file, by symbol name. Cached; never raises."""
    try:
        stat = abs_path.stat()
    except OSError:
        return {}
    signature = (stat.st_mtime_ns, stat.st_size)
    key = str(abs_path)
    with _lock:
        cached = _tags_cache.get(key)
        if cached and cached[0] == signature:
            _tags_cache.move_to_end(key)
            return cached[1]
    if stat.st_size > MAX_FILE_BYTES:
        tags: dict[str, list[dict]] = {}
    else:
        exe = toolchain.find_ctags(bin_dir)
        if exe is None or not probe_binary(exe)[0]:
            return {}
        # The subprocess runs outside the lock; a rare duplicate ctags run on
        # a concurrent query is cheaper than serializing every enrichment.
        tags = _run_ctags(exe, abs_path)
    with _lock:
        _tags_cache[key] = (signature, tags)
        _tags_cache.move_to_end(key)
        while len(_tags_cache) > CACHE_CAPACITY:
            _tags_cache.popitem(last=False)
    return tags


def _kind_rank(kind: str | None) -> int:
    try:
        return _KIND_PRIORITY.index(kind)
    except ValueError:
        return len(_KIND_PRIORITY)


def best_tag(tags: dict[str, list[dict]], name: str, line: int) -> dict | None:
    """The tag that matches a (name, line) result record, or None.

    Exact line match first (kind-priority tie-break for multi-tag lines like
    ``typedef struct foo foo;``); when the name is unique in the file, accept
    it at any line — gtags' parser and ctags occasionally disagree by a line.
    """
    entries = tags.get(name)
    if not entries:
        return None
    exact = [t for t in entries if t["line"] == line]
    if exact:
        return min(exact, key=lambda t: _kind_rank(t["kind"]))
    if len(entries) == 1:
        return entries[0]
    return None
