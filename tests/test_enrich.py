"""Unit tests for ctags metadata enrichment (no gtags/global needed)."""

import json
import subprocess
import textwrap

import pytest

from gtags_mcp import enrich, toolchain


@pytest.fixture(autouse=True)
def fresh_enrich_cache():
    enrich.reset_cache()
    yield
    enrich.reset_cache()


def _fake_completed(stdout: str, returncode: int = 0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


# ---------------------------------------------------------------------------
# Tag normalization
# ---------------------------------------------------------------------------


def test_normalize_strips_typename_prefix_and_composes_scope():
    tag = enrich._normalize_tag(
        {
            "_type": "tag",
            "name": "id",
            "line": 11,
            "kind": "member",
            "typeref": "typename:item_id_t",
            "scope": "item",
            "scopeKind": "struct",
        }
    )
    assert tag == {
        "line": 11,
        "kind": "member",
        "typeref": "item_id_t",
        "scope": "struct:item",
        "signature": None,
    }


def test_normalize_keeps_meaningful_typeref():
    tag = enrich._normalize_tag(
        {"_type": "tag", "name": "item_t", "line": 15, "kind": "typedef", "typeref": "struct:item"}
    )
    assert tag["typeref"] == "struct:item"


def test_normalize_anonymous_names_made_readable():
    # ctags invents "__anon<hash>" names for anonymous enums/structs.
    tag = enrich._normalize_tag(
        {
            "_type": "tag",
            "name": "TCP_ESTABLISHED",
            "line": 20,
            "kind": "enumerator",
            "scope": "__anon0416201d0103",
            "scopeKind": "enum",
            "typeref": "struct:__anon4a343dbc0308",
        }
    )
    assert tag["scope"] == "enum:<anonymous>"
    assert tag["typeref"] == "struct:<anonymous>"


def test_normalize_scope_without_scopekind_kept_raw():
    tag = enrich._normalize_tag(
        {"_type": "tag", "name": "x", "line": 3, "kind": "member", "scope": "outer"}
    )
    assert tag["scope"] == "outer"


def test_normalize_rejects_non_tags_and_bad_lines():
    assert enrich._normalize_tag({"_type": "ptag", "name": "x", "line": 1}) is None
    assert enrich._normalize_tag({"_type": "tag", "name": "x", "line": "7"}) is None
    assert enrich._normalize_tag({"_type": "tag", "line": 1}) is None


def test_run_ctags_skips_malformed_json_lines(monkeypatch, tmp_path):
    good = json.dumps(
        {"_type": "tag", "name": "f", "line": 2, "kind": "function", "signature": "(void)"}
    )
    stdout = f"not json\n{good}\n{{\"_type\": \"tag\"}}\n"
    monkeypatch.setattr(
        enrich.subprocess, "run", lambda *a, **k: _fake_completed(stdout)
    )
    tags = enrich._run_ctags("ctags", tmp_path / "f.c")
    assert list(tags) == ["f"]
    assert tags["f"][0]["signature"] == "(void)"


# ---------------------------------------------------------------------------
# best_tag matching policy
# ---------------------------------------------------------------------------


def _tags(*entries):
    tags = {}
    for name, line, kind in entries:
        tags.setdefault(name, []).append(
            {"line": line, "kind": kind, "typeref": None, "scope": None, "signature": None}
        )
    return tags


def test_best_tag_exact_line_match():
    tags = _tags(("f", 10, "prototype"), ("f", 40, "function"))
    assert enrich.best_tag(tags, "f", 40)["kind"] == "function"
    assert enrich.best_tag(tags, "f", 10)["kind"] == "prototype"


def test_best_tag_unique_name_fallback_tolerates_line_drift():
    tags = _tags(("g", 12, "function"))
    assert enrich.best_tag(tags, "g", 14)["kind"] == "function"


def test_best_tag_ambiguous_without_line_match_returns_none():
    tags = _tags(("f", 10, "prototype"), ("f", 40, "function"))
    assert enrich.best_tag(tags, "f", 99) is None
    assert enrich.best_tag(tags, "missing", 1) is None


def test_best_tag_same_line_tie_broken_by_kind_priority():
    # `typedef struct foo foo;` emits struct + typedef tags on one line.
    tags = _tags(("foo", 5, "struct"), ("foo", 5, "typedef"))
    assert enrich.best_tag(tags, "foo", 5)["kind"] == "typedef"


# ---------------------------------------------------------------------------
# Capability probe
# ---------------------------------------------------------------------------

UNIVERSAL_JSON = (
    "Universal Ctags 6.2.0(5437fa6), Copyright (C) 2015-2025 Universal Ctags Team\n"
    "  Optional compiled features: +wildcards, +regex, +json, +yaml\n"
)
UNIVERSAL_NO_JSON = (
    "Universal Ctags 6.2.0(5437fa6), Copyright (C) 2015-2025 Universal Ctags Team\n"
    "  Optional compiled features: +wildcards, +regex\n"
)
EXUBERANT = "Exuberant Ctags 5.8, Copyright (C) 1996-2009 Darren Hiebert\n"


@pytest.mark.parametrize(
    ("version_out", "usable"),
    [(UNIVERSAL_JSON, True), (UNIVERSAL_NO_JSON, False), (EXUBERANT, False)],
)
def test_available_probes_version_output(monkeypatch, version_out, usable):
    monkeypatch.setattr(toolchain, "find_ctags", lambda *a, **k: "/fake/ctags")
    monkeypatch.setattr(
        enrich.subprocess, "run", lambda *a, **k: _fake_completed(version_out)
    )
    assert enrich.available() is usable


def test_available_false_without_binary(monkeypatch):
    monkeypatch.setattr(toolchain, "find_ctags", lambda *a, **k: None)
    assert enrich.available() is False


def test_probe_swallows_subprocess_failure(monkeypatch):
    def boom(*a, **k):
        raise OSError("no such file")

    monkeypatch.setattr(toolchain, "find_ctags", lambda *a, **k: "/fake/ctags")
    monkeypatch.setattr(enrich.subprocess, "run", boom)
    assert enrich.available() is False
    assert "unavailable" in enrich.status_line()


def test_probe_result_cached_per_binary(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return _fake_completed(UNIVERSAL_JSON)

    monkeypatch.setattr(toolchain, "find_ctags", lambda *a, **k: "/fake/ctags")
    monkeypatch.setattr(enrich.subprocess, "run", fake_run)
    assert enrich.available() and enrich.available()
    assert len(calls) == 1


def test_status_line_active(monkeypatch):
    monkeypatch.setattr(toolchain, "find_ctags", lambda *a, **k: "/fake/ctags")
    monkeypatch.setattr(
        enrich.subprocess, "run", lambda *a, **k: _fake_completed(UNIVERSAL_JSON)
    )
    line = enrich.status_line()
    assert line.startswith("metadata enrichment: active")
    assert "Universal Ctags 6.2.0(5437fa6)" in line


# ---------------------------------------------------------------------------
# tags_for_file: cache behavior and failure swallowing
# ---------------------------------------------------------------------------


@pytest.fixture
def ctags_stub(monkeypatch, tmp_path):
    """Route ctags subprocess calls through a canned-output counter."""
    state = {"runs": 0, "stdout": "", "returncode": 0, "raise": None}

    def fake_run(args, **kwargs):
        if args[-1] == "--version":
            return _fake_completed(UNIVERSAL_JSON)
        state["runs"] += 1
        if state["raise"]:
            raise state["raise"]
        return _fake_completed(state["stdout"], state["returncode"])

    monkeypatch.setattr(toolchain, "find_ctags", lambda *a, **k: "/fake/ctags")
    monkeypatch.setattr(enrich.subprocess, "run", fake_run)
    return state


def _tag_line(name, line, kind="function"):
    return json.dumps({"_type": "tag", "name": name, "line": line, "kind": kind})


def test_tags_for_file_cached_until_file_changes(ctags_stub, tmp_path):
    src = tmp_path / "a.c"
    src.write_text("int f(void) { return 0; }\n")
    ctags_stub["stdout"] = _tag_line("f", 1)

    assert "f" in enrich.tags_for_file(src)
    assert "f" in enrich.tags_for_file(src)
    assert ctags_stub["runs"] == 1

    src.write_text("/* moved */\nint f(void) { return 0; }\n")
    ctags_stub["stdout"] = _tag_line("f", 2)
    assert enrich.tags_for_file(src)["f"][0]["line"] == 2
    assert ctags_stub["runs"] == 2


def test_tags_for_file_caches_negative_results(ctags_stub, tmp_path):
    src = tmp_path / "broken.c"
    src.write_text("int\n")
    ctags_stub["returncode"] = 1
    assert enrich.tags_for_file(src) == {}
    assert enrich.tags_for_file(src) == {}
    assert ctags_stub["runs"] == 1


def test_tags_for_file_missing_file(ctags_stub, tmp_path):
    assert enrich.tags_for_file(tmp_path / "nope.c") == {}
    assert ctags_stub["runs"] == 0


def test_tags_for_file_skips_huge_files(ctags_stub, tmp_path, monkeypatch):
    monkeypatch.setattr(enrich, "MAX_FILE_BYTES", 10)
    src = tmp_path / "huge.c"
    src.write_text("x" * 100)
    assert enrich.tags_for_file(src) == {}
    assert ctags_stub["runs"] == 0


@pytest.mark.parametrize(
    "exc", [OSError("gone"), subprocess.TimeoutExpired(cmd="ctags", timeout=1)]
)
def test_tags_for_file_swallows_subprocess_errors(ctags_stub, tmp_path, exc):
    src = tmp_path / "a.c"
    src.write_text("int f;\n")
    ctags_stub["raise"] = exc
    assert enrich.tags_for_file(src) == {}


def test_lru_evicts_oldest_entry(ctags_stub, tmp_path, monkeypatch):
    monkeypatch.setattr(enrich, "CACHE_CAPACITY", 2)
    files = []
    for i in range(3):
        f = tmp_path / f"f{i}.c"
        f.write_text(f"int f{i};\n")
        files.append(f)
    ctags_stub["stdout"] = _tag_line("f", 1)
    for f in files:
        enrich.tags_for_file(f)
    assert ctags_stub["runs"] == 3
    enrich.tags_for_file(files[0])  # evicted -> re-parsed
    assert ctags_stub["runs"] == 4
    enrich.tags_for_file(files[2])  # still cached
    assert ctags_stub["runs"] == 4


# ---------------------------------------------------------------------------
# Real ctags end-to-end (skips without Universal Ctags +json)
# ---------------------------------------------------------------------------

requires_ctags_json = pytest.mark.skipif(
    not enrich.available(), reason="Universal Ctags with JSON output not available"
)


@requires_ctags_json
def test_real_ctags_extracts_c_metadata(tmp_path):
    enrich.reset_cache()  # module-level skip probe polluted the cache
    src = tmp_path / "types.c"
    src.write_text(
        textwrap.dedent(
            """\
            #define SQUARE(x) ((x) * (x))
            enum color { RED, GREEN };
            struct item { int id; };
            typedef struct item item_t;
            int process(struct item *it, int n)
            {
                return it->id + n;
            }
            """
        )
    )
    tags = enrich.tags_for_file(src)
    assert enrich.best_tag(tags, "SQUARE", 1)["signature"] == "(x)"
    assert enrich.best_tag(tags, "GREEN", 2)["scope"] == "enum:color"
    assert enrich.best_tag(tags, "item_t", 4)["typeref"] == "struct:item"
    func = enrich.best_tag(tags, "process", 5)
    assert func["kind"] == "function"
    assert func["typeref"] == "int"
    assert "struct item" in func["signature"]
