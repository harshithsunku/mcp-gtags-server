"""Macro-family symbol resolution: unit tests plus end-to-end fixture tests."""

import json
import textwrap

import pytest

from gtags_mcp import config, enrich, guards, macros, server, toolchain

requires_global = pytest.mark.skipif(
    toolchain.find_global() is None or toolchain.find_gtags() is None,
    reason="GNU Global not installed",
)


# ---------------------------------------------------------------------------
# Unit tests: pure string work, no GNU Global needed.
# ---------------------------------------------------------------------------


def make_query(data):
    """Fake `global` runner: maps (flags...) tuples to canned cxref lists."""
    calls = []

    def query(flags):
        calls.append(flags)
        return data.get(tuple(flags), [])

    query.calls = calls
    return query


SYSCALL_DATA = {
    ("-x", "--", "SYSCALL_DEFINE3"): [
        ("SYSCALL_DEFINE3", 723, "fs/read_write.c",
         "SYSCALL_DEFINE3(read, unsigned int, fd, char __user *, buf, size_t, count)"),
        ("SYSCALL_DEFINE3", 1163, "fs/read_write.c",
         "SYSCALL_DEFINE3(readv, unsigned long, fd, const struct iovec __user *, vec,"),
    ],
}


@pytest.mark.parametrize(
    "queried",
    ["sys_read", "__x64_sys_read", "__ia32_sys_read", "__se_sys_read", "__do_sys_read"],
)
def test_syscall_family_resolves_all_arch_spellings(queried):
    hits, via = macros.resolve(queried, make_query(SYSCALL_DATA))
    assert via == "macro:SYSCALL_DEFINE"
    assert [(h[2], h[1]) for h in hits] == [("fs/read_write.c", 723)]
    assert hits[0][0] == queried  # records answer for the name that was asked


def test_syscall_family_does_not_match_other_first_args():
    hits, via = macros.resolve("sys_write", make_query(SYSCALL_DATA))
    assert hits == [] and via is None


def test_compat_family_wins_over_plain_sys():
    data = {
        ("-x", "--", "COMPAT_SYSCALL_DEFINE2"): [
            ("COMPAT_SYSCALL_DEFINE2", 10, "fs/ioctl.c",
             "COMPAT_SYSCALL_DEFINE2(ioctl, unsigned int, fd)"),
        ],
    }
    hits, via = macros.resolve("compat_sys_ioctl", make_query(data))
    assert via == "macro:COMPAT_SYSCALL_DEFINE"
    assert hits[0][2] == "fs/ioctl.c"


def test_trace_family_matches_first_and_second_arg():
    data = {
        ("-rx", "--", "TRACE_EVENT"): [
            ("TRACE_EVENT", 220, "include/trace/events/sched.h",
             "TRACE_EVENT(sched_switch,"),
        ],
        ("-rx", "--", "DEFINE_EVENT"): [
            ("DEFINE_EVENT", 300, "include/trace/events/sched.h",
             "DEFINE_EVENT(sched_wakeup_template, sched_wakeup,"),
        ],
    }
    hits, via = macros.resolve("trace_sched_switch", make_query(data))
    assert via == "macro:TRACE_EVENT"
    assert hits[0][1] == 220

    hits, via = macros.resolve("trace_sched_wakeup", make_query(data))
    assert via == "macro:TRACE_EVENT"
    assert hits[0][1] == 300


def test_prefix_family_runs_even_with_direct_hits():
    hits, via = macros.resolve(
        "sys_read", make_query(SYSCALL_DATA), direct_empty=False
    )
    assert via == "macro:SYSCALL_DEFINE"
    assert hits


def test_definer_scan_finds_bare_names():
    data = {
        ("-sx", "--", "css_set_lock"): [
            ("css_set_lock", 82, "kernel/cgroup/cgroup.c",
             "DEFINE_SPINLOCK(css_set_lock);"),
            ("css_set_lock", 500, "kernel/cgroup/cgroup.c",
             "\tspin_lock(&css_set_lock);"),
        ],
    }
    hits, via = macros.resolve("css_set_lock", make_query(data))
    assert via == "macro:DEFINE_SPINLOCK"
    assert [(h[2], h[1]) for h in hits] == [("kernel/cgroup/cgroup.c", 82)]


def test_definer_scan_handles_second_argument():
    data = {
        ("-rx", "--", "runqueues"): [
            ("runqueues", 131, "kernel/sched/core.c",
             "DEFINE_PER_CPU_SHARED_ALIGNED(struct rq, runqueues);"),
        ],
    }
    hits, via = macros.resolve("runqueues", make_query(data))
    assert via == "macro:DEFINE_PER_CPU_SHARED_ALIGNED"
    assert hits[0][1] == 131


def test_definer_scan_ranks_define_before_declare():
    data = {
        ("-sx", "--", "runqueues"): [
            ("runqueues", 2415, "include/linux/sched.h",
             "DECLARE_PER_CPU_SHARED_ALIGNED(struct rq, runqueues);"),
            ("runqueues", 131, "kernel/sched/core.c",
             "DEFINE_PER_CPU_SHARED_ALIGNED(struct rq, runqueues);"),
        ],
    }
    hits, via = macros.resolve("runqueues", make_query(data))
    assert via == "macro:DEFINE_PER_CPU_SHARED_ALIGNED"
    assert hits[0][2] == "kernel/sched/core.c"
    assert hits[1][2] == "include/linux/sched.h"


def test_definer_scan_skipped_when_direct_hits_exist():
    data = {
        ("-sx", "--", "css_set_lock"): [
            ("css_set_lock", 82, "kernel/cgroup/cgroup.c",
             "DEFINE_SPINLOCK(css_set_lock);"),
        ],
    }
    query = make_query(data)
    hits, via = macros.resolve("css_set_lock", query, direct_empty=False)
    assert hits == [] and via is None
    assert query.calls == []  # bare names skip the family scan entirely


def test_export_symbol_is_not_a_definition():
    data = {
        ("-rx", "--", "phantom_fn"): [
            ("phantom_fn", 40, "lib/foo.c", "EXPORT_SYMBOL_GPL(phantom_fn);"),
        ],
    }
    hits, via = macros.resolve("phantom_fn", make_query(data))
    assert hits == [] and via is None


def test_fuzzy_spellings_last_resort():
    data = {
        ("-x", "--", "do_sync_read"): [
            ("do_sync_read", 10, "fs/read_write.c", "ssize_t do_sync_read(...)"),
        ],
    }
    hits, via = macros.resolve("__do_sync_read", make_query(data))
    assert via == "fuzzy:do_sync_read"
    assert hits[0][0] == "do_sync_read"  # the spelling that actually exists


def test_fuzzy_spellings_generation():
    assert macros.fuzzy_spellings("__foo") == ["foo", "_foo", "____foo"] or (
        "foo" in macros.fuzzy_spellings("__foo")
    )
    assert "bar" in macros.fuzzy_spellings("bar_")
    assert "__baz" in macros.fuzzy_spellings("baz")
    assert macros.fuzzy_spellings("x") != ["x"]


def test_non_identifier_queries_are_ignored():
    query = make_query({})
    assert macros.resolve("sys_.*", query) == ([], None)
    assert query.calls == []


def test_exported_via():
    sources = [
        "\tret = phantom_fn(a);",
        "EXPORT_SYMBOL_GPL(phantom_fn);",
    ]
    assert macros.exported_via("phantom_fn", sources) == "EXPORT_SYMBOL_GPL"
    assert macros.exported_via("other_fn", sources) is None
    assert macros.exported_via("phantom_fn", ["EXPORT_SYMBOL(phantom_fn);"]) == (
        "EXPORT_SYMBOL"
    )


def test_hits_are_deduped_and_capped():
    refs = [
        ("SYSCALL_DEFINE0", n, f"f{n % 3}.c", "SYSCALL_DEFINE0(fork)")
        for n in range(500)
    ]
    data = {
        ("-x", "--", "SYSCALL_DEFINE0"): refs,
        ("-rx", "--", "SYSCALL_DEFINE0"): refs,  # duplicates must collapse
    }
    hits, _ = macros.resolve("sys_fork", make_query(data))
    assert len(hits) == macros.MAX_HITS
    assert len({(h[2], h[1]) for h in hits}) == len(hits)


# ---------------------------------------------------------------------------
# End-to-end: a tiny kernel-flavoured project through the MCP tools.
# ---------------------------------------------------------------------------


def _drain_refresh_state():
    for thread in list(server._refresh_threads.values()):
        thread.join(timeout=30)
    server._last_update.clear()
    server._update_cost.clear()
    server._refresh_errors.clear()
    server._refresh_threads.clear()


@pytest.fixture(autouse=True)
def fresh_state():
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
def kernelish_project(tmp_path):
    (tmp_path / "syscalls.h").write_text(
        textwrap.dedent(
            """\
            #ifndef SYSCALLS_H
            #define SYSCALLS_H
            #define SYSCALL_DEFINE3(name, ...) long sys_##name(__VA_ARGS__)
            #define TRACE_EVENT(name, proto, args) int __trace_##name(void)
            #define DEFINE_SPINLOCK(x) int x
            #endif
            """
        )
    )
    (tmp_path / "trace.h").write_text(
        textwrap.dedent(
            """\
            #include "syscalls.h"

            TRACE_EVENT(sched_switch,
                TP_PROTO(int prev),
                TP_ARGS(prev));
            """
        )
    )
    (tmp_path / "read.c").write_text(
        textwrap.dedent(
            """\
            #include "syscalls.h"

            DEFINE_SPINLOCK(css_lock);

            long helper_fn(int fd)
            {
                return fd + css_lock;
            }

            SYSCALL_DEFINE3(read, unsigned int, fd, char *, buf)
            {
                return helper_fn(fd);
            }
            EXPORT_SYMBOL(helper_fn);
            """
        )
    )
    return tmp_path


@requires_global
def test_find_definition_resolves_syscall(kernelish_project):
    result = json.loads(
        server.find_definition("sys_read", project_root=str(kernelish_project))
    )
    assert result["resolved_via"] == "macro:SYSCALL_DEFINE"
    assert result["results"], result
    top = result["results"][0]
    assert top["symbol"] == "sys_read"
    assert top["path"] == "read.c"
    assert "SYSCALL_DEFINE3(read" in top["snippet"]


@requires_global
def test_find_definition_resolves_trace_event(kernelish_project):
    result = json.loads(
        server.find_definition(
            "trace_sched_switch", project_root=str(kernelish_project)
        )
    )
    assert result["resolved_via"] == "macro:TRACE_EVENT"
    assert result["results"][0]["path"] == "trace.h"


@requires_global
def test_find_definition_resolves_bare_definer(kernelish_project):
    result = json.loads(
        server.find_definition("css_lock", project_root=str(kernelish_project))
    )
    assert result["resolved_via"] == "macro:DEFINE_SPINLOCK"
    assert "DEFINE_SPINLOCK(css_lock)" in result["results"][0]["snippet"]


@requires_global
def test_find_definition_fuzzy_fallback(kernelish_project):
    result = json.loads(
        server.find_definition("__helper_fn", project_root=str(kernelish_project))
    )
    assert result["resolved_via"] == "fuzzy:helper_fn"
    assert result["results"][0]["symbol"] == "helper_fn"


@requires_global
def test_find_definition_no_resolved_via_on_plain_hits(kernelish_project):
    result = json.loads(
        server.find_definition("helper_fn", project_root=str(kernelish_project))
    )
    assert "resolved_via" not in result
    assert result["results"][0]["symbol"] == "helper_fn"


@requires_global
def test_find_definition_text_mode_fallback(kernelish_project):
    result = server.find_definition(
        "sys_read", project_root=str(kernelish_project), format="text"
    )
    assert "SYSCALL_DEFINE3(read" in result
    assert "resolved via macro:SYSCALL_DEFINE" in result


@requires_global
def test_symbol_info_reports_resolution_and_export(kernelish_project):
    info = json.loads(
        server.symbol_info("sys_read", project_root=str(kernelish_project))
    )["results"]
    assert info["resolved_via"] == "macro:SYSCALL_DEFINE"
    assert info["definition_count"] >= 1

    info = json.loads(
        server.symbol_info("helper_fn", project_root=str(kernelish_project))
    )["results"]
    assert info["resolved_via"] is None
    assert info["exported"] == "EXPORT_SYMBOL"


@requires_global
def test_macro_resolution_opt_out(kernelish_project, monkeypatch):
    monkeypatch.setenv("GTAGS_MCP_MACRO_RESOLVE", "0")
    result = json.loads(
        server.find_definition("sys_read", project_root=str(kernelish_project))
    )
    assert "resolved_via" not in result
    assert result["results"] == []


@requires_global
def test_macro_resolution_config_opt_out(kernelish_project):
    (kernelish_project / ".gtags-mcp.toml").write_text("macro_resolve = false\n")
    result = json.loads(
        server.find_definition("sys_read", project_root=str(kernelish_project))
    )
    assert "resolved_via" not in result
    assert result["results"] == []
