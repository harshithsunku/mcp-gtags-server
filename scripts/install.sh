#!/usr/bin/env bash
# mcp-gtags-server one-line installer — 100% user space, never needs sudo.
#
#   curl -fsSL https://raw.githubusercontent.com/harshithsunku/mcp-gtags-server/main/scripts/install.sh | bash
#
# What it does (all inside your home directory):
#   1. Ensures `uv` is available (~/.local/bin) — installs it if missing.
#   2. Installs OR UPDATES the mcp-gtags-server package (release-driven:
#      every GitHub tag published to PyPI is picked up automatically).
#   3. Runs `gtags-mcp setup`: installs GNU Global (prebuilt binaries when
#      available, otherwise built from source) plus universal-ctags and
#      Pygments into ~/.gtags-mcp. On updates, outdated toolchains are
#      wiped and reinstalled automatically.
#   4. Starts (or restarts after an update) the MCP server in the
#      background over HTTP, then prints the client configuration.
#
# Safe to re-run any time:
#   - up to date  -> "already installed", prints the config again
#   - new release -> updates everything, restarts the background server
#
# Environment overrides:
#   GTAGS_MCP_PORT=8383        HTTP port for the background server
#   GTAGS_MCP_HOST=127.0.0.1   bind address (0.0.0.0 exposes on the network)
#   GTAGS_MCP_NO_SERVER=1      skip the background server (stdio-only usage)

set -euo pipefail

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
    Linux|Darwin) ;;
    *) die "unsupported OS: $(uname -s) (Linux and macOS only)" ;;
esac

command -v curl >/dev/null 2>&1 || die "curl is required"

export PATH="$HOME/.local/bin:$PATH"

HOST="${GTAGS_MCP_HOST:-127.0.0.1}"
PORT="${GTAGS_MCP_PORT:-8383}"
GTAGS_HOME="${GTAGS_MCP_HOME:-$HOME/.gtags-mcp}"
PID_FILE="$GTAGS_HOME/server.pid"
LOG_FILE="$GTAGS_HOME/server.log"

installed_version() {
    command -v gtags-mcp >/dev/null 2>&1 && gtags-mcp --version 2>/dev/null | awk '{print $NF}' || true
}

server_running() {
    [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

stop_server() {
    if server_running; then
        say "Stopping background server (pid $(cat "$PID_FILE")) ..."
        kill "$(cat "$PID_FILE")" 2>/dev/null || true
        sleep 1
    fi
    rm -f "$PID_FILE"
}

start_server() {
    mkdir -p "$GTAGS_HOME"
    say "Starting background MCP server on ${HOST}:${PORT} ..."
    nohup gtags-mcp --transport http --host "$HOST" --port "$PORT" \
        >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    sleep 2
    if ! server_running; then
        rm -f "$PID_FILE"
        warn "background server failed to start — see $LOG_FILE"
        warn "stdio transport still works; see the config below."
        return 1
    fi
    say "Server running (pid $(cat "$PID_FILE"), log: $LOG_FILE)"
}

print_config() {
    echo
    if server_running; then
        gtags-mcp config --transport http --host "$HOST" --port "$PORT"
        echo
        echo "  (Prefer per-client processes? The stdio variant also works:)"
        echo "      claude mcp add --scope user gtags -- gtags-mcp"
    else
        gtags-mcp config
    fi
    echo
    echo "  Sanity check any time:   gtags-mcp doctor"
    echo "  No per-repo installs — every repo is indexed automatically on"
    echo "  the first query."
}

# --- 1. uv ------------------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
    say "Installing uv (user-space Python tool manager) ..."
    curl -fsSL https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
    command -v uv >/dev/null 2>&1 || die "uv installation failed"
fi

# --- 2. mcp-gtags-server (install or update) ----------------------------------
BEFORE="$(installed_version)"
say "Checking mcp-gtags-server ..."
uv tool install --upgrade --quiet mcp-gtags-server
command -v gtags-mcp >/dev/null 2>&1 || die "gtags-mcp not on PATH after install"
AFTER="$(installed_version)"

UPDATED=0
if [[ -z "$BEFORE" ]]; then
    say "Installed mcp-gtags-server v${AFTER}"
elif [[ "$BEFORE" == "$AFTER" ]]; then
    say "Already installed and up to date (v${AFTER}) — nothing to install"
else
    say "Updated mcp-gtags-server v${BEFORE} -> v${AFTER}"
    UPDATED=1
fi

# --- 3. gtags toolchain (idempotent; self-heals after updates) ----------------
say "Verifying gtags toolchain in $GTAGS_HOME (no sudo) ..."
gtags-mcp setup

# --- 4. PATH persistence ------------------------------------------------------
ensure_path_line='export PATH="$HOME/.local/bin:$PATH"'
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
        if [[ -f "$rc" ]] && ! grep -qs '\.local/bin' "$rc"; then
            printf '\n# added by mcp-gtags-server installer\n%s\n' "$ensure_path_line" >> "$rc"
            say "Added ~/.local/bin to PATH in $rc"
        fi
    done
fi

# --- 5. background server ------------------------------------------------------
if [[ "${GTAGS_MCP_NO_SERVER:-0}" == "1" ]]; then
    say "GTAGS_MCP_NO_SERVER=1 — skipping the background server"
elif server_running && [[ "$UPDATED" == "0" ]]; then
    say "Background server already running (pid $(cat "$PID_FILE"))"
else
    stop_server
    start_server || true
fi

# --- 6. print the client config ------------------------------------------------
say "All set! Connect your tools with the configuration below:"
print_config
