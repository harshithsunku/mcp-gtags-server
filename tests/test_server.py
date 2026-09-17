"""End-to-end tests for the gtags MCP tools against a tiny C project."""

import json
import os
import subprocess
import textwrap

import pytest

from gtags_mcp import config, enrich, guards, output, server, toolchain

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
    for build in list(server._builds.values()):
        build.done.wait(timeout=60)
    for thread in list(server._refresh_threads.values()):
        thread.join(timeout=30)
    server._build_errors.clear()
    server._last_update.clear()
    server._update_cost.clear()
    server._refresh_errors.clear()
    server._refresh_threads.clear()
    server._index_generation.clear()
    server._reset_fx_cache()


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
    usages = json.loads(server.find_references("printf", root, format="json"))
    assert usages["fallback"] == "symbol_usages"
    assert any(r["path"] == "main.c" for r in usages["results"])


@requires_global
def test_definition_miss_carries_prefix_suggestions(c_project):
    root = str(c_project)
    miss = json.loads(server.find_definition("add_", root, format="json"))
    assert miss["results"] == []
    assert "add_numbers" in miss["suggestions"]
    text = server.find_definition("add_", root)
    assert "similar symbols: add_numbers" in text
    assert "0 definitions" not in text  # no empty summary header on a miss


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


def test_loader_hint():
    hint = server._loader_hint("global: /lib64/libc.so.6: version `GLIBC_2.34' not found")
    assert "setup --force" in hint
    assert server._loader_hint("GTAGS not found") == ""


def test_glibc_failure_gets_remediation_hint(c_project, monkeypatch):
    """A toolchain binary rejected by an old glibc must surface a fix hint,
    not just the raw loader error (the RHEL 8 scenario)."""
    loader_error = "gtags: /lib64/libc.so.6: version `GLIBC_2.34' not found"

    def glibc_broken_run(args, cwd, timeout=server.QUERY_TIMEOUT_SECONDS, **kwargs):
        return "", loader_error, 1

    monkeypatch.setattr(server, "_run", glibc_broken_run)
    result = server.find_definition("add_numbers", str(c_project))
    assert "GLIBC_2.34" in result
    assert "setup --force" in result


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
    total = json.loads(server.list_file_symbols("many.c", root, format="json"))["total"]
    assert total >= 3

    page = server.list_file_symbols("many.c", root, limit=2)
    assert f"[1-2 of {total} · offset=2 for more]" in page
    assert len([line for line in page.splitlines() if line[:1].isdigit()]) == 2

    page2 = server.list_file_symbols("many.c", root, limit=2, offset=2)
    assert f"[3-{min(4, total)} of {total}" in page2

    past_end = server.list_file_symbols("many.c", root, offset=999)
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

    result = server.find_definition("long_named_fn", root)
    assert "long.c" in result
    # Row = "path:line: " + snippet (capped) + tags — never the 500-char line.
    assert all(len(line) <= output.MAX_SNIPPET_CHARS + 80 for line in result.splitlines())

    record = json.loads(server.find_definition("long_named_fn", root, format="json"))["results"][0]
    assert len(record["snippet"]) <= output.MAX_SNIPPET_CHARS + 4


@requires_global
def test_get_symbol_body_returns_only_the_function(c_project):
    root = str(c_project)
    body = server.get_symbol_body("add_numbers", root)
    assert "== util.c:3 ==" in body
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
    result = server.find_callers("add_numbers", root)
    # gtags also counts the util.h prototype as a reference; the call from
    # main() must be attributed to the enclosing function `main`, shown with
    # the call site's source so the agent needs no follow-up grep.
    assert 'main  main.c:7: printf("%d\\n", add_numbers(2, 3));' in result

    callers = json.loads(server.find_callers("add_numbers", root, format="json"))["results"]
    main = next(c for c in callers if c["caller"] == "main")
    assert main["path"] == "main.c" and main["sites"] == [7]
    assert "add_numbers(2, 3)" in main["call"]


@requires_global
def test_find_references_grouped_by_file(c_project):
    root = str(c_project)
    result = server.find_references("add_numbers", root, group_by="file")
    assert "add_numbers: 2 references in 2 files" in result
    assert "main.c" in result and "util.h" in result

    summary = json.loads(
        server.find_references("add_numbers", root, group_by="file", format="json")
    )
    assert summary["grouped_by"] == "file"
    assert summary["total_references"] == 2
    assert {r["path"] for r in summary["results"]} == {"main.c", "util.h"}


@requires_global
def test_find_callees(c_project):
    root = str(c_project)
    result = server.find_callees("main", root)
    assert "  add_numbers  util.c:3" in result
    assert "external/unresolved: printf" in result

    callees = json.loads(server.find_callees("main", root, format="json"))["results"]
    assert {"symbol": "add_numbers", "path": "util.c", "line": 3} in callees["in_tree"]
    assert "printf" in callees["external"]


@requires_global
def test_find_definition_usage_summary(c_project):
    root = str(c_project)
    result = server.find_definition("add_numbers", root)
    assert result.splitlines()[0].startswith("add_numbers: 1 definition · 2 refs in 2 files")
    assert "util.c:3: int add_numbers(int a, int b)" in result
    assert "next: get_symbol_body" in result

    info = json.loads(server.find_definition("add_numbers", root, format="json"))
    assert info["results"][0]["path"] == "util.c"
    assert info["reference_count"] == 2 and info["file_count"] == 2
    assert info["definition_count"] == 1
    assert {t["path"] for t in info["top_files"]} == {"main.c", "util.h"}
    assert "get_symbol_body" in info["next_tools"]

    # case_insensitive may match several names: no single-symbol summary.
    ci = json.loads(
        server.find_definition("ADD_NUMBERS", root, case_insensitive=True, format="json")
    )
    assert "reference_count" not in ci


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
    result = json.loads(server.find_definition("main", "/nonexistent/path/xyz", format="json"))
    assert result["error"].startswith("Error")
    assert result["next_tools"]
    text = server.find_definition("main", "/nonexistent/path/xyz", format="text")
    assert text.startswith("Error")


@requires_global
def test_no_match_message(c_project):
    root = str(c_project)
    result = json.loads(server.find_definition("does_not_exist_anywhere", root, format="json"))
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
    result = json.loads(server.find_definition("add_numbers", str(c_project), format="json"))
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
    hit = json.loads(server.find_definition("add_numbers", root, format="json"))
    assert "get_symbol_body" in hit["next_tools"]
    miss = json.loads(server.find_definition("no_such_symbol", root, format="json"))
    assert "find_references" in miss["next_tools"]


@requires_global
def test_json_pagination(many_symbols_project):
    root = str(many_symbols_project)
    full = json.loads(server.list_file_symbols("many.c", root, limit=100, format="json"))
    total = full["total"]
    assert total >= 3 and full["truncated"] is False

    page = json.loads(server.list_file_symbols("many.c", root, limit=2, format="json"))
    assert len(page["results"]) == 2
    assert page["total"] == total and page["truncated"] is True

    page2 = json.loads(server.list_file_symbols("many.c", root, limit=2, offset=2, format="json"))
    assert page2["offset"] == 2
    assert page2["results"][0] == full["results"][2]

    past_end = json.loads(server.list_file_symbols("many.c", root, offset=999, format="json"))
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
    result = json.loads(server.find_definition("generated_fn", root, format="json"))
    assert result["results"] == []
    paths, _, err = server._raw_global(["-P"], root)
    assert err is None and "build/generated.c" not in paths


@requires_global
def test_newly_ignored_file_dropped_on_refresh(git_project):
    root = str(git_project)
    server.find_definition("add_numbers", root)  # builds index
    (git_project / ".gitignore").write_text("build/\nutil.c\n")
    server.update_index(root)
    result = json.loads(server.find_definition("add_numbers", root, format="json"))
    assert result["results"] == []


@requires_global
def test_skip_globs_config(c_project):
    (c_project / "skipped.gen.c").write_text("int from_generator(void) { return 1; }\n")
    (c_project / config.PROJECT_CONFIG_NAME).write_text('skip_globs = ["*.gen.c"]\n')
    root = str(c_project)
    assert "util.c" in server.find_definition("add_numbers", root)
    result = json.loads(server.find_definition("from_generator", root, format="json"))
    assert result["results"] == []


@requires_global
def test_root_autodetected_from_subdirectory(c_project, monkeypatch):
    root = str(c_project)
    server.find_definition("add_numbers", root)  # builds GTAGS at the root
    subdir = c_project / "nested" / "deeper"
    subdir.mkdir(parents=True)
    monkeypatch.chdir(subdir)
    result = json.loads(server.find_definition("add_numbers", format="json"))
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
    records = json.loads(server.find_definition(symbol, root, format="json"))["results"]
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
    result = json.loads(server.list_file_symbols("types.c", str(rich_c_project), format="json"))
    kinds = {r["symbol"]: r["kind"] for r in result["results"]}
    assert kinds.get("process_items") == "function"
    assert kinds.get("item_t") == "typedef"
    assert kinds.get("SQUARE") == "macro"


@requires_global
@requires_ctags_json
def test_find_definition_rows_enriched(rich_c_project):
    root = str(rich_c_project)
    info = json.loads(server.find_definition("process_items", root, format="json"))
    definition = info["results"][0]
    assert definition["kind"] == "function"
    assert definition["typeref"] == "int"
    assert "struct item" in definition["signature"]

    assert "[function]" in server.find_definition("process_items", root)
    assert "[enumerator]" in server.find_definition("COLOR_GREEN", root)
    assert "[typedef]" in server.find_definition("item_t", root)


@requires_global
@requires_ctags_json
def test_references_stay_unenriched(rich_c_project):
    root = str(rich_c_project)
    refs = json.loads(server.find_references("SQUARE", root, format="json"))["results"]
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
    return json.loads(server.find_definition(symbol, root, **kwargs, format="json"))


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
    refs = json.loads(server.find_references("foo_mode", root, format="json"))["results"]
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
def test_find_definition_guard_variants(guarded_c_project):
    root = str(guarded_c_project)
    info = json.loads(server.find_definition("foo_mode", root, format="json"))
    assert info["definition_count"] == 2
    assert info["guard_variants"] == 2  # CONFIG_FOO vs !CONFIG_FOO

    text = server.find_definition("foo_mode", root)
    assert "foo_mode: 2 definitions under 2 #if variants" in text
    assert "[#if CONFIG_FOO]" in text or "; #if CONFIG_FOO]" in text
    assert "#if !CONFIG_FOO]" in text

    filtered = json.loads(
        server.find_definition("foo_mode", root, active_config="CONFIG_FOO", format="json")
    )
    assert filtered["definition_count"] == 1
    assert filtered["config_filtered"] == 1
    assert filtered["guard_variants"] == 1

    none_live = server.find_definition(
        "bar_only", root, active_config="CONFIG_FOO,CONFIG_BAR"
    )
    assert "filtered out by active_config" in none_live
    assert "No definition found" in none_live


@requires_global
def test_find_definition_single_guard_keeps_plain_header(c_project):
    text = server.find_definition("add_numbers", str(c_project))
    assert "#if variants" not in text
    assert "util.c:3:" in text


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

    info = json.loads(server.find_definition("foo_mode", root, format="json"))
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

    result = json.loads(server.find_definition("add_numbers", root, format="json"))
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
    result = json.loads(server.find_definition("add_numbers", format="json"))
    assert result["root"] == str(c_project.resolve())


# ---------------------------------------------------------------------------
# Stability pass: per-tool edge cases
# ---------------------------------------------------------------------------


@requires_global
def test_suggestions_capped_and_absent_on_hit(c_project):
    (c_project / "sugg.c").write_text(
        "".join(f"int sugg_fn_{i:02d}(void) {{ return {i}; }}\n" for i in range(12))
    )
    root = str(c_project)
    miss = json.loads(server.find_definition("sugg_fn", root, format="json"))
    assert miss["results"] == []
    assert len(miss["suggestions"]) == server.MAX_SUGGESTIONS
    assert all(s.startswith("sugg_fn_") for s in miss["suggestions"])

    hit = json.loads(server.find_definition("add_numbers", root, format="json"))
    assert hit["results"] and "suggestions" not in hit


@requires_global
def test_suggestions_exclude_exact_name(c_project):
    """A config-filtered miss must not suggest the queried name itself."""
    (c_project / "gated.c").write_text(
        "#ifdef CONFIG_ONLY\nint gated_fn(void) { return 1; }\n#endif\n"
        "int gated_fn_helper(void) { return 2; }\n"
    )
    root = str(c_project)
    miss = json.loads(
        server.find_definition("gated_fn", root, active_config="!CONFIG_ONLY", format="json")
    )
    assert miss["results"] == [] and miss["config_filtered"] == 1
    assert "gated_fn" not in miss["suggestions"]
    assert "gated_fn_helper" in miss["suggestions"]


@requires_global
def test_references_no_fallback_flag_on_real_references(c_project):
    refs = json.loads(server.find_references("add_numbers", str(c_project), format="json"))
    assert refs["results"] and "fallback" not in refs


@requires_global
def test_references_fallback_preserves_case_insensitive(c_project):
    root = str(c_project)
    sensitive = json.loads(server.find_references("PRINTF", root, format="json"))
    assert sensitive["results"] == [] and "fallback" not in sensitive
    ci = json.loads(server.find_references("PRINTF", root, case_insensitive=True, format="json"))
    assert ci["fallback"] == "symbol_usages"
    assert any(r["path"] == "main.c" for r in ci["results"])


@requires_global
def test_references_fallback_guards_and_config_filter(c_project):
    """Fallback (symbol-usage) records still carry guard stacks and honor
    active_config filtering, exactly like real reference records."""
    (c_project / "log.c").write_text(
        '#include "util.h"\n#ifdef CONFIG_LOGGING\n'
        "static void log_it(void) { external_log_fn(1); }\n#endif\n"
    )
    root = str(c_project)
    data = json.loads(server.find_references("external_log_fn", root, format="json"))
    assert data["fallback"] == "symbol_usages"
    (rec,) = data["results"]
    assert rec["guard"] == ["CONFIG_LOGGING"]

    filtered = json.loads(
        server.find_references(
            "external_log_fn", root, active_config="!CONFIG_LOGGING", format="json"
        )
    )
    assert filtered["fallback"] == "symbol_usages"
    assert filtered["results"] == [] and filtered["config_filtered"] == 1


@requires_global
def test_get_symbol_body_struct(c_project):
    (c_project / "shapes.h").write_text(
        "struct shape {\n    int width;\n    int height;\n};\n\nint after_struct;\n"
    )
    body = server.get_symbol_body("shape", str(c_project))
    assert "int width;" in body and "int height;" in body
    assert "};" in body
    assert "after_struct" not in body


def test_extract_body_prototype_stops_at_semicolon(tmp_path):
    proto = tmp_path / "p.h"
    proto.write_text("int proto_fn(int a,\n             int b);\nint other;\n")
    body = server._extract_body(proto, 1)
    assert body == ["int proto_fn(int a,", "             int b);"]


@requires_global
def test_get_symbol_body_truncates_at_max_lines(c_project, monkeypatch):
    monkeypatch.setattr(server, "MAX_BODY_LINES", 8)
    lines = "".join(f"    x += {i};\n" for i in range(30))
    (c_project / "big.c").write_text(f"int big_fn(int x)\n{{\n{lines}    return x;\n}}\n")
    data = json.loads(server.get_symbol_body("big_fn", str(c_project), format="json"))
    body = data["results"][0]["body"]
    assert "... body truncated at 8 lines ..." in body
    assert len(body.splitlines()) == 9  # 8 body lines + the marker


@requires_global
def test_get_symbol_body_omitted_definitions(guarded_c_project):
    root = str(guarded_c_project)
    data = json.loads(server.get_symbol_body("foo_mode", root, max_definitions=1, format="json"))
    assert data["total"] == 2 and data["truncated"] is True
    assert data["omitted_definitions"] == 1
    assert len(data["results"]) == 1
    text = server.get_symbol_body("foo_mode", root, max_definitions=1, format="text")
    assert "1 more definition(s) not shown" in text


@requires_global
def test_find_callers_ranked_by_call_sites(c_project):
    (c_project / "heavy.c").write_text(
        '#include "util.h"\n'
        "void heavy_user(void)\n{\n"
        "    add_numbers(1, 1);\n"
        "    add_numbers(2, 2);\n"
        "    add_numbers(3, 3);\n"
        "}\n"
    )
    data = json.loads(server.find_callers("add_numbers", str(c_project), format="json"))
    top = data["results"][0]
    assert top["caller"] == "heavy_user" and len(top["sites"]) == 3
    site_counts = [len(r["sites"]) for r in data["results"]]
    assert site_counts == sorted(site_counts, reverse=True)


@requires_global
def test_find_callers_file_scope_attribution(c_project):
    """A reference in a file with no definitions maps to '(file scope)'."""
    (c_project / "decl.c").write_text("int add_numbers(int a, int b);\n")
    data = json.loads(server.find_callers("add_numbers", str(c_project), format="json"))
    scoped = [r for r in data["results"] if r["path"] == "decl.c"]
    assert scoped == [
        {"caller": "(file scope)", "path": "decl.c", "sites": [1],
         "call": "int add_numbers(int a, int b);"}
    ]


@requires_global
def test_find_callers_breadth_guard(c_project, monkeypatch):
    """>500 referencing files aborts with the find_references hint."""
    root = str(c_project)
    server.find_definition("add_numbers", root)  # build the index first
    resolved = c_project.resolve()
    real_raw = server._raw_global

    def fake_raw(flags, project_root, _retry=True):
        if flags and flags[0] == "-rx":
            out = "\n".join(
                f"wide_sym 1 dir/file_{i:04d}.c wide_sym();" for i in range(501)
            )
            return out, resolved, None
        return real_raw(flags, project_root, _retry)

    monkeypatch.setattr(server, "_raw_global", fake_raw)
    data = json.loads(server.find_callers("wide_sym", root, format="json"))
    assert "too broad" in data["error"]
    assert "501 files" in data["error"]
    assert data["next_tools"] == ["find_references"]
    text = server.find_callers("wide_sym", root)
    assert "too broad" in text and "next: find_references" in text


@requires_global
def test_find_references_file_grouping_sort_order(c_project):
    (c_project / "hot.c").write_text(
        '#include "util.h"\n'
        "int hot_a(void)\n{\n    return add_numbers(1, 1);\n}\n"
        "int hot_b(void)\n{\n    return add_numbers(2, 2);\n}\n"
    )
    data = json.loads(
        server.find_references("add_numbers", str(c_project), group_by="file", format="json")
    )
    # Count desc, then path asc: hot.c (2 refs) first, then main.c / util.h (1 each).
    assert [r["path"] for r in data["results"]] == ["hot.c", "main.c", "util.h"]
    assert [r["count"] for r in data["results"]] == [2, 1, 1]
    assert data["total_references"] == 4


@requires_global
def test_find_references_auto_grouping_and_path_prefix(c_project, monkeypatch):
    root = str(c_project)
    (c_project / "sub").mkdir()
    (c_project / "sub" / "more.c").write_text(
        '#include "../util.h"\nint more(void)\n{\n    return add_numbers(3, 3);\n}\n'
    )
    server.update_index(root)
    # Below the threshold, auto lists individual sites...
    lines = json.loads(server.find_references("add_numbers", root, format="json"))
    assert "grouped_by" not in lines and lines["total"] == 3
    # ...above it, auto switches to per-file counts.
    monkeypatch.setattr(server, "REFERENCE_GROUP_THRESHOLD", 2)
    grouped = json.loads(server.find_references("add_numbers", root, format="json"))
    assert grouped["grouped_by"] == "file" and grouped["total_references"] == 3
    # group_by="line" always wins; path_prefix narrows to one directory.
    narrowed = json.loads(
        server.find_references(
            "add_numbers", root, group_by="line", path_prefix="sub/", format="json"
        )
    )
    assert [r["path"] for r in narrowed["results"]] == ["sub/more.c"]
    assert narrowed["path_prefix"] == "sub"
    text = server.find_references("add_numbers", root, group_by="line", path_prefix="sub")
    assert "add_numbers: 1 reference under sub/" in text


@requires_global
def test_find_callees_caps_call_targets(c_project):
    calls = "".join(f"    t{i:02d}(x);\n" for i in range(45))
    (c_project / "wide.c").write_text(f"void wide_fn(int x)\n{{\n{calls}}}\n")
    data = json.loads(server.find_callees("wide_fn", str(c_project), format="json"))
    res = data["results"]
    assert data["truncated"] is True
    assert data["capped_call_targets"] == 5
    assert len(res["in_tree"]) + len(res["external"]) == 40


@requires_global
def test_find_callees_filters_keywords_and_self(c_project):
    (c_project / "kw.c").write_text(
        '#include "util.h"\n'
        "int keywordy(int x)\n{\n"
        "    if (x > 0) {\n"
        "        while (x--) { }\n"
        "        return (int)sizeof(int) + add_numbers(x, x);\n"
        "    }\n"
        "    for (;;) break;\n"
        "    switch (x) { default: break; }\n"
        "    return keywordy(x - 1);\n"
        "}\n"
    )
    data = json.loads(server.find_callees("keywordy", str(c_project), format="json"))
    res = data["results"]
    assert [c["symbol"] for c in res["in_tree"]] == ["add_numbers"]
    assert res["external"] == []  # keywords and the self-call are filtered


@requires_global
def test_find_definition_exported_detection(c_project):
    source = (c_project / "util.c").read_text()
    (c_project / "util.c").write_text(source + "EXPORT_SYMBOL(add_numbers);\n")
    info = json.loads(server.find_definition("add_numbers", str(c_project), format="json"))
    assert info["exported"] == "EXPORT_SYMBOL"
    assert "exported via EXPORT_SYMBOL" in server.find_definition("add_numbers", str(c_project))


@requires_global
def test_find_definition_config_kills_all_definitions(guarded_c_project):
    root = str(guarded_c_project)
    info = json.loads(
        server.find_definition("bar_only", root, active_config="CONFIG_FOO", format="json")
    )
    assert info["definition_count"] == 0 and info["config_filtered"] == 1
    assert info["results"] == []
    text = server.find_definition("bar_only", root, active_config="CONFIG_FOO")
    assert "(1 filtered out by active_config)" in text


@requires_global
def test_update_index_full_rebuild_keeps_legacy_location(c_project):
    """full=True on a legacy root-level index rebuilds in place."""
    root = str(c_project)
    files = "util.h\nutil.c\nmain.c\n"
    _, stderr, code = server._run(
        ["gtags", "--skip-unreadable", "-f", "-"], c_project, input_text=files
    )
    assert code == 0, stderr

    result = server.update_index(root, full=True)
    assert "legacy root-level index" in result
    assert (c_project / "GTAGS").is_file()
    assert not (c_project / server.INDEX_DIR_NAME).exists()
    assert "util.c" in server.find_definition("add_numbers", root)


@requires_global
def test_update_index_full_failure_keeps_envelope_and_recovers(c_project, monkeypatch):
    root = str(c_project)
    server.find_definition("add_numbers", root)  # healthy build first
    monkeypatch.setattr(
        server, "_run_index", lambda root_, incremental: ("", "simulated gtags crash", 1)
    )
    data = json.loads(server.update_index(root, full=True, format="json"))
    assert set(data) == {"tool", "root", "error", "next_tools"}
    assert "simulated gtags crash" in data["error"]
    monkeypatch.undo()
    # The failed rebuild must not strand state: the next query rebuilds.
    assert "util.c" in server.find_definition("add_numbers", root)


# ---------------------------------------------------------------------------
# ctags export recovery: definitions the index parser missed
# ---------------------------------------------------------------------------


@pytest.fixture
def export_gap_project(tmp_path):
    """Reproduces the GNU Global parser derail: the sparse-annotated forward
    declaration makes gtags miss foo_lock's real definition entirely; the
    EXPORT_SYMBOL line is the recovery signal."""
    (tmp_path / "foo.c").write_text(
        textwrap.dedent(
            """\
            struct foo { int held; };

            static void __sched __foo_lock_slowpath(struct foo *lock) __acquires(lock);

            void __sched foo_lock(struct foo *lock)
            {
                lock->held = 1;
            }
            EXPORT_SYMBOL(foo_lock);
            """
        )
    )
    (tmp_path / "user.c").write_text(
        "struct foo;\nvoid use_it(struct foo *f)\n{\n    foo_lock(f);\n}\n"
    )
    return tmp_path


def _skip_if_parser_fixed(data):
    if data["results"] and "resolved_via" not in data:
        pytest.skip("GNU Global's parser no longer derails on this fixture")


@requires_global
@requires_ctags_json
def test_export_recovery_finds_parser_missed_definition(export_gap_project):
    root = str(export_gap_project)
    data = json.loads(server.find_definition("foo_lock", root, format="json"))
    _skip_if_parser_fixed(data)
    assert data["resolved_via"] == "ctags:EXPORT_SYMBOL"
    top = data["results"][0]
    assert top["path"] == "foo.c" and top["line"] == 5
    assert top["kind"] == "function"  # enriched from the same (cached) ctags run
    assert "foo_lock(struct foo *lock)" in top["snippet"]

    text = server.find_definition("foo_lock", root)
    assert "(resolved via ctags:EXPORT_SYMBOL)" in text


@requires_global
@requires_ctags_json
def test_definition_summary_includes_recovered_definition(export_gap_project):
    root = str(export_gap_project)
    info = json.loads(server.find_definition("foo_lock", root, format="json"))
    if info["results"] and not info.get("resolved_via"):
        pytest.skip("GNU Global's parser no longer derails on this fixture")
    assert info["resolved_via"] == "ctags:EXPORT_SYMBOL"
    assert info["exported"] == "EXPORT_SYMBOL"
    assert info["definition_count"] >= 1
    assert info["results"][0]["path"] == "foo.c"


@requires_global
@requires_ctags_json
def test_get_symbol_body_and_callees_read_recovered_definition(export_gap_project):
    root = str(export_gap_project)
    data = json.loads(server.get_symbol_body("foo_lock", root, format="json"))
    _skip_if_parser_fixed(data)
    assert data["resolved_via"] == "ctags:EXPORT_SYMBOL"
    assert "lock->held = 1;" in data["results"][0]["body"]
    text = server.get_symbol_body("foo_lock", root, format="text")
    assert "(resolved via ctags:EXPORT_SYMBOL)" in text

    callees = json.loads(server.find_callees("foo_lock", root, format="json"))
    assert callees["resolved_via"] == "ctags:EXPORT_SYMBOL"
    assert callees["definition"] == {"path": "foo.c", "line": 5}


@requires_global
@requires_ctags_json
def test_export_recovery_respects_enrich_optout(export_gap_project, monkeypatch):
    monkeypatch.setattr(server, "_no_enrich", True)
    data = json.loads(server.find_definition("foo_lock", str(export_gap_project), format="json"))
    assert "resolved_via" not in data


@requires_global
def test_export_recovery_inert_on_plain_projects(c_project):
    data = json.loads(server.find_definition("add_numbers", str(c_project), format="json"))
    assert "resolved_via" not in data


# ---------------------------------------------------------------------------
# Cross-cutting: every tool keeps the envelope contract, hit or error
# ---------------------------------------------------------------------------

ALL_TOOLS = [
    ("find_definition", {"symbol": "add_numbers"}),
    ("find_references", {"symbol": "add_numbers"}),
    ("get_symbol_body", {"symbol": "add_numbers"}),
    ("find_callers", {"symbol": "add_numbers"}),
    ("find_callees", {"symbol": "main"}),
    ("reachability", {"from_symbol": "main", "to_symbol": "add_numbers"}),
    ("list_file_symbols", {"file_path": "util.c"}),
    ("update_index", {"full": True}),
]


@pytest.fixture
def committed_project(git_project):
    """git_project with everything committed (blast_radius needs a HEAD)."""
    for args in (["add", "-A"], ["commit", "-qm", "base"]):
        subprocess.run(
            ["git", "-C", str(git_project), "-c", "user.name=t",
             "-c", "user.email=t@t", *args],
            check=True,
            capture_output=True,
        )
    return git_project


@requires_global
@pytest.mark.parametrize("tool_name,args", ALL_TOOLS, ids=[t[0] for t in ALL_TOOLS])
def test_envelope_contract_all_tools(committed_project, tool_name, args):
    root = str(committed_project)
    data = json.loads(getattr(server, tool_name)(project_root=root, **args, format="json"))
    assert data["tool"] == tool_name
    assert data["root"] == str(committed_project.resolve())
    assert "results" in data and "error" not in data
    assert isinstance(data["next_tools"], list)


@requires_global
@pytest.mark.parametrize("tool_name,args", ALL_TOOLS, ids=[t[0] for t in ALL_TOOLS])
def test_envelope_error_contract_all_tools(tool_name, args):
    bad_root = "/nonexistent/path/xyz"
    data = json.loads(getattr(server, tool_name)(project_root=bad_root, **args, format="json"))
    assert data["tool"] == tool_name
    assert data["error"].startswith("Error")
    assert "results" not in data
    assert isinstance(data["next_tools"], list)
    text = getattr(server, tool_name)(project_root=bad_root, **args)
    assert isinstance(text, str) and text.startswith("Error")


@requires_global
@pytest.mark.parametrize("tool_name,args", ALL_TOOLS, ids=[t[0] for t in ALL_TOOLS])
def test_text_is_default_and_renders_every_tool(committed_project, tool_name, args):
    root = str(committed_project)
    text = getattr(server, tool_name)(project_root=root, **args)
    assert text and not text.lstrip().startswith("{")  # compact text, not JSON
    assert text == output.render_text(
        json.loads(getattr(server, tool_name)(project_root=root, **args, format="json"))
    )


@requires_global
def test_concurrent_queries_during_refresh(many_symbols_project):
    """8 threads x mixed queries racing a background refresh: no exceptions,
    every response is a valid envelope."""
    from concurrent.futures import ThreadPoolExecutor

    root = str(many_symbols_project)
    server.find_definition("add_numbers", root)  # build the index
    server._last_update.clear()  # the next query kicks a background refresh

    calls = [
        lambda: server.find_definition("add_numbers", root, format="json"),
        lambda: server.find_references("add_numbers", root, format="json"),
        lambda: server.find_references("add_numbers", root, group_by="file", format="json"),
        lambda: server.find_callers("add_numbers", root, format="json"),
        lambda: server.list_file_symbols("util.c", root, format="json"),
        lambda: server.get_symbol_body("add_numbers", root, format="json"),
        lambda: server.find_callees("main", root, format="json"),
        lambda: server.find_definition("fn_a", root, format="json"),
    ]
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(call) for call in calls * 3]
        outputs = [f.result(timeout=60) for f in futures]
    for raw in outputs:
        data = json.loads(raw)
        assert data["tool"] and "results" in data and "error" not in data
    server._wait_for_refresh(many_symbols_project.resolve())


def test_mcp_tool_schemas_stable():
    """The async roots wrapper must not change the registered tool schemas."""
    import anyio

    tools = {t.name: t for t in anyio.run(server.mcp.list_tools)}
    assert set(tools) == {
        "find_definition", "find_references", "get_symbol_body", "find_callers",
        "find_callees", "reachability", "list_file_symbols", "update_index",
    }
    props = tools["find_definition"].input_schema["properties"]
    assert {
        "symbol", "project_root", "case_insensitive",
        "limit", "offset", "format", "active_config",
    } <= set(props)
    assert tools["find_definition"].input_schema["required"] == ["symbol"]
    for name, tool in tools.items():
        # Every tool keeps the per-call project_root escape hatch.
        assert "project_root" in tool.input_schema["properties"], name
        # Compact text is the default output; JSON on request.
        assert tool.input_schema["properties"]["format"]["default"] == "text", name
        # The injected Context stays out of the agent-facing schema.
        assert "ctx" not in tool.input_schema["properties"], name
        # Bodies return a string: no derived {"result": str} outputSchema,
        # or every response is sent twice.
        assert tool.output_schema is None, name
        assert tool.title, name


def test_mcp_tool_annotations():
    """Clients gate permissions / parallel calls on these hints."""
    import anyio

    tools = {t.name: t for t in anyio.run(server.mcp.list_tools)}
    for name, tool in tools.items():
        hints = tool.annotations
        assert hints is not None, name
        assert hints.open_world_hint is False, name
        assert hints.read_only_hint is (name != "update_index"), name
    update = tools["update_index"].annotations
    assert update.destructive_hint is False
    assert update.idempotent_hint is True


# ---------------------------------------------------------------------------
# Index lifecycle: single-flight full builds, live-call cold start, cwd guard
# ---------------------------------------------------------------------------


def _slow_full_builds(monkeypatch, delay: float) -> list[int]:
    """Count full (non-incremental) builds and make each take `delay` seconds."""
    import threading
    import time as _time

    builds: list[int] = []
    real_run_index = server._run_index

    def slow(root, incremental):
        if not incremental:
            builds.append(threading.get_ident())
            _time.sleep(delay)
        return real_run_index(root, incremental)

    monkeypatch.setattr(server, "_run_index", slow)
    return builds


@requires_global
def test_concurrent_first_queries_share_one_build(c_project, monkeypatch):
    """Regression (v1.5.0): parallel first queries on an unindexed repo each
    started their own `gtags` over the same database — "GTAGS not found",
    "chmod(2) failed", corruption. Now they join one single-flight build."""
    from concurrent.futures import ThreadPoolExecutor

    builds = _slow_full_builds(monkeypatch, 0.5)
    root = str(c_project)
    calls = [
        lambda: server.find_definition("add_numbers", root, format="json"),
        lambda: server.find_references("add_numbers", root, format="json"),
        lambda: server.find_callers("add_numbers", root, format="json"),
        lambda: server.get_symbol_body("add_numbers", root, format="json"),
        lambda: server.update_index(root, full=True, format="json"),
    ]
    with ThreadPoolExecutor(max_workers=8) as pool:
        outputs = [f.result(timeout=60) for f in [pool.submit(c) for c in calls * 2]]
    for raw in outputs:
        data = json.loads(raw)
        assert "error" not in data, data
    # update_index(full=True) may legitimately start ONE more build after the
    # first finished; what must never happen is overlapping builds.
    assert 1 <= len(builds) <= 3
    assert "util.c" in server.find_definition("add_numbers", root)


@requires_global
def test_live_call_cold_start_returns_retry_status(c_project, monkeypatch):
    """A live MCP call must not block past INDEX_BUILD_WAIT_SECONDS on a first
    build (clients enforce ~60 s timeouts); it gets a retry status instead."""
    import time as _time

    import anyio
    from mcp import Client

    _slow_full_builds(monkeypatch, 1.5)
    monkeypatch.setattr(server, "INDEX_BUILD_WAIT_SECONDS", 0.2)
    root = str(c_project)

    async def call():
        async with Client(server.mcp, mode="legacy") as client:
            return await client.call_tool(
                "find_definition", {"symbol": "add_numbers", "project_root": root}
            )

    started = _time.monotonic()
    first = anyio.run(call)
    assert _time.monotonic() - started < 1.2
    assert first.is_error is True
    assert "for the first time" in first.content[0].text
    assert "Retry this call shortly" in first.content[0].text

    server._builds[c_project.resolve()].done.wait(timeout=30)
    second = anyio.run(call)
    assert second.is_error is False
    assert "util.c:3:" in second.content[0].text

    # Direct (non-MCP) calls keep blocking until the build is done.
    assert server._live_mcp_call.get() is False


@requires_global
def test_failed_build_reported_once_then_retried(c_project, monkeypatch):
    calls: list[bool] = []
    real_run_index = server._run_index

    def failing_once(root, incremental):
        if not incremental and not calls:
            calls.append(True)
            return "", "gtags: simulated failure", 1
        return real_run_index(root, incremental)

    monkeypatch.setattr(server, "_run_index", failing_once)
    root = str(c_project)
    failed = json.loads(server.find_definition("add_numbers", root, format="json"))
    assert "simulated failure" in failed["error"]
    assert c_project.resolve() not in server._build_errors  # reported, not re-reported
    assert "util.c" in server.find_definition("add_numbers", root)  # retried


@requires_global
def test_cwd_fallback_disabled_asks_for_project_root(c_project, tmp_path_factory, monkeypatch):
    plugin_dir = tmp_path_factory.mktemp("plugin-install")  # not a repository
    monkeypatch.chdir(plugin_dir)
    monkeypatch.setenv("GTAGS_MCP_CWD_FALLBACK", "0")

    data = json.loads(server.find_definition("add_numbers", format="json"))
    assert "Pass project_root=<absolute path" in data["error"]
    assert not (plugin_dir / server.INDEX_DIR_NAME).exists()  # never indexed

    # An explicit root, or a repo marker walking up from cwd, still works.
    assert "util.c" in server.find_definition("add_numbers", str(c_project))
    (c_project / ".git").mkdir()
    monkeypatch.chdir(c_project)
    assert "util.c" in server.find_definition("add_numbers")

    monkeypatch.setattr(server, "_no_cwd_fallback", True)
    monkeypatch.delenv("GTAGS_MCP_CWD_FALLBACK")
    monkeypatch.chdir(plugin_dir)
    assert "project_root" in server.find_definition("add_numbers")
