"""End-to-end tests for the gtags MCP tools against a tiny C project."""

import json
import os
import subprocess
import textwrap

import pytest

from gtags_mcp import config, enrich, guards, server, toolchain

requires_global = pytest.mark.skipif(
    toolchain.find_global() is None or toolchain.find_gtags() is None,
    reason="GNU Global not installed",
)

requires_pygments = pytest.mark.skipif(
    toolchain.find_global() is None
    or toolchain.find_gtags() is None
    or not server._plugin_deps_available(),
    reason="ctags + Pygments plugin parser not available",
)


def _drain_refresh_state():
    for thread in list(server._refresh_threads.values()):
        thread.join(timeout=30)
    server._last_update.clear()
    server._update_cost.clear()
    server._refresh_errors.clear()
    server._refresh_threads.clear()


@pytest.fixture(autouse=True)
def fresh_update_cache():
    """Isolate debounce/refresh, config-cache, and enrich-cache state."""
    _drain_refresh_state()
    config.reset_cache()
    enrich.reset_cache()
    guards.reset_cache()
    yield
    _drain_refresh_state()
    config.reset_cache()
    enrich.reset_cache()
    guards.reset_cache()


@pytest.fixture
def c_project(tmp_path):
    (tmp_path / "util.h").write_text(
        textwrap.dedent(
            """\
            #ifndef UTIL_H
            #define UTIL_H
            int add_numbers(int a, int b);
            #endif
            """
        )
    )
    (tmp_path / "util.c").write_text(
        textwrap.dedent(
            """\
            #include "util.h"

            int add_numbers(int a, int b)
            {
                return a + b;
            }
            """
        )
    )
    (tmp_path / "main.c").write_text(
        textwrap.dedent(
            """\
            #include <stdio.h>
            #include "util.h"

            int main(void)
            {
                /* TODO: handle argv */
                printf("%d\\n", add_numbers(2, 3));
                return 0;
            }
            """
        )
    )
    return tmp_path


@requires_global
def test_auto_index_on_first_query(c_project):
    """Queries build the index themselves — into .gtags-mcp/, not the root."""
    root = str(c_project)
    assert not (c_project / server.INDEX_DIR_NAME).exists()

    definition = server.find_definition("add_numbers", root)
    assert "util.c" in definition
    db_dir = c_project / server.INDEX_DIR_NAME
    assert (db_dir / "GTAGS").is_file()
    # The project root itself stays clean, and the index dir self-gitignores.
    for name in ("GTAGS", "GRTAGS", "GPATH"):
        assert not (c_project / name).exists()
    assert (db_dir / ".gitignore").read_text() == "*\n"


@requires_global
def test_query_flow(c_project):
    root = str(c_project)

    references = server.find_references("add_numbers", root)
    assert "main.c" in references

    symbols = server.list_file_symbols("util.c", root)
    assert "add_numbers" in symbols

    # printf has no in-tree reference records: find_references falls back to
    # the symbol-usage database (`global -sx`) and flags it in the envelope.
    usages = json.loads(server.find_references("printf", root))
    assert usages["fallback"] == "symbol_usages"
    assert any(r["path"] == "main.c" for r in usages["results"])


@requires_global
def test_definition_miss_carries_prefix_suggestions(c_project):
    root = str(c_project)
    miss = json.loads(server.find_definition("add_", root))
    assert miss["results"] == []
    assert "add_numbers" in miss["suggestions"]
    text = server.find_definition("add_", root, format="text")
    assert "Similar defined symbols: add_numbers" in text


@requires_global
def test_default_root_is_cwd(c_project, monkeypatch):
    monkeypatch.chdir(c_project)
    definition = server.find_definition("add_numbers")
    assert "util.c" in definition


@requires_global
def test_auto_update_picks_up_new_symbol(c_project):
    """A new file becomes visible after the background refresh completes."""
    root = str(c_project)
    server.find_definition("add_numbers", root)  # builds index

    (c_project / "extra.c").write_text("int extra_fn(void) { return 42; }\n")
    server._last_update.clear()  # get past the debounce window

    server.find_definition("extra_fn", root)  # kicks background refresh
    server._wait_for_refresh(c_project.resolve())

    definition = server.find_definition("extra_fn", root)
    assert "extra.c" in definition


@requires_global
def test_query_never_blocks_on_refresh(c_project, monkeypatch):
    """Queries answer immediately even while a slow refresh runs behind."""
    import time as _time

    root = str(c_project)
    server.find_definition("add_numbers", root)  # builds index
    server._last_update.clear()  # force a refresh on the next query

    real_run = server._run

    def slow_update_run(args, cwd, timeout=server.QUERY_TIMEOUT_SECONDS, **kwargs):
        if args[0] == "gtags" and "-i" in args:  # incremental refresh
            _time.sleep(1.0)
        return real_run(args, cwd, timeout, **kwargs)

    monkeypatch.setattr(server, "_run", slow_update_run)

    t0 = _time.monotonic()
    result = server.find_definition("add_numbers", root)
    elapsed = _time.monotonic() - t0
    assert "util.c" in result
    assert elapsed < 0.5, f"query blocked on refresh ({elapsed:.2f}s)"
    server._wait_for_refresh(c_project.resolve())


@requires_global
def test_update_index_is_synchronous_barrier(c_project):
    root = str(c_project)
    server.find_definition("add_numbers", root)  # builds index

    (c_project / "fresh.c").write_text("int fresh_fn(void) { return 7; }\n")
    result = server.update_index(root)
    assert "synchronous" in result

    definition = server.find_definition("fresh_fn", root)
    assert "fresh.c" in definition


@requires_global
def test_background_refresh_error_surfaces(c_project, monkeypatch):
    root = str(c_project)
    server.find_definition("add_numbers", root)  # builds index
    server._last_update.clear()  # force a refresh on the next query

    real_run = server._run

    def failing_update_run(args, cwd, timeout=server.QUERY_TIMEOUT_SECONDS, **kwargs):
        if args[0] == "gtags" and "-i" in args:  # incremental refresh
            # Sleep so the kicking query returns before the failure is
            # recorded — the warning must surface on the NEXT query.
            __import__("time").sleep(0.3)
            return "", "simulated index corruption", 3
        return real_run(args, cwd, timeout, **kwargs)

    monkeypatch.setattr(server, "_run", failing_update_run)

    server.find_definition("add_numbers", root)  # kicks failing refresh
    server._wait_for_refresh(c_project.resolve())
    monkeypatch.setattr(server, "_run", real_run)
    server._last_update[c_project.resolve()] = __import__("time").monotonic()

    result = server.find_definition("add_numbers", root)
    assert "Warning: background index refresh failed" in result
    assert "simulated index corruption" in result


@requires_global
def test_failed_full_build_leaves_no_partial_index(c_project, monkeypatch):
    """A failed first build must not strand a partial GTAGS that every later
    query trips over ('seems corrupted') — the DB files are cleaned up and the
    next query rebuilds from scratch."""
    root = str(c_project)
    real_run = server._run

    def failing_full_build(args, cwd, timeout=server.QUERY_TIMEOUT_SECONDS, **kwargs):
        if args[0] == "gtags" and "-i" not in args:
            db_dir = c_project / server.INDEX_DIR_NAME
            db_dir.mkdir(exist_ok=True)
            (db_dir / "GTAGS").write_bytes(b"partial garbage")
            return "", "simulated parser crash", 1
        return real_run(args, cwd, timeout, **kwargs)

    monkeypatch.setattr(server, "_run", failing_full_build)
    result = server.find_definition("add_numbers", root)
    assert "automatic indexing failed" in result
    assert not (c_project / server.INDEX_DIR_NAME / "GTAGS").exists()

    monkeypatch.setattr(server, "_run", real_run)
    recovered = server.find_definition("add_numbers", root)
    assert "util.c" in recovered


@requires_global
def test_corrupted_index_auto_recovers(c_project):
    """A corrupt database (interrupted build, crashed process) is wiped,
    rebuilt, and the query retried — with a warning in the envelope."""
    root = str(c_project)
    assert "util.c" in server.find_definition("add_numbers", root)  # healthy build
    (c_project / server.INDEX_DIR_NAME / "GTAGS").write_bytes(b"\x00garbage" * 64)

    result = server.find_definition("add_numbers", root)
    assert "util.c" in result
    assert "rebuilt automatically" in result


@requires_global
def test_explicit_index_and_update_tools(c_project):
    root = str(c_project)
    assert "Rebuilt" in server.update_index(root, full=True)

    (c_project / "extra.c").write_text("int extra_fn(void) { return 42; }\n")
    assert "updated" in server.update_index(root)
    assert "extra.c" in server.find_definition("extra_fn", root)


@pytest.fixture
def many_symbols_project(c_project):
    """The tiny C project plus a file defining four functions (pagination)."""
    (c_project / "many.c").write_text(
        "int fn_a(void) { return 1; }\n"
        "int fn_b(void) { return 2; }\n"
        "int fn_c(void) { return 3; }\n"
        "int fn_d(void) { return 4; }\n"
    )
    return c_project


@requires_global
def test_pagination(many_symbols_project):
    root = str(many_symbols_project)
    full = server.list_file_symbols("many.c", root, limit=100, format="text")
    total = len(full.splitlines())
    assert total >= 3

    page = server.list_file_symbols("many.c", root, limit=2, format="text")
    assert f"showing 1-2 of {total} matches" in page
    assert "pass offset=2 to continue" in page

    page2 = server.list_file_symbols("many.c", root, limit=2, offset=2, format="text")
    assert f"showing 3-{min(4, total)} of {total} matches" in page2

    past_end = server.list_file_symbols("many.c", root, offset=999, format="text")
    assert "past the last" in past_end


@requires_global
def test_case_insensitive(c_project):
    root = str(c_project)
    assert "No definition found" in server.find_definition("ADD_NUMBERS", root)
    result = server.find_definition("ADD_NUMBERS", root, case_insensitive=True)
    assert "util.c" in result


@requires_global
def test_long_lines_are_truncated(c_project):
    root = str(c_project)
    long_line = "int long_named_fn(void) { return 0; } /* " + "x" * 500 + " */\n"
    (c_project / "long.c").write_text(long_line)

    result = server.find_definition("long_named_fn", root, format="text")
    assert "long.c" in result
    assert all(len(line) <= server.MAX_LINE_CHARS + 4 for line in result.splitlines())

    # JSON snippets are truncated too.
    record = json.loads(server.find_definition("long_named_fn", root))["results"][0]
    assert len(record["snippet"]) <= server.MAX_LINE_CHARS + 4


@requires_global
def test_get_symbol_body_returns_only_the_function(c_project):
    root = str(c_project)
    body = server.get_symbol_body("add_numbers", root, format="text")
    assert "=== util.c:3 ===" in body
    assert "return a + b;" in body
    # It must not leak the rest of the file or other files.
    assert "#include" not in body
    assert "printf" not in body


@requires_global
def test_get_symbol_body_multiline_macro(c_project):
    (c_project / "macros.h").write_text(
        "#define SQUARE(x) \\\n    ((x) * (x))\n\nint after_macro;\n"
    )
    body = server.get_symbol_body("SQUARE", str(c_project))
    assert "((x) * (x))" in body
    assert "after_macro" not in body


@requires_global
def test_find_callers_maps_refs_to_enclosing_function(c_project):
    root = str(c_project)
    result = server.find_callers("add_numbers", root, format="text")
    # gtags also counts the util.h prototype as a reference; the call from
    # main() must be attributed to the enclosing function `main`.
    assert "main  main.c  1 call site at line(s) 7" in result

    callers = json.loads(server.find_callers("add_numbers", root))["results"]
    assert {"caller": "main", "path": "main.c", "sites": [7]} in callers


@requires_global
def test_summarize_references(c_project):
    root = str(c_project)
    result = server.summarize_references("add_numbers", root, format="text")
    assert "2 references across 2 files:" in result
    assert "main.c" in result and "util.h" in result

    summary = json.loads(server.summarize_references("add_numbers", root))
    assert summary["total_references"] == 2
    assert {r["path"] for r in summary["results"]} == {"main.c", "util.h"}


@requires_global
def test_find_callees(c_project):
    root = str(c_project)
    result = server.find_callees("main", root, format="text")
    assert "add_numbers  util.c:3" in result
    assert "External/unresolved: printf" in result

    callees = json.loads(server.find_callees("main", root))["results"]
    assert {"symbol": "add_numbers", "path": "util.c", "line": 3} in callees["in_tree"]
    assert "printf" in callees["external"]


@requires_global
def test_symbol_info(c_project):
    root = str(c_project)
    result = server.symbol_info("add_numbers", root, format="text")
    assert "defined at util.c:3" in result
    assert "referenced 2 time(s) across 2 file(s)" in result
    assert "next: get_symbol_body" in result

    info = json.loads(server.symbol_info("add_numbers", root))
    card = info["results"]
    assert card["definitions"][0]["path"] == "util.c"
    assert card["reference_count"] == 2 and card["file_count"] == 2
    assert "get_symbol_body" in info["next_tools"]


@pytest.fixture
def mixed_project(c_project):
    (c_project / "pylib.py").write_text(
        textwrap.dedent(
            """\
            def py_util(a, b):
                total = a + b
                return total


            def unrelated():
                return 42
            """
        )
    )
    (c_project / "app.py").write_text(
        textwrap.dedent(
            """\
            from pylib import py_util


            def run_app():
                return py_util(2, 3)
            """
        )
    )
    return c_project


@requires_pygments
def test_mixed_project_python_definition(mixed_project):
    root = str(mixed_project)
    result = server.find_definition("py_util", root)
    assert "pylib.py" in result


@requires_pygments
def test_mixed_project_python_references(mixed_project):
    root = str(mixed_project)
    result = server.find_references("py_util", root)
    assert "app.py" in result


@requires_pygments
def test_mixed_project_c_still_works(mixed_project):
    root = str(mixed_project)
    result = server.find_definition("add_numbers", root)
    assert "util.c" in result


@requires_pygments
def test_python_body_extraction(mixed_project):
    root = str(mixed_project)
    body = server.get_symbol_body("py_util", root)
    assert "def py_util(a, b):" in body
    assert "return total" in body
    assert "unrelated" not in body  # indentation-delimited: next def excluded


@requires_pygments
def test_python_callees(mixed_project):
    root = str(mixed_project)
    result = server.find_callees("run_app", root, format="text")
    assert "py_util  pylib.py:1" in result


@requires_pygments
def test_index_reports_multilanguage_label(mixed_project):
    result = server.update_index(str(mixed_project), full=True)
    assert "native-pygments" in result


def test_bad_project_root():
    result = json.loads(server.find_definition("main", "/nonexistent/path/xyz"))
    assert result["error"].startswith("Error")
    assert result["next_tools"]
    text = server.find_definition("main", "/nonexistent/path/xyz", format="text")
    assert text.startswith("Error")


@requires_global
def test_no_match_message(c_project):
    root = str(c_project)
    result = json.loads(server.find_definition("does_not_exist_anywhere", root))
    assert result["results"] == [] and result["total"] == 0
    assert "No definition found" in result["message"]


# ---------------------------------------------------------------------------
# Milestone 1: structured JSON output, junk skipping, root auto-detection
# ---------------------------------------------------------------------------

RECORD_KEYS = {
    "symbol", "path", "line", "col",
    "kind", "typeref", "scope", "signature", "guard", "snippet",
}


@requires_global
def test_json_record_schema(c_project):
    result = json.loads(server.find_definition("add_numbers", str(c_project)))
    assert result["tool"] == "find_definition"
    assert result["total"] == 1 and result["offset"] == 0
    assert result["truncated"] is False and result["warning"] is None
    (record,) = result["results"]
    assert set(record) == RECORD_KEYS
    assert record["symbol"] == "add_numbers"
    assert record["path"] == "util.c"  # repo-relative
    assert record["line"] == 3
    assert record["col"] == 5  # 1-based position of the symbol in the snippet
    # kind is populated only when Universal Ctags (+json) is available;
    # strict enrichment assertions live in the milestone-2 test section.
    assert record["kind"] in (None, "function")
    assert record["guard"] == []  # scanned, unconditional (milestone 3)
    assert "add_numbers" in record["snippet"]


@requires_global
def test_json_next_tools_hints(c_project):
    root = str(c_project)
    hit = json.loads(server.find_definition("add_numbers", root))
    assert "get_symbol_body" in hit["next_tools"]
    miss = json.loads(server.find_definition("no_such_symbol", root))
    assert "find_references" in miss["next_tools"]


@requires_global
def test_json_pagination(many_symbols_project):
    root = str(many_symbols_project)
    full = json.loads(server.list_file_symbols("many.c", root, limit=100))
    total = full["total"]
    assert total >= 3 and full["truncated"] is False

    page = json.loads(server.list_file_symbols("many.c", root, limit=2))
    assert len(page["results"]) == 2
    assert page["total"] == total and page["truncated"] is True

    page2 = json.loads(server.list_file_symbols("many.c", root, limit=2, offset=2))
    assert page2["offset"] == 2
    assert page2["results"][0] == full["results"][2]

    past_end = json.loads(server.list_file_symbols("many.c", root, offset=999))
    assert past_end["results"] == [] and past_end["total"] == total


@pytest.fixture
def git_project(c_project):
    """The tiny C project as a git repo with an ignored build/ directory."""
    subprocess.run(["git", "init", "-q", str(c_project)], check=True)
    (c_project / ".gitignore").write_text("build/\n")
    (c_project / "build").mkdir()
    (c_project / "build" / "generated.c").write_text("int generated_fn(void) { return 1; }\n")
    return c_project


@requires_global
def test_gitignored_files_are_not_indexed(git_project):
    root = str(git_project)
    assert "util.c" in server.find_definition("add_numbers", root)  # indexed fine
    result = json.loads(server.find_definition("generated_fn", root))
    assert result["results"] == []
    paths, _, err = server._raw_global(["-P"], root)
    assert err is None and "build/generated.c" not in paths


@requires_global
def test_newly_ignored_file_dropped_on_refresh(git_project):
    root = str(git_project)
    server.find_definition("add_numbers", root)  # builds index
    (git_project / ".gitignore").write_text("build/\nutil.c\n")
    server.update_index(root)
    result = json.loads(server.find_definition("add_numbers", root))
    assert result["results"] == []


@requires_global
def test_skip_globs_config(c_project):
    (c_project / "skipped.gen.c").write_text("int from_generator(void) { return 1; }\n")
    (c_project / config.PROJECT_CONFIG_NAME).write_text('skip_globs = ["*.gen.c"]\n')
    root = str(c_project)
    assert "util.c" in server.find_definition("add_numbers", root)
    result = json.loads(server.find_definition("from_generator", root))
    assert result["results"] == []


@requires_global
def test_root_autodetected_from_subdirectory(c_project, monkeypatch):
    root = str(c_project)
    server.find_definition("add_numbers", root)  # builds GTAGS at the root
    subdir = c_project / "nested" / "deeper"
    subdir.mkdir(parents=True)
    monkeypatch.chdir(subdir)
    result = json.loads(server.find_definition("add_numbers"))
    assert result["root"] == str(c_project.resolve())
    assert result["results"][0]["path"] == "util.c"


# ---------------------------------------------------------------------------
# Milestone 2: ctags metadata enrichment (kind / typeref / scope / signature)
# ---------------------------------------------------------------------------

requires_ctags_json = pytest.mark.skipif(
    not enrich.available(),
    reason="Universal Ctags with JSON output not available",
)


@pytest.fixture
def rich_c_project(tmp_path):
    """A C project with one of everything enrichment can classify."""
    (tmp_path / "types.h").write_text(
        textwrap.dedent(
            """\
            #ifndef TYPES_H
            #define TYPES_H
            #define MAX_ITEMS 64
            struct item;
            int process_items(struct item *items, int count);
            #endif
            """
        )
    )
    (tmp_path / "types.c").write_text(
        textwrap.dedent(
            """\
            #include "types.h"

            #define SQUARE(x) ((x) * (x))

            enum color { COLOR_RED, COLOR_GREEN = 5 };

            struct item {
                unsigned long id;
                char name[32];
            };

            typedef struct item item_t;

            int process_items(struct item *items, int count)
            {
                int total = 0;
                for (int i = 0; i < count; i++)
                    total += SQUARE((int)items[i].id);
                return total;
            }
            """
        )
    )
    return tmp_path


def _definition_record(symbol, root, path=None):
    records = json.loads(server.find_definition(symbol, root))["results"]
    if path is not None:
        records = [r for r in records if r["path"] == path]
    assert records, f"no definition record for {symbol}"
    return records[0]


@requires_global
@requires_ctags_json
def test_enriched_function_definition(rich_c_project):
    rec = _definition_record("process_items", str(rich_c_project), path="types.c")
    assert rec["kind"] == "function"
    assert rec["typeref"] == "int"
    assert "struct item" in rec["signature"]
    assert rec["guard"] == []  # scanned, unconditional (milestone 3)


@requires_global
@requires_ctags_json
def test_enriched_kinds_across_c_constructs(rich_c_project):
    root = str(rich_c_project)
    enumerator = _definition_record("COLOR_GREEN", root)
    assert enumerator["kind"] == "enumerator"
    assert enumerator["scope"] == "enum:color"

    typedef = _definition_record("item_t", root)
    assert typedef["kind"] == "typedef"
    assert typedef["typeref"] == "struct:item"

    fn_macro = _definition_record("SQUARE", root)
    assert fn_macro["kind"] == "macro" and fn_macro["signature"] == "(x)"

    obj_macro = _definition_record("MAX_ITEMS", root)
    assert obj_macro["kind"] == "macro" and obj_macro["signature"] is None

    struct = _definition_record("item", root, path="types.c")
    assert struct["kind"] == "struct"


@requires_global
@requires_ctags_json
def test_list_file_symbols_enriched(rich_c_project):
    result = json.loads(server.list_file_symbols("types.c", str(rich_c_project)))
    kinds = {r["symbol"]: r["kind"] for r in result["results"]}
    assert kinds.get("process_items") == "function"
    assert kinds.get("item_t") == "typedef"
    assert kinds.get("SQUARE") == "macro"


@requires_global
@requires_ctags_json
def test_symbol_info_card_enriched(rich_c_project):
    root = str(rich_c_project)
    card = json.loads(server.symbol_info("process_items", root))["results"]
    definition = card["definitions"][0]
    assert definition["kind"] == "function"
    assert definition["typeref"] == "int"
    assert "struct item" in definition["signature"]

    text = server.symbol_info("process_items", root, format="text")
    assert "function process_items(" in text
    assert "-> int" in text

    enum_text = server.symbol_info("COLOR_GREEN", root, format="text")
    assert "enumerator COLOR_GREEN (enum:color)" in enum_text

    typedef_text = server.symbol_info("item_t", root, format="text")
    assert "typedef item_t = struct:item" in typedef_text


@requires_global
@requires_ctags_json
def test_references_stay_unenriched(rich_c_project):
    root = str(rich_c_project)
    refs = json.loads(server.find_references("SQUARE", root))["results"]
    assert refs and all(r["kind"] is None for r in refs)


@requires_global
@requires_ctags_json
@pytest.mark.parametrize("how", ["config", "env", "flag"])
def test_enrichment_opt_out(rich_c_project, monkeypatch, how):
    root = str(rich_c_project)
    if how == "config":
        (rich_c_project / config.PROJECT_CONFIG_NAME).write_text("enrich = false\n")
    elif how == "env":
        monkeypatch.setenv("GTAGS_MCP_ENRICH", "0")
    else:
        monkeypatch.setattr(server, "_no_enrich", True)

    def forbidden(*args, **kwargs):  # opt-out must never reach ctags
        raise AssertionError("tags_for_file called despite enrichment opt-out")

    monkeypatch.setattr(enrich, "tags_for_file", forbidden)
    rec = _definition_record("process_items", root, path="types.c")
    assert rec["kind"] is None and rec["signature"] is None
    assert rec["typeref"] is None and rec["scope"] is None


@requires_global
@requires_ctags_json
def test_enrichment_tracks_file_edits(rich_c_project):
    root = str(rich_c_project)
    before = _definition_record("process_items", root, path="types.c")
    assert before["kind"] == "function"

    source = (rich_c_project / "types.c").read_text()
    (rich_c_project / "types.c").write_text(
        source.replace(
            "int process_items(", "/* moved */\n\nlong process_items("
        )
    )
    # Nudge mtime past the index build's second so `global -u` sees the edit.
    stat = (rich_c_project / "types.c").stat()
    os.utime(rich_c_project / "types.c", (stat.st_atime + 2, stat.st_mtime + 2))
    server.update_index(root)  # synchronous freshness barrier
    after = _definition_record("process_items", root, path="types.c")
    assert after["line"] > before["line"]
    assert after["kind"] == "function"
    assert after["typeref"] == "long"


# ---------------------------------------------------------------------------
# Milestone 3: #ifdef guard awareness + active_config filtering
# ---------------------------------------------------------------------------


@pytest.fixture
def guarded_c_project(tmp_path):
    """A C project with #ifdef alternates, an include guard, and plain code."""
    (tmp_path / "feature.h").write_text(
        textwrap.dedent(
            """\
            #ifndef FEATURE_H
            #define FEATURE_H

            int always_here(void);

            #ifdef CONFIG_FOO
            int foo_mode(int x);
            #else
            static inline int foo_mode(int x) { return 0; }
            #endif

            #endif /* FEATURE_H */
            """
        )
    )
    (tmp_path / "feature.c").write_text(
        textwrap.dedent(
            """\
            #include "feature.h"

            int always_here(void)
            {
                return foo_mode(1);
            }

            #ifdef CONFIG_FOO
            int foo_mode(int x)
            {
                return x * 2;
            }
            #endif

            #if defined(CONFIG_BAR) && !defined(CONFIG_FOO)
            int bar_only(void)
            {
                return foo_mode(9);
            }
            #endif
            """
        )
    )
    return tmp_path


def _defs(symbol, root, **kwargs):
    return json.loads(server.find_definition(symbol, root, **kwargs))


@requires_global
def test_guard_tagging_on_definitions(guarded_c_project):
    root = str(guarded_c_project)
    records = _defs("foo_mode", root)["results"]
    # gtags reports two definitions: the real one (CONFIG_FOO) and the
    # inline stub (!CONFIG_FOO); the header prototype is a reference.
    by_path = {(r["path"], r["line"]): r["guard"] for r in records}
    assert by_path == {
        ("feature.c", 9): ["CONFIG_FOO"],
        ("feature.h", 9): ["!CONFIG_FOO"],
    }

    # Unguarded symbol: scanned file, empty stack — include guard invisible.
    (unguarded,) = [
        r for r in _defs("always_here", root)["results"] if r["path"] == "feature.c"
    ]
    assert unguarded["guard"] == []


@requires_global
def test_guard_tagging_on_references(guarded_c_project):
    root = str(guarded_c_project)
    refs = json.loads(server.find_references("foo_mode", root))["results"]
    guards_by_line = {(r["path"], r["line"]): r["guard"] for r in refs}
    assert guards_by_line[("feature.c", 5)] == []  # call in always_here
    assert guards_by_line[("feature.c", 18)] == [
        "defined(CONFIG_BAR) && !defined(CONFIG_FOO)"
    ]
    assert guards_by_line[("feature.h", 7)] == ["CONFIG_FOO"]  # the prototype


@requires_global
def test_active_config_filters_definitions(guarded_c_project):
    root = str(guarded_c_project)
    on = _defs("foo_mode", root, active_config="CONFIG_FOO")
    assert [(r["path"], r["line"]) for r in on["results"]] == [("feature.c", 9)]
    assert on["config_filtered"] == 1  # the !CONFIG_FOO stub
    assert on["total"] == 1

    off = _defs("foo_mode", root, active_config="!CONFIG_FOO")
    assert [(r["path"], r["line"]) for r in off["results"]] == [("feature.h", 9)]
    assert off["config_filtered"] == 1  # the CONFIG_FOO definition


@requires_global
def test_active_config_dot_config_file(guarded_c_project):
    root = str(guarded_c_project)
    (guarded_c_project / "test.config").write_text("CONFIG_FOO=y\n")
    result = _defs("foo_mode", root, active_config="test.config")
    lines = {(r["path"], r["line"]) for r in result["results"]}
    assert lines == {("feature.c", 9)}  # the !CONFIG_FOO stub is dead

    # Closed world: CONFIG_BAR absent from the .config -> bar_only is dead.
    bar = _defs("bar_only", root, active_config="test.config")
    assert bar["results"] == [] and bar["config_filtered"] == 1


@requires_global
def test_active_config_bad_path_is_error(guarded_c_project):
    result = _defs("foo_mode", str(guarded_c_project), active_config="missing/.config")
    assert "error" in result and "not found" in result["error"]


@requires_global
def test_symbol_info_guard_card(guarded_c_project):
    root = str(guarded_c_project)
    card = json.loads(server.symbol_info("foo_mode", root))["results"]
    assert card["definition_count"] == 2
    assert card["guard_variants"] == 2  # CONFIG_FOO vs !CONFIG_FOO

    text = server.symbol_info("foo_mode", root, format="text")
    assert "2 definitions under 2 distinct guards:" in text
    assert "[CONFIG_FOO] defined at" in text
    assert "[!CONFIG_FOO] defined at" in text

    filtered = json.loads(
        server.symbol_info("foo_mode", root, active_config="CONFIG_FOO")
    )["results"]
    assert filtered["definition_count"] == 1
    assert filtered["config_filtered"] == 1
    assert filtered["guard_variants"] == 1

    none_live = server.symbol_info(
        "bar_only", root, format="text", active_config="CONFIG_FOO,CONFIG_BAR"
    )
    assert "no definition is live under active_config" in none_live


@requires_global
def test_symbol_info_single_guard_keeps_plain_card(c_project):
    text = server.symbol_info("add_numbers", str(c_project), format="text")
    assert "distinct guards" not in text
    assert "defined at util.c:3" in text


@requires_global
@pytest.mark.parametrize("how", ["config", "env", "flag"])
def test_guards_opt_out(guarded_c_project, monkeypatch, how):
    root = str(guarded_c_project)
    if how == "config":
        (guarded_c_project / config.PROJECT_CONFIG_NAME).write_text("guards = false\n")
    elif how == "env":
        monkeypatch.setenv("GTAGS_MCP_GUARDS", "0")
    else:
        monkeypatch.setattr(server, "_no_guards", True)

    def forbidden(*args, **kwargs):
        raise AssertionError("guards_for_file called despite opt-out")

    monkeypatch.setattr(guards, "guards_for_file", forbidden)
    records = _defs("foo_mode", root)["results"]
    assert all(r["guard"] is None for r in records)

    # Explicit active_config with guards disabled is an error, not a no-op.
    result = _defs("foo_mode", root, active_config="CONFIG_FOO")
    assert "error" in result and "guard scanning" in result["error"]

    info = json.loads(server.symbol_info("foo_mode", root))["results"]
    assert info["guard_variants"] is None


@requires_global
def test_guards_track_file_edits(guarded_c_project):
    root = str(guarded_c_project)
    before = {
        (r["path"], r["line"]): r["guard"] for r in _defs("foo_mode", root)["results"]
    }
    assert before[("feature.c", 9)] == ["CONFIG_FOO"]

    source = (guarded_c_project / "feature.c").read_text()
    (guarded_c_project / "feature.c").write_text(
        source.replace("#ifdef CONFIG_FOO", "#ifdef CONFIG_NEW_NAME")
    )
    stat = (guarded_c_project / "feature.c").stat()
    os.utime(
        guarded_c_project / "feature.c", (stat.st_atime + 2, stat.st_mtime + 2)
    )
    server.update_index(root)
    after = {
        (r["path"], r["line"]): r["guard"] for r in _defs("foo_mode", root)["results"]
    }
    assert after[("feature.c", 9)] == ["CONFIG_NEW_NAME"]


# ---------------------------------------------------------------------------
# Index database location: .gtags-mcp/ inside the root (legacy GTAGS honored)
# ---------------------------------------------------------------------------


@requires_global
def test_legacy_root_index_respected(c_project):
    """A pre-existing root-level GTAGS keeps being used — no .gtags-mcp dir."""
    root = str(c_project)
    # Build an old-style root-level index the way pre-0.9.1 versions did.
    files = "util.h\nutil.c\nmain.c\n"
    _, stderr, code = server._run(
        ["gtags", "--skip-unreadable", "-f", "-"], c_project, input_text=files
    )
    assert code == 0, stderr
    assert (c_project / "GTAGS").is_file()

    result = json.loads(server.find_definition("add_numbers", root))
    assert result["results"][0]["path"] == "util.c"
    assert not (c_project / server.INDEX_DIR_NAME).exists()

    # Incremental refresh also stays root-level for legacy indexes.
    (c_project / "extra.c").write_text("int extra_fn(void) { return 1; }\n")
    server.update_index(root)
    assert "extra.c" in server.find_definition("extra_fn", root)
    assert not (c_project / server.INDEX_DIR_NAME).exists()


@requires_global
def test_index_dir_never_indexed(c_project):
    """The .gtags-mcp database itself must not appear in any results."""
    root = str(c_project)
    server.find_definition("add_numbers", root)  # builds .gtags-mcp/
    server.update_index(root, full=True)  # full rebuild with the dir already present

    paths, _, err = server._raw_global(["-P"], root)
    assert err is None and paths.strip()
    assert server.INDEX_DIR_NAME not in paths


@requires_global
def test_index_dir_never_indexed_in_git_repo(git_project):
    root = str(git_project)
    server.find_definition("add_numbers", root)
    server.update_index(root, full=True)
    paths, _, err = server._raw_global(["-P"], root)
    assert err is None and paths.strip()
    assert server.INDEX_DIR_NAME not in paths
    # git must not see the index either (self-gitignoring directory).
    status = subprocess.run(
        ["git", "-C", root, "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert server.INDEX_DIR_NAME not in status


@requires_global
def test_root_autodetected_via_index_dir(c_project, monkeypatch):
    """A previously indexed non-git root is found from a subdirectory."""
    root = str(c_project)
    server.find_definition("add_numbers", root)  # creates .gtags-mcp/GTAGS
    assert not (c_project / ".git").exists()
    subdir = c_project / "sub" / "deeper"
    subdir.mkdir(parents=True)
    monkeypatch.chdir(subdir)
    result = json.loads(server.find_definition("add_numbers"))
    assert result["root"] == str(c_project.resolve())


def test_mcp_tool_schemas_stable():
    """The async roots wrapper must not change the registered tool schemas."""
    import anyio

    tools = {t.name: t for t in anyio.run(server.mcp.list_tools)}
    assert len(tools) == 11
    props = tools["find_definition"].inputSchema["properties"]
    assert {
        "symbol", "project_root", "case_insensitive",
        "limit", "offset", "format", "active_config",
    } <= set(props)
    assert tools["find_definition"].inputSchema["required"] == ["symbol"]
    # Every tool keeps the per-call project_root escape hatch.
    for name, tool in tools.items():
        assert "project_root" in tool.inputSchema["properties"], name
