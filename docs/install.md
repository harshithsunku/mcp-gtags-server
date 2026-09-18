# Install

The only prerequisite is [`uv`](https://docs.astral.sh/uv/). Everything else —
GNU Global, universal-ctags, Pygments — installs itself into `~/.gtags-mcp` on the
first tool call, in user space, without `sudo` or a compiler.

```bash
# one-time, if you don't have uv yet
curl -LsSf https://astral.sh/uv/install.sh | sh
```

!!! warning "Pick one installation path"
    A plugin **and** a manual entry means two servers running and every tool
    appearing twice in the agent's tool list. If you switch to the plugin, remove
    the manual entry first (`claude mcp remove gtags`).

## One config entry, every repository

=== "Claude Code"

    ```bash
    claude mcp add --scope user gtags -- uvx mcp-gtags-server
    ```

=== "Codex"

    `~/.codex/config.toml`:

    ```toml
    [mcp_servers.gtags]
    command = "uvx"
    args = ["mcp-gtags-server"]
    startup_timeout_sec = 30   # the first run downloads the package
    ```

=== "Cursor"

    `~/.cursor/mcp.json` (or the one-click install badge in the README):

    ```json
    {
      "mcpServers": {
        "gtags": { "command": "uvx", "args": ["mcp-gtags-server"] }
      }
    }
    ```

=== "Any MCP client"

    ```json
    {
      "mcpServers": {
        "gtags": { "command": "uvx", "args": ["mcp-gtags-server"] }
      }
    }
    ```

A single user-level entry serves every repository: each call resolves its project
root from the tool argument, the client's workspace, or the working directory. See
[Configuration](configuration.md#project-root-resolution).

## As a plugin

The plugin adds the server **plus** a "C/C++ code navigation" skill and the
`/gtags:impact` and `/gtags:explain` slash commands. Installing it registers the
MCP server for you — there is no config file to edit.

| Client | Install |
|---|---|
| **Claude Code** | `/plugin marketplace add harshithsunku/mcp-gtags-server` then `/plugin install mcp-gtags-server@mcp-gtags-server` |
| **Codex** | `codex plugin marketplace add harshithsunku/mcp-gtags-server`, then install it from the Plugins Directory in the ChatGPT desktop app |
| **Cursor** | Loads [Agent Plugins](https://agent-plugins.org) 1.0.0; installs from the Cursor Marketplace or a team marketplace |

Details in [Plugin & slash commands](plugin.md).

## Shared background server (HTTP)

One installer, one background server, every client and IDE window on the machine
pointing at it:

```bash
curl -fsSL https://raw.githubusercontent.com/harshithsunku/mcp-gtags-server/main/scripts/install.sh | bash
```

It installs the package with `uv`, sets up the toolchain, starts
`mcp-gtags-server --transport http --host 127.0.0.1 --port 8383`, and prints the
client configuration. Re-run it any time to update.

```bash
claude mcp add --scope user --transport http gtags http://127.0.0.1:8383/mcp
```

!!! note "Security"
    The HTTP endpoint is unauthenticated. It binds `127.0.0.1` by default, and the
    SDK enables DNS-rebinding protection for localhost binds. Only bind `0.0.0.0`
    on networks you trust.

## Claude Desktop (.mcpb bundle)

Download `mcp-gtags-server.mcpb` from the
[latest release](https://github.com/harshithsunku/mcp-gtags-server/releases/latest)
and open it with Claude Desktop. The bundle vendors the Python package, so it runs
on any system `python3` ≥ 3.10; the gtags toolchain still installs itself on first
use.

## Manual toolchain (optional)

Prefer system packages, or already have GNU Global? Install it yourself and the
server will use it:

```bash
# EITHER user-space, no sudo:
uvx mcp-gtags-server setup

# OR a system package:
sudo apt install global universal-ctags python3-pygments   # Debian/Ubuntu
brew install global universal-ctags                        # macOS
```

`--no-auto-setup` (or `GTAGS_MCP_AUTO_SETUP=0`) disables the automatic install so
tools fail with instructions instead.

## Verify

```bash
uvx mcp-gtags-server doctor     # what the server detects here
uvx mcp-gtags-server config     # ready-to-paste client configuration
```

`doctor` reports the resolved binaries, the parser label, guard scanning, the
auto-setup state and where the index lives for the current project.

## Update and uninstall

```bash
uvx --refresh mcp-gtags-server --version    # pick up a new release
uv tool upgrade mcp-gtags-server            # if installed with uv tool install
claude mcp remove gtags                     # remove the manual entry
rm -rf ~/.gtags-mcp                         # remove the managed toolchain
rm -rf <project>/.gtags-mcp                 # remove one project's index
```

## Requirements

- Python ≥ 3.10 (provided by `uv` when you use `uvx`)
- Linux or macOS for the prebuilt GNU Global binaries (glibc ≥ 2.28 on Linux;
  older hosts fall back to a source build, which needs `make` and a C compiler)
- Git is optional — it is used to respect `.gitignore` when collecting files
