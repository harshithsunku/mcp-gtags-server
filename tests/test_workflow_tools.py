"""reachability and blast_radius: agent workflow tools over the caller graph."""

import json
import subprocess
import textwrap

import pytest

from gtags_mcp import config, enrich, guards, server, toolchain

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


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": "/usr/bin:/bin",
            "HOME": str(repo),
        },
    )


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


@pytest.fixture
def git_project(chain_project):
    _git(chain_project, "init", "-q")
    _git(chain_project, "add", ".")
    _git(chain_project, "commit", "-qm", "base")
    return chain_project


def test_reachability_finds_shortest_chain(chain_project):
    result = json.loads(
        server.reachability("main", "leaf", project_root=str(chain_project))
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
        server.reachability("middle", "leaf", project_root=str(chain_project))
    )["results"]
    assert res["path_found"] and res["depth"] == 1

    res = json.loads(
        server.reachability("leaf", "leaf", project_root=str(chain_project))
    )["results"]
    assert res["path_found"] and res["depth"] == 0


def test_reachability_no_path(chain_project):
    result = json.loads(
        server.reachability("main", "island", project_root=str(chain_project))
    )
    res = result["results"]
    assert res["path_found"] is False
    assert res["hops"] == []
    assert "No call path" in result["message"]
    assert "function pointers" in result["message"]


def test_reachability_respects_max_depth(chain_project):
    res = json.loads(
        server.reachability(
            "main", "leaf", project_root=str(chain_project), max_depth=1
        )
    )["results"]
    assert res["path_found"] is False


def test_reachability_text_format(chain_project):
    text = server.reachability(
        "main", "leaf", project_root=str(chain_project), format="text"
    )
    assert "reachable in 2 call(s): main -> middle -> leaf" in text
    assert "middle calls leaf at chain.c:8 (2 sites)" in text
    assert "main calls middle at" in text


def test_blast_radius_uncommitted_change(git_project):
    source = (git_project / "chain.c").read_text()
    (git_project / "chain.c").write_text(source.replace("x + 1", "x + 2", 1))

    result = json.loads(server.blast_radius(project_root=str(git_project)))
    by_symbol = {rec["symbol"]: rec for rec in result["results"]}
    assert by_symbol["leaf"]["distance"] == 0
    assert by_symbol["middle"]["distance"] == 1
    assert by_symbol["middle"]["via"] == "leaf"
    assert "island" not in by_symbol
    assert result["changed_functions"] == 1
    assert result["git_ref"] == "HEAD"


def test_blast_radius_depth_expands_transitively(git_project):
    source = (git_project / "chain.c").read_text()
    (git_project / "chain.c").write_text(source.replace("x + 1", "x + 2", 1))

    result = json.loads(server.blast_radius(project_root=str(git_project), depth=2))
    by_symbol = {rec["symbol"]: rec for rec in result["results"]}
    assert by_symbol["main"]["distance"] == 2
    assert by_symbol["main"]["via"] == "middle"
    # Ranked by distance: changed function first, furthest caller last.
    distances = [rec["distance"] for rec in result["results"]]
    assert distances == sorted(distances)


def test_blast_radius_depth_zero_lists_changed_only(git_project):
    source = (git_project / "chain.c").read_text()
    (git_project / "chain.c").write_text(source.replace("x + 1", "x + 2", 1))

    result = json.loads(server.blast_radius(project_root=str(git_project), depth=0))
    assert [rec["symbol"] for rec in result["results"]] == ["leaf"]


def test_blast_radius_committed_ref(git_project):
    source = (git_project / "chain.c").read_text()
    (git_project / "chain.c").write_text(source.replace("return x;", "return -x;"))
    _git(git_project, "commit", "-aqm", "tweak island")

    result = json.loads(
        server.blast_radius(git_ref="HEAD~1", project_root=str(git_project))
    )
    symbols = [rec["symbol"] for rec in result["results"]]
    assert symbols == ["island"]  # island has no callers


def test_blast_radius_clean_tree(git_project):
    result = json.loads(server.blast_radius(project_root=str(git_project)))
    assert result["results"] == []
    assert "no changes" in result["message"]


def test_blast_radius_bad_ref(git_project):
    result = json.loads(
        server.blast_radius(git_ref="no-such-ref", project_root=str(git_project))
    )
    assert "git diff" in result["error"]

    result = json.loads(
        server.blast_radius(git_ref="--output=/tmp/x", project_root=str(git_project))
    )
    assert "invalid git_ref" in result["error"]


def test_blast_radius_text_format(git_project):
    source = (git_project / "chain.c").read_text()
    (git_project / "chain.c").write_text(source.replace("x + 1", "x + 2", 1))

    text = server.blast_radius(project_root=str(git_project), format="text")
    assert "[changed] leaf" in text
    assert "[d=1] middle" in text and "via leaf" in text
