"""reachability and the caller-graph definition cache: agent workflow tools."""

import json
import os
import textwrap

import pytest

from gtags_mcp import config, enrich, guards, server, toolchain

requires_global = pytest.mark.skipif(
    toolchain.find_global() is None or toolchain.find_gtags() is None,
    reason="GNU Global not installed",
)

pytestmark = requires_global


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
def chain_project(tmp_path):
    """main -> middle -> leaf, plus an unrelated island() function."""
    (tmp_path / "chain.c").write_text(
        textwrap.dedent(
            """\
            int leaf(int x)
            {
                return x + 1;
            }

            int middle(int x)
            {
                int a = leaf(x);
                return a + leaf(x + 1);
            }

            int island(int x)
            {
                return x;
            }

            int main(void)
            {
                return middle(41);
            }
            """
        )
    )
    return tmp_path


def test_reachability_finds_shortest_chain(chain_project):
    result = json.loads(
        server.reachability("main", "leaf", project_root=str(chain_project), format="json")
    )
    res = result["results"]
    assert res["path_found"] is True
    assert res["depth"] == 2
    assert [hop["symbol"] for hop in res["hops"]] == ["main", "middle", "leaf"]
    first = res["hops"][0]
    assert first["calls"] == "middle" and first["path"] == "chain.c"
    assert res["hops"][1]["call_sites"] == 2  # middle calls leaf twice
    assert res["hops"][-1]["calls"] is None
    assert res["hops"][-1]["line"] == 1  # leaf's definition


def test_reachability_trivial_and_direct(chain_project):
    res = json.loads(
        server.reachability("middle", "leaf", project_root=str(chain_project), format="json")
    )["results"]
    assert res["path_found"] and res["depth"] == 1

    res = json.loads(
        server.reachability("leaf", "leaf", project_root=str(chain_project), format="json")
    )["results"]
    assert res["path_found"] and res["depth"] == 0


def test_reachability_no_path(chain_project):
    result = json.loads(
        server.reachability("main", "island", project_root=str(chain_project), format="json")
    )
    res = result["results"]
    assert res["path_found"] is False
    assert res["hops"] == []
    assert "No call path" in result["message"]
    assert "function pointers" in result["message"]


def test_reachability_respects_max_depth(chain_project):
    res = json.loads(
        server.reachability(
            "main", "leaf", project_root=str(chain_project), max_depth=1, format="json"
        )
    )["results"]
    assert res["path_found"] is False


def test_reachability_text_format(chain_project):
    text = server.reachability("main", "leaf", project_root=str(chain_project))
    assert text.startswith("main reaches leaf in 2 calls:")
    assert "  middle (chain.c:8) calls leaf" in text
    assert "  main (chain.c:" in text and ") calls middle" in text


def test_reachability_exploration_budget(chain_project, monkeypatch):
    """A zeroed exploration budget stops the walk and says so in the message."""
    monkeypatch.setattr(server, "MAX_GRAPH_EXPANSIONS", 0)
    result = json.loads(
        server.reachability("main", "leaf", project_root=str(chain_project), format="json")
    )
    res = result["results"]
    assert res["path_found"] is False
    assert res["nodes_explored"] == 0
    assert "0-function exploration budget" in result["message"]


@pytest.fixture
def macro_wrapped_project(tmp_path):
    """helper_fn is only 'called' from inside an ALL-CAPS macro definition."""
    (tmp_path / "wrap.h").write_text("#define WRAP_HELPER(x) helper_fn(x)\n")
    (tmp_path / "user.c").write_text(
        '#include "wrap.h"\n'
        "int helper_fn(int x) { return x; }\n"
        "int uses_wrap(void) { return WRAP_HELPER(3); }\n"
    )
    return tmp_path


def test_reachability_macroish_callers_listed_not_expanded(macro_wrapped_project):
    root = str(macro_wrapped_project)
    # The macro itself IS found as a direct caller of helper_fn ...
    callers = json.loads(server.find_callers("helper_fn", root, format="json"))["results"]
    assert any(c["caller"] == "WRAP_HELPER" for c in callers)
    direct = json.loads(server.reachability("WRAP_HELPER", "helper_fn", root, format="json"))["results"]
    assert direct["path_found"] is True and direct["depth"] == 1
    # ... but the walk never expands THROUGH it: uses_wrap only reaches
    # helper_fn via the WRAP_HELPER macro name, so no path is reported.
    via_macro = json.loads(server.reachability("uses_wrap", "helper_fn", root, format="json"))["results"]
    assert via_macro["path_found"] is False


# ---------------------------------------------------------------------------
# Per-file `-fx` definition cache (keyed by index generation)
# ---------------------------------------------------------------------------


def _count_fx_runs(monkeypatch):
    """Wrap server._run to count `global -fx` subprocess invocations."""
    counts = {"fx": 0}
    real_run = server._run

    def counting_run(args, cwd, timeout=server.QUERY_TIMEOUT_SECONDS, **kwargs):
        if args[0] == "global" and "-fx" in args:
            counts["fx"] += 1
        return real_run(args, cwd, timeout, **kwargs)

    monkeypatch.setattr(server, "_run", counting_run)
    return counts


def test_fx_cache_one_run_per_file_per_generation(chain_project, monkeypatch):
    root = str(chain_project)
    server.find_definition("leaf", root)  # builds the index (generation 1)
    counts = _count_fx_runs(monkeypatch)

    server.reachability("main", "leaf", project_root=root)
    # The BFS visits several nodes, all referenced in chain.c — one -fx run.
    assert counts["fx"] == 1
    server.reachability("main", "leaf", project_root=root)
    server.find_callers("leaf", root)
    server.find_callers("middle", root)
    assert counts["fx"] == 1  # every later walk is served from the cache


def test_fx_cache_invalidated_by_update_index(chain_project, monkeypatch):
    root = str(chain_project)
    json.loads(server.find_callers("leaf", root, format="json"))  # build + populate cache
    counts = _count_fx_runs(monkeypatch)
    before = json.loads(server.find_callers("leaf", root, format="json"))
    assert counts["fx"] == 0  # cache hit
    assert all(r["caller"] != "newcomer" for r in before["results"])

    source = (chain_project / "chain.c").read_text()
    (chain_project / "chain.c").write_text(
        source + "\nint newcomer(int x)\n{\n    return leaf(x);\n}\n"
    )
    # Nudge mtime past the index build's second so `gtags -i` sees the edit.
    stat = (chain_project / "chain.c").stat()
    os.utime(chain_project / "chain.c", (stat.st_atime + 2, stat.st_mtime + 2))
    server.update_index(root)  # synchronous refresh bumps the generation

    after = json.loads(server.find_callers("leaf", root, format="json"))
    assert counts["fx"] >= 1  # re-queried under the new generation
    assert any(r["caller"] == "newcomer" for r in after["results"])


def test_index_generation_bumps_on_every_refresh_path(chain_project):
    root = chain_project.resolve()
    assert server._current_generation(root) == 0
    server.find_definition("leaf", str(chain_project))  # initial build
    assert server._current_generation(root) == 1
    server.update_index(str(chain_project), full=True)
    assert server._current_generation(root) == 2
    server.update_index(str(chain_project))  # synchronous incremental
    assert server._current_generation(root) == 3
