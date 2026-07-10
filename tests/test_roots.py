"""MCP roots-protocol behavior: root resolution from client workspace roots.

Uses the SDK's in-memory transport so a real ClientSession (with a
list_roots_callback) talks to the real FastMCP server, exercising the
async wrapper, the per-session cache, and the resolution ladder end to end.
The lowlevel server (mcp._mcp_server) is passed because older SDKs' helper
does not accept a FastMCP instance directly.
"""

import json
import textwrap
from pathlib import Path

import anyio
import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from gtags_mcp import server, toolchain

requires_global = pytest.mark.skipif(
    toolchain.find_global() is None or toolchain.find_gtags() is None,
    reason="GNU Global not installed",
)


@pytest.fixture(autouse=True)
def fresh_roots_state():
    server._session_roots.clear()
    yield
    server._session_roots.clear()


def _make_project(base: Path, name: str, symbol: str) -> Path:
    root = base / name
    root.mkdir()
    (root / "main.c").write_text(
        textwrap.dedent(
            f"""\
            int {symbol}(int x) {{ return x + 1; }}
            int main(void) {{ return {symbol}(1); }}
            """
        )
    )
    return root


@pytest.fixture
def project_a(tmp_path):
    return _make_project(tmp_path, "repo_a", "alpha_fn")


@pytest.fixture
def project_b(tmp_path):
    return _make_project(tmp_path, "repo_b", "beta_fn")


def _roots_callback(*paths: Path, counter: list | None = None):
    async def callback(_context) -> types.ListRootsResult:
        if counter is not None:
            counter.append(1)
        return types.ListRootsResult(
            roots=[types.Root(uri=p.as_uri()) for p in paths]
        )

    return callback


async def _call(session, tool: str, **arguments) -> dict:
    result = await session.call_tool(tool, arguments)
    return json.loads(result.content[0].text)


# ---------------------------------------------------------------------------
# URI conversion
# ---------------------------------------------------------------------------


def test_root_uri_to_path(tmp_path):
    assert server._root_uri_to_path(tmp_path.as_uri()) == tmp_path.resolve()
    assert server._root_uri_to_path("https://example.com/x") is None
    assert server._root_uri_to_path((tmp_path / "missing").as_uri()) is None
    spaced = tmp_path / "with space"
    spaced.mkdir()
    assert server._root_uri_to_path(spaced.as_uri()) == spaced.resolve()


# ---------------------------------------------------------------------------
# _effective_root ladder (direct, via the ContextVar)
# ---------------------------------------------------------------------------


def test_single_root_wins_over_cwd(project_a):
    token = server._roots_ctx.set((project_a,))
    try:
        root, err = server._effective_root(None)
    finally:
        server._roots_ctx.reset(token)
    assert err is None
    assert root == project_a


def test_multiple_roots_need_disambiguation(project_a, project_b):
    token = server._roots_ctx.set((project_a, project_b))
    try:
        root, err = server._effective_root(None)
    finally:
        server._roots_ctx.reset(token)
    assert root is None
    assert "2 workspace roots" in err
    assert str(project_a) in err and str(project_b) in err


def test_multiple_roots_disambiguated_by_cwd(project_a, project_b, monkeypatch):
    monkeypatch.chdir(project_a)
    token = server._roots_ctx.set((project_a, project_b))
    try:
        root, err = server._effective_root(None)
    finally:
        server._roots_ctx.reset(token)
    assert err is None
    assert root == project_a


def test_explicit_project_root_beats_client_roots(project_a, project_b):
    token = server._roots_ctx.set((project_a,))
    try:
        root, err = server._effective_root(str(project_b))
    finally:
        server._roots_ctx.reset(token)
    assert err is None
    assert root == project_b


# ---------------------------------------------------------------------------
# End-to-end through a real client session (in-memory transport)
# ---------------------------------------------------------------------------


@requires_global
def test_client_root_used_for_queries(project_a):
    async def run():
        async with create_connected_server_and_client_session(
            server.mcp._mcp_server, list_roots_callback=_roots_callback(project_a)
        ) as session:
            return await _call(session, "find_definition", symbol="alpha_fn")

    envelope = anyio.run(run)
    assert envelope.get("error") is None
    assert envelope["root"] == str(project_a)
    assert envelope["results"][0]["path"] == "main.c"


@requires_global
def test_two_client_roots_error_then_explicit_choice(project_a, project_b):
    async def run():
        async with create_connected_server_and_client_session(
            server.mcp._mcp_server,
            list_roots_callback=_roots_callback(project_a, project_b),
        ) as session:
            ambiguous = await _call(session, "find_definition", symbol="beta_fn")
            explicit = await _call(
                session,
                "find_definition",
                symbol="beta_fn",
                project_root=str(project_b),
            )
            return ambiguous, explicit

    ambiguous, explicit = anyio.run(run)
    assert "workspace roots" in ambiguous["error"]
    assert str(project_a) in ambiguous["error"]
    assert explicit.get("error") is None
    assert explicit["root"] == str(project_b)


@requires_global
def test_roots_fetched_once_per_session(project_a):
    calls: list = []

    async def run():
        async with create_connected_server_and_client_session(
            server.mcp._mcp_server,
            list_roots_callback=_roots_callback(project_a, counter=calls),
        ) as session:
            await _call(session, "find_definition", symbol="alpha_fn")
            await _call(session, "find_references", symbol="alpha_fn")

    anyio.run(run)
    assert len(calls) == 1


@requires_global
def test_client_without_roots_capability_uses_cwd(project_a, monkeypatch):
    monkeypatch.chdir(project_a)

    async def run():
        async with create_connected_server_and_client_session(
            server.mcp._mcp_server  # no list_roots_callback -> no roots capability
        ) as session:
            return await _call(session, "find_definition", symbol="alpha_fn")

    envelope = anyio.run(run)
    assert envelope.get("error") is None
    assert envelope["root"] == str(project_a)
