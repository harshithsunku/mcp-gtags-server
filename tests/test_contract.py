"""Client-facing contract: what MCP clients actually read, pinned in tests.

Budgets come from the clients' documentation:
- Claude Code loads only tool NAMES and server instructions at session start
  and truncates tool descriptions and server instructions at 2 KB each.
- Codex reads the server instructions and advises keeping the first 512
  characters self-contained.
- Tool search matches names AND descriptions, so each description's first
  sentence carries the words agents search with ("who calls", "usages", ...).
"""

import json
import pathlib

import anyio
import jsonschema
from mcp import Client

from gtags_mcp import server

CLIENT_TEXT_LIMIT = 2048
CODEX_INSTRUCTIONS_HEAD = 512

TOOLS = {
    "find_definition", "find_references", "get_symbol_body", "find_callers",
    "find_callees", "reachability", "list_file_symbols", "update_index",
}
ALWAYS_LOADED = {"find_definition", "find_callers", "get_symbol_body"}


def _list(kind: str):
    async def run():
        async with Client(server.mcp, mode="legacy") as client:
            if kind == "tools":
                return (await client.list_tools()).tools
            return (await client.list_prompts()).prompts

    return anyio.run(run)


def test_instructions_fit_client_budgets():
    instructions = server.mcp.instructions
    assert len(instructions.encode()) <= CLIENT_TEXT_LIMIT
    head = instructions[:CODEX_INSTRUCTIONS_HEAD]
    # The self-contained head routes by task, names the output shape, and
    # tells the agent how to pick the repository.
    for needle in ("find_callers", "find_references", "reachability",
                   "get_symbol_body", "find_definition", "Keep grep",
                   "format='json'", "project_root"):
        assert needle in head, needle
    assert head.rstrip().endswith("ambiguous.")  # no sentence cut at 512
    assert "ALWAYS" not in instructions  # route by task, don't overclaim


def test_tool_surface_and_metadata():
    tools = {t.name: t for t in _list("tools")}
    assert set(tools) == TOOLS
    for name, tool in tools.items():
        assert len((tool.description or "").encode()) <= CLIENT_TEXT_LIMIT, name
        hints = tool.annotations
        assert hints.read_only_hint is (name != "update_index"), name
        assert hints.open_world_hint is False, name
        always = (tool.meta or {}).get("anthropic/alwaysLoad") is True
        assert always is (name in ALWAYS_LOADED), name


def test_descriptions_lead_with_search_terms():
    first_lines = {t.name: (t.description or "").split("\n\n")[0].lower() for t in _list("tools")}
    expected = {
        "find_callers": ("who calls", "callers", "call hierarchy", "incoming calls"),
        "find_references": ("references", "usages"),
        "find_definition": ("go to definition",),
        "find_callees": ("callees", "outgoing calls"),
        "reachability": ("call path", "call chain"),
        "get_symbol_body": ("source", "body"),
        "list_file_symbols": ("outline", "document symbols"),
        "update_index": ("refresh", "index"),
    }
    for name, words in expected.items():
        for word in words:
            assert word in first_lines[name], (name, word)


def test_prompts_are_registered_and_render():
    prompts = {p.name: p for p in _list("prompts")}
    assert set(prompts) == {"impact", "explain"}
    assert [a.name for a in prompts["impact"].arguments] == ["git_ref"]
    assert prompts["impact"].arguments[0].required is False
    assert [a.name for a in prompts["explain"].arguments] == ["symbol"]

    async def get(name, args):
        async with Client(server.mcp, mode="legacy") as client:
            return (await client.get_prompt(name, args)).messages[0].content.text

    impact = anyio.run(get, "impact", {"git_ref": "HEAD~1"})
    assert "git diff HEAD~1" in impact and "find_callers" in impact
    explain = anyio.run(get, "explain", {"symbol": "vfs_read"})
    for tool in ("find_definition", "get_symbol_body", "find_callers"):
        assert tool in explain


# ---------------------------------------------------------------------------
# Plugin package: one folder installable in Claude Code (native format) and in
# Codex / Cursor (Agent Plugins 1.0.0). Schemas are vendored for offline runs.
# ---------------------------------------------------------------------------

REPO = pathlib.Path(__file__).resolve().parent.parent
PLUGIN = REPO / "plugin"
SCHEMAS = pathlib.Path(__file__).resolve().parent / "fixtures" / "agent-plugins"


def _json(path: pathlib.Path):
    return json.loads(path.read_text())


def test_agent_plugins_manifests_validate():
    """Codex's standard format and Cursor's portable format, per the spec."""
    for name, manifest in (("plugin", PLUGIN / "plugin.json"), ("mcp", PLUGIN / "mcp.json")):
        jsonschema.validate(_json(manifest), _json(SCHEMAS / f"{name}.schema.json"))
    server_entry = _json(PLUGIN / "mcp.json")["mcpServers"]["gtags"]
    assert server_entry["type"] == "stdio"
    assert server_entry["command"] == "uvx"  # bare executable name, per spec §7.2.1
    # Agent Plugins clients run the server from the PLUGIN folder, which must
    # never be mistaken for the user's project.
    assert server_entry["env"]["GTAGS_MCP_CWD_FALLBACK"] == "0"


def test_plugin_pins_the_server_major_version():
    """A 3.x server would rename tools the 2.x skill and prompts describe."""
    for manifest in (PLUGIN / "mcp.json", PLUGIN / ".mcp.json"):
        args = _json(manifest)["mcpServers"]["gtags"]["args"]
        assert args[:2] == ["--from", "mcp-gtags-server>=2,<3"]
        assert args[-1] == "mcp-gtags-server"


def test_plugin_and_marketplaces_point_at_one_folder():
    claude = _json(REPO / ".claude-plugin" / "marketplace.json")
    assert [p["source"] for p in claude["plugins"]] == ["./plugin"]
    codex = _json(REPO / ".agents" / "plugins" / "marketplace.json")
    assert [p["source"]["path"] for p in codex["plugins"]] == ["./plugin"]
    cursor = _json(REPO / ".cursor-plugin" / "marketplace.json")
    assert [p["source"] for p in cursor["plugins"]] == ["plugin"]
    names = {
        claude["plugins"][0]["name"], codex["plugins"][0]["name"],
        cursor["plugins"][0]["name"], _json(PLUGIN / "plugin.json")["name"],
        _json(PLUGIN / ".claude-plugin" / "plugin.json")["name"],
    }
    assert names == {"mcp-gtags-server"}


def test_skill_is_portable_and_within_the_listing_budget():
    skill = (PLUGIN / "skills" / "c-code-navigation" / "SKILL.md").read_text()
    assert skill.startswith("---\n")
    front = skill.split("---", 2)[1]
    fields = dict(
        line.split(":", 1) for line in front.strip().splitlines() if ":" in line
    )
    assert fields["name"].strip() == "c-code-navigation"
    # Agent Skills standard fields only, so the same skill loads everywhere.
    assert set(fields) <= {"name", "description", "license"}
    # description + when_to_use are truncated at 1,536 chars in skill listings.
    assert len(fields["description"].strip()) <= 1536
    for tool in TOOLS:
        assert tool in skill, tool


def test_version_is_in_sync_across_every_manifest():
    """Five files carry the version; a release that misses one ships a lie."""
    from gtags_mcp import __version__

    pyproject = (REPO / "pyproject.toml").read_text()
    assert f'version = "{__version__}"' in pyproject
    server_json = _json(REPO / "server.json")
    assert server_json["version"] == __version__
    assert server_json["packages"][0]["version"] == __version__
    assert _json(PLUGIN / "plugin.json")["version"] == __version__
    assert _json(PLUGIN / ".claude-plugin" / "plugin.json")["version"] == __version__
    cursor_entry = _json(REPO / ".cursor-plugin" / "marketplace.json")["plugins"][0]
    assert cursor_entry["version"] == __version__


def test_cursor_listing_assets_exist():
    """Cursor's submission checklist wants a committed logo referenced by a
    relative path; the path resolves under the plugin directory."""
    entry = _json(REPO / ".cursor-plugin" / "marketplace.json")["plugins"][0]
    logo = entry["logo"]
    assert not logo.startswith(("/", "http")), "commit the logo, use a relative path"
    assert (PLUGIN / logo).is_file()
    assert (PLUGIN / logo).read_text().lstrip().startswith("<svg")
    # Components the entry points at must exist too.
    assert (PLUGIN / entry["skills"]).is_dir()
    assert (PLUGIN / entry["mcpServers"]).is_file()
