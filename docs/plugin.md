# Plugin & slash commands

One folder in the repository installs three things at once: the MCP server, a
navigation **skill** that tells the agent which tool answers which question, and
two **slash commands**. There is no MCP config file to edit — installing the plugin
registers the server.

## Install

| Client | How |
|---|---|
| **Claude Code** | `/plugin marketplace add harshithsunku/mcp-gtags-server` then `/plugin install mcp-gtags-server@mcp-gtags-server` |
| **Codex** | `codex plugin marketplace add harshithsunku/mcp-gtags-server`, then install it from the Plugins Directory in the ChatGPT desktop app |
| **Cursor** | Cursor loads [Agent Plugins](https://agent-plugins.org) 1.0.0 but installs from the Cursor Marketplace or a team marketplace; until then use the [manual entry](install.md#one-config-entry-every-repository) |

!!! warning "Install one way, not both"
    A plugin plus a manual entry runs two servers, and every tool shows up twice
    under different names. Switching to the plugin? `claude mcp remove gtags` first.

## Slash commands

These are MCP prompts, so they cost no tool-schema context at all. They work with
any installation method in clients that support MCP prompts (Claude Code, Cursor).

### `/gtags:impact [git_ref]`

Change-impact analysis, and the replacement for the old `blast_radius` tool:

1. run `git diff <ref>` (default `HEAD`) and list each changed C/C++ function;
2. call `find_callers` on each one;
3. report the callers ranked by risk, with `path:line`, naming those that likely
   need updating.

### `/gtags:explain <symbol>`

Definition → body → callers → a short explanation covering purpose, inputs and
outputs, side effects and locking, config-dependent variants, and the most
important callers.

## What the skill does

`skills/c-code-navigation/SKILL.md` is model-invoked: the agent loads it when a
question looks like C/C++ navigation. It carries the routing table (which tool
answers which question), how to read the compact output, the `project_root` rule,
kernel specifics such as `active_config`, and the change-impact recipe. It also
states plainly when to **keep using grep** — string literals, comments, Kconfig and
Makefiles.

## Layout

```text
plugin/
├── plugin.json                      # Agent Plugins 1.0.0 (Codex, Cursor)
├── mcp.json                         # portable MCP server config
├── .claude-plugin/plugin.json       # Claude Code manifest
├── .mcp.json                        # Claude Code MCP server config
└── skills/c-code-navigation/SKILL.md
```

Marketplace manifests for all three clients live at the repository root
(`.claude-plugin/`, `.agents/plugins/`, `.cursor-plugin/`) and point at that one
folder.

## Version pinning

Both MCP configs launch the server as:

```json
{ "command": "uvx", "args": ["--from", "mcp-gtags-server>=2,<3", "mcp-gtags-server"] }
```

The plugin ships a skill and prompts that describe the 2.x tool surface, so it
pins the server to 2.x: a future major that renames tools can't silently run under
the old skill, while every 2.x fix still arrives automatically. Manual installs
stay unpinned (`uvx mcp-gtags-server`).

## Working directory

Agent Plugins clients launch the server from the **plugin's** folder, not your
project. The plugin therefore sets `GTAGS_MCP_CWD_FALLBACK=0`, so the server asks
for `project_root` instead of indexing the plugin directory. Claude Code also
answers `roots/list` with your workspace, which resolves the repository
automatically.
