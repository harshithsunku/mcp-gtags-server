"""The eval harness: golden-set loading, checks, scoring, exit codes."""

import json
import textwrap

import pytest

from gtags_mcp import config, enrich, evalharness, guards, server, toolchain

requires_global = pytest.mark.skipif(
    toolchain.find_global() is None or toolchain.find_gtags() is None,
    reason="GNU Global not installed",
)

pytestmark = requires_global


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
def c_project(tmp_path):
    (tmp_path / "code.c").write_text(
        textwrap.dedent(
            """\
            int helper(int x)
            {
                return x + 1;
            }

            int caller_fn(int x)
            {
                return helper(x);
            }
            """
        )
    )
    return tmp_path


def _write_golden(tmp_path, cases):
    golden = tmp_path / "golden.jsonl"
    lines = ["# comment line", ""]
    lines += [json.dumps(case) for case in cases]
    golden.write_text("\n".join(lines) + "\n")
    return golden


def test_eval_passes_and_reports(c_project, tmp_path, capsys):
    golden = _write_golden(
        tmp_path,
        [
            {
                "id": "def-helper",
                "category": "definition",
                "tool": "find_definition",
                "args": {"symbol": "helper"},
                "expect": {"paths": ["code.c"], "top_path": ["code.c"]},
            },
            {
                "id": "callers-helper",
                "category": "callers",
                "tool": "find_callers",
                "args": {"symbol": "helper"},
                "expect": {"callers": ["caller_fn"], "min_results": 1},
            },
            {
                "id": "reach",
                "category": "workflow",
                "tool": "reachability",
                "args": {"from_symbol": "caller_fn", "to_symbol": "helper"},
                "expect": {"path_found": True},
            },
        ],
    )
    code = evalharness.run(str(golden), str(c_project), threshold=1.0)
    out = capsys.readouterr().out
    assert code == 0
    assert "3 cases" in out
    assert "definition" in out and "1/1 passed" in out
    assert "recall (expected content found): 2/3 = 66.7%" not in out  # all recall ok
    assert "precision@1 (top result right):  1/1 = 100.0%" in out
    assert "PASS" in out


def test_eval_fails_below_threshold(c_project, tmp_path, capsys):
    golden = _write_golden(
        tmp_path,
        [
            {
                "id": "wrong-path",
                "category": "definition",
                "tool": "find_definition",
                "args": {"symbol": "helper"},
                "expect": {"paths": ["other.c"]},
            },
        ],
    )
    code = evalharness.run(str(golden), str(c_project), threshold=0.9)
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL wrong-path" in out
    assert "expected path other.c" in out


def test_eval_tolerates_known_fail_within_threshold(c_project, tmp_path, capsys):
    golden = _write_golden(
        tmp_path,
        [
            {
                "id": "ok",
                "category": "definition",
                "tool": "find_definition",
                "args": {"symbol": "helper"},
                "expect": {"paths": ["code.c"]},
            },
            {
                "id": "known-fail",
                "category": "definition",
                "tool": "find_definition",
                "args": {"symbol": "helper"},
                "expect": {"paths": ["missing.c"]},
            },
        ],
    )
    assert evalharness.run(str(golden), str(c_project), threshold=0.5) == 0
    assert evalharness.run(str(golden), str(c_project), threshold=0.9) == 1
    capsys.readouterr()


def test_eval_missing_golden_or_root(tmp_path, capsys):
    assert evalharness.run(str(tmp_path / "nope.jsonl"), str(tmp_path), 0.9) == 2
    golden = _write_golden(tmp_path, [])
    assert evalharness.run(str(golden), None, 0.9) == 2
    capsys.readouterr()


def test_eval_rejects_malformed_cases(tmp_path):
    golden = tmp_path / "golden.jsonl"
    golden.write_text('{"id": "x", "tool": "find_definition"}\n')
    with pytest.raises(SystemExit, match="missing 'category'"):
        evalharness._load_cases(golden)
    golden.write_text("{not json}\n")
    with pytest.raises(SystemExit, match="bad JSON"):
        evalharness._load_cases(golden)


def test_repo_golden_set_is_well_formed():
    from pathlib import Path

    cases = evalharness._load_cases(Path(__file__).parent.parent / "evals/golden.jsonl")
    assert len(cases) >= 45
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    categories = {case["category"] for case in cases}
    assert {"definition", "macro", "references", "callers", "guards", "workflow"} <= categories
