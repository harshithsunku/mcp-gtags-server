"""Unit tests for #ifdef guard scanning and active_config evaluation."""

import textwrap

import pytest

from gtags_mcp import guards


@pytest.fixture(autouse=True)
def fresh_guards_cache():
    guards.reset_cache()
    yield
    guards.reset_cache()


def _stacks(source: str) -> guards.FileGuards:
    return guards.scan_text(textwrap.dedent(source))


def _guard_at(source: str, line: int) -> list[str]:
    return guards.guard_list(_stacks(source).stack_at(line))


# ---------------------------------------------------------------------------
# Scanner: stacks and boundaries
# ---------------------------------------------------------------------------


def test_simple_ifdef_else_stacks():
    src = """\
    int always;
    #ifdef CONFIG_FOO
    int on;
    #else
    int off;
    #endif
    int after;
    """
    assert _guard_at(src, 1) == []
    assert _guard_at(src, 3) == ["CONFIG_FOO"]
    assert _guard_at(src, 5) == ["!CONFIG_FOO"]
    assert _guard_at(src, 7) == []


def test_boundary_lines():
    src = """\
    #ifdef A
    int inside;
    #endif
    int outside;
    """
    fg = _stacks(src)
    # Regions switch on the line AFTER each directive; the directive lines
    # themselves never carry symbols, so the exact side doesn't matter.
    assert guards.guard_list(fg.stack_at(1)) == []
    assert guards.guard_list(fg.stack_at(2)) == ["A"]
    assert guards.guard_list(fg.stack_at(4)) == []


def test_ifndef_and_nesting_order_outermost_first():
    src = """\
    #ifdef CONFIG_HIGHMEM
    #ifndef ARCH_HAS_KMAP_FLUSH_TLB
    void kmap_flush_tlb(void);
    #endif
    #endif
    """
    assert _guard_at(src, 3) == ["CONFIG_HIGHMEM", "!ARCH_HAS_KMAP_FLUSH_TLB"]


def test_elif_chain_efiapi_shape():
    src = """\
    #if defined(CONFIG_X86_64)
    #define __efiapi __attribute__((ms_abi))
    #elif defined(CONFIG_X86_32)
    #define __efiapi __attribute__((regparm(0)))
    #else
    #define __efiapi
    #endif
    """
    assert _guard_at(src, 2) == ["CONFIG_X86_64"]
    assert _guard_at(src, 4) == ["!CONFIG_X86_64 && CONFIG_X86_32"]
    assert _guard_at(src, 6) == ["!CONFIG_X86_64 && !CONFIG_X86_32"]


def test_compound_else_composition_parenthesized():
    src = """\
    #if BITS_PER_LONG==32 && defined(CONFIG_SMP)
    int a;
    #elif BITS_PER_LONG==32 && defined(CONFIG_PREEMPTION)
    int b;
    #else
    int c;
    #endif
    """
    assert _guard_at(src, 2) == ["BITS_PER_LONG==32 && defined(CONFIG_SMP)"]
    assert _guard_at(src, 6) == [
        "!(BITS_PER_LONG==32 && defined(CONFIG_SMP)) && "
        "!(BITS_PER_LONG==32 && defined(CONFIG_PREEMPTION))"
    ]


def test_if_zero_block():
    src = """\
    #if 0
    int dead;
    #endif
    """
    assert _guard_at(src, 2) == ["0"]


def test_backslash_continuation_joined():
    src = (
        "#if defined(CONFIG_A) && \\\n"
        "    defined(CONFIG_B)\n"
        "int x;\n"
        "#endif\n"
    )
    fg = guards.scan_text(src)
    assert guards.guard_list(fg.stack_at(3)) == [
        "defined(CONFIG_A) && defined(CONFIG_B)"
    ]


def test_spaced_and_indented_directives():
    src = """\
    # if HZ < 100
    int slow;
    #  endif
    """
    assert _guard_at(src, 2) == ["HZ < 100"]
    assert _guard_at(src, 3)[0:0] == []  # no crash on spaced #endif


# ---------------------------------------------------------------------------
# Comments must not confuse the scanner
# ---------------------------------------------------------------------------


def test_directives_inside_block_comments_ignored():
    src = """\
    /*
     * Example: wrap with
     * #ifdef CONFIG_NOT_REAL
     */
    int real;
    """
    assert _guard_at(src, 5) == []


def test_misleading_comment_on_else_ignored():
    # highmem-internal.h style: the comment names the OPPOSITE condition.
    src = """\
    #ifdef CONFIG_HIGHMEM
    int on;
    #else /* CONFIG_HIGHMEM */
    int off;
    #endif /* CONFIG_HIGHMEM */
    """
    assert _guard_at(src, 4) == ["!CONFIG_HIGHMEM"]


def test_line_comment_on_directive_stripped():
    src = """\
    #ifdef CONFIG_A // enable A
    int a;
    #endif
    """
    assert _guard_at(src, 2) == ["CONFIG_A"]


def test_comment_start_inside_string_does_not_hide_directives():
    src = """\
    const char *s = "/*";
    #ifdef CONFIG_A
    int a;
    #endif
    """
    assert _guard_at(src, 3) == ["CONFIG_A"]


# ---------------------------------------------------------------------------
# Include-guard suppression
# ---------------------------------------------------------------------------

CLASSIC_GUARD = """\
#ifndef UTIL_H
#define UTIL_H
int f(void);
#ifdef CONFIG_A
int g(void);
#endif
#endif
"""


def test_include_guard_suppressed():
    fg = guards.scan_text(CLASSIC_GUARD)
    assert guards.guard_list(fg.stack_at(3)) == []
    assert guards.guard_list(fg.stack_at(5)) == ["CONFIG_A"]


def test_if_not_defined_variant_suppressed():
    src = """\
    #if !defined(UTIL_H)
    #define UTIL_H
    int f(void);
    #endif
    """
    assert _guard_at(src, 3) == []


def test_guard_with_leading_and_trailing_comments_suppressed():
    src = """\
    /* SPDX-License-Identifier: GPL-2.0 */
    #ifndef UTIL_H
    #define UTIL_H
    int f(void);
    #endif /* UTIL_H */
    """
    assert _guard_at(src, 4) == []


def test_not_a_guard_when_content_precedes():
    src = """\
    int early;
    #ifndef UTIL_H
    #define UTIL_H
    int f(void);
    #endif
    """
    assert _guard_at(src, 4) == ["!UTIL_H"]


def test_not_a_guard_when_content_follows_final_endif():
    src = """\
    #ifndef UTIL_H
    #define UTIL_H
    int f(void);
    #endif
    int trailing;
    """
    assert _guard_at(src, 3) == ["!UTIL_H"]


def test_not_a_guard_when_define_missing_or_different():
    no_define = """\
    #ifndef UTIL_H
    int f(void);
    #endif
    """
    assert _guard_at(no_define, 2) == ["!UTIL_H"]
    different = """\
    #ifndef UTIL_H
    #define UTIL_HX
    int f(void);
    #endif
    """
    assert _guard_at(different, 3) == ["!UTIL_H"]


def test_pragma_once_creates_no_frame():
    src = """\
    #pragma once
    #ifdef CONFIG_A
    int a;
    #endif
    """
    assert _guard_at(src, 1) == []
    assert _guard_at(src, 3) == ["CONFIG_A"]


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "src",
    [
        "",  # empty file
        "#endif\n#endif\nint x;\n",  # extra #endif
        "#else\nint x;\n",  # #else without #if
        "#elif defined(A)\nint x;\n",  # dangling #elif
        "#ifdef A\nint x;\n",  # unclosed at EOF
        "#ifdef\nint x;\n#endif\n",  # #ifdef with no macro
        "#if\nint x;\n#endif\n",  # #if with no expression
    ],
)
def test_broken_input_never_raises(src):
    fg = guards.scan_text(src)
    assert isinstance(fg.stack_at(1), tuple)


def test_crlf_input():
    src = "#ifdef A\r\nint x;\r\n#endif\r\n"
    fg = guards.scan_text(src)
    assert guards.guard_list(fg.stack_at(2)) == ["A"]


def test_unclosed_frame_at_eof_keeps_stack():
    src = "#ifdef A\nint x;\n"
    assert _guard_at(src, 2) == ["A"]


# ---------------------------------------------------------------------------
# Per-file cache
# ---------------------------------------------------------------------------


def test_guards_for_file_cached_and_invalidated(tmp_path, monkeypatch):
    calls = []
    real_scan = guards.scan_text
    monkeypatch.setattr(
        guards, "scan_text", lambda text: calls.append(1) or real_scan(text)
    )
    src = tmp_path / "a.c"
    src.write_text("#ifdef A\nint x;\n#endif\n")
    assert guards.guards_for_file(src) is guards.guards_for_file(src)
    assert len(calls) == 1
    src.write_text("#ifdef B\nint x;\n#endif\n")
    fg = guards.guards_for_file(src)
    assert guards.guard_list(fg.stack_at(2)) == ["B"]
    assert len(calls) == 2


def test_guards_for_file_missing_and_oversized(tmp_path, monkeypatch):
    assert guards.guards_for_file(tmp_path / "nope.c") is None
    monkeypatch.setattr(guards, "MAX_FILE_BYTES", 10)
    big = tmp_path / "big.c"
    big.write_text("x" * 100)
    assert guards.guards_for_file(big) is None  # negative-cached
    assert guards.guards_for_file(big) is None


def test_lru_eviction(tmp_path, monkeypatch):
    monkeypatch.setattr(guards, "CACHE_CAPACITY", 2)
    calls = []
    real_scan = guards.scan_text
    monkeypatch.setattr(
        guards, "scan_text", lambda text: calls.append(1) or real_scan(text)
    )
    files = []
    for i in range(3):
        f = tmp_path / f"f{i}.c"
        f.write_text(f"int f{i};\n")
        files.append(f)
        guards.guards_for_file(f)
    assert len(calls) == 3
    guards.guards_for_file(files[0])  # evicted -> rescanned
    assert len(calls) == 4
    guards.guards_for_file(files[2])  # still cached
    assert len(calls) == 4


# ---------------------------------------------------------------------------
# .config / macro-list parsing
# ---------------------------------------------------------------------------


def test_parse_dot_config_semantics():
    cfg = guards.parse_dot_config(
        textwrap.dedent(
            """\
            # Automatically generated file
            CONFIG_SMP=y
            CONFIG_FSCACHE=m
            # CONFIG_HIGHMEM is not set
            CONFIG_NR_CPUS=64
            CONFIG_LOCALVERSION="-test"
            CONFIG_OFF=n
            """
        )
    )
    assert cfg.defined["CONFIG_SMP"] == 1
    assert "CONFIG_FSCACHE" not in cfg.defined  # =m defines _MODULE instead
    assert cfg.defined["CONFIG_FSCACHE_MODULE"] == 1
    assert "CONFIG_HIGHMEM" not in cfg.defined
    assert cfg.defined["CONFIG_NR_CPUS"] == 64
    assert cfg.defined["CONFIG_LOCALVERSION"] == "-test"
    assert "CONFIG_OFF" not in cfg.defined
    assert cfg.closed is True


def test_parse_macro_list_open_world():
    cfg = guards.parse_macro_list("CONFIG_SMP, BITS_PER_LONG=64, !CONFIG_DEBUG")
    assert cfg.defined == {"CONFIG_SMP": 1, "BITS_PER_LONG": 64}
    assert cfg.absent == frozenset({"CONFIG_DEBUG"})
    assert cfg.closed is False


def test_load_active_config_file_vs_list(tmp_path):
    dot = tmp_path / "test.config"
    dot.write_text("CONFIG_FOO=y\n")
    cfg, err = guards.load_active_config(str(dot), None)
    assert err is None and cfg.defined == {"CONFIG_FOO": 1} and cfg.closed

    cfg, err = guards.load_active_config("CONFIG_FOO", tmp_path)
    assert err is None and not cfg.closed

    cfg, err = guards.load_active_config("missing/.config", tmp_path)
    assert cfg is None and "not found" in err


def test_load_active_config_relative_to_root(tmp_path):
    (tmp_path / ".config").write_text("CONFIG_BAR=y\n")
    cfg, err = guards.load_active_config(".config", tmp_path)
    assert err is None and cfg.defined == {"CONFIG_BAR": 1}


# ---------------------------------------------------------------------------
# Tri-state evaluator
# ---------------------------------------------------------------------------

CFG = guards.parse_macro_list("CONFIG_ON, BITS_PER_LONG=64, !CONFIG_OFF")
CLOSED = guards.parse_dot_config("CONFIG_Y=y\nCONFIG_M=m\n")


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("defined(CONFIG_ON)", True),
        ("defined CONFIG_ON", True),  # paren-less form
        ("!defined(CONFIG_ON)", False),
        ("defined(CONFIG_OFF)", False),  # explicit !CONFIG_OFF
        ("defined(CONFIG_MYSTERY)", None),  # open world: unknown
        ("CONFIG_ON", True),
        ("BITS_PER_LONG == 64", True),
        ("BITS_PER_LONG == 32", False),
        ("BITS_PER_LONG >= 32 && defined(CONFIG_ON)", True),
        ("defined(CONFIG_MYSTERY) || defined(CONFIG_ON)", True),  # Kleene or
        ("defined(CONFIG_MYSTERY) && !defined(CONFIG_ON)", False),  # Kleene and
        ("defined(CONFIG_MYSTERY) && defined(CONFIG_ON)", None),
        ("!defined(CONFIG_MYSTERY)", None),  # not(unknown) = unknown
        ("0", False),
        ("1", True),
        ("(BITS_PER_LONG + 0) > 32", True),  # parens stay arithmetic
        ("0x40 == BITS_PER_LONG", True),
        ("64UL == BITS_PER_LONG", True),
        ("FOO(3)", None),  # function-like macro: unknown
        ("garbage ~~ !!", None),  # unparseable: unknown
        ("BITS_PER_LONG / 0", None),  # division by zero: unknown
    ],
)
def test_eval_expr(expr, expected):
    assert guards.eval_expr(expr, CFG) is expected


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("defined(CONFIG_Y)", True),
        ("defined(CONFIG_M)", False),  # =m does NOT define the base name
        ("defined(CONFIG_M_MODULE)", True),
        ("defined(CONFIG_ABSENT)", False),  # closed world
        ("defined(NOT_A_CONFIG)", None),  # non-CONFIG stays unknown
        ("IS_ENABLED(CONFIG_Y)", True),
        ("IS_ENABLED(CONFIG_M)", True),
        ("IS_ENABLED(CONFIG_ABSENT)", False),
        ("IS_BUILTIN(CONFIG_M)", False),
        ("IS_MODULE(CONFIG_M)", True),
        ("IS_REACHABLE(CONFIG_M)", True),
    ],
)
def test_eval_expr_closed_world_and_kernel_idioms(expr, expected):
    assert guards.eval_expr(expr, CLOSED) is expected


def test_stack_satisfiable_filters_only_definite_false():
    # Note the unknown case uses a NON-CONFIG macro: a .config is a closed
    # world for CONFIG_* (absent means off), but says nothing about
    # __ASSEMBLY__ / ARCH_HAS_* style macros.
    fg = guards.scan_text(
        "#ifdef CONFIG_Y\nint a;\n#else\nint b;\n#endif\n"
        "#ifdef ARCH_HAS_MYSTERY\nint c;\n#endif\n"
        "#ifdef CONFIG_UNSET\nint d;\n#endif\n"
    )
    assert guards.stack_satisfiable(fg.stack_at(2), CLOSED) is True
    assert guards.stack_satisfiable(fg.stack_at(4), CLOSED) is False
    assert guards.stack_satisfiable(fg.stack_at(7), CLOSED) is None
    assert guards.stack_satisfiable(fg.stack_at(10), CLOSED) is False  # closed
    assert guards.stack_satisfiable((), CLOSED) is True  # unguarded


def test_else_of_elif_chain_satisfiability():
    fg = guards.scan_text(
        "#if defined(CONFIG_Y)\nint a;\n"
        "#elif defined(CONFIG_M_MODULE)\nint b;\n"
        "#else\nint c;\n#endif\n"
    )
    assert guards.stack_satisfiable(fg.stack_at(2), CLOSED) is True
    assert guards.stack_satisfiable(fg.stack_at(4), CLOSED) is False
    assert guards.stack_satisfiable(fg.stack_at(6), CLOSED) is False


def test_if_zero_filtered_under_any_config():
    fg = guards.scan_text("#if 0\nint dead;\n#endif\n")
    assert guards.stack_satisfiable(fg.stack_at(2), CFG) is False
    assert guards.stack_satisfiable(fg.stack_at(2), CLOSED) is False
