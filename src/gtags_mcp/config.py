"""Layered configuration for the gtags MCP server.

Two optional TOML files feed the server's defaults, so nothing ever *needs*
to be configured on the command line:

- **Project config** — ``.gtags-mcp.toml`` at the project root, checked into
  the repo like ``.cursor``/``.editorconfig`` so a whole team shares it.
- **User config** — ``~/.config/gtags-mcp/config.toml`` (respects
  ``XDG_CONFIG_HOME``), for per-machine defaults.

Precedence (highest wins) for every setting:

    tool-call argument > CLI flag > environment variable
        > project config > user config > built-in default

Recognised keys (all optional)::

    root = "/abs/path/to/project"   # default project root
    label = "native-pygments"       # GTAGSLABEL parser label to force
    bin_dir = "~/.gtags-mcp/bin"    # extra directory searched for binaries
    skip_globs = ["*.gen.c", "third_party/*"]  # never index matching paths
    respect_gitignore = true        # use `git ls-files` to honour .gitignore
    enrich = true                   # ctags kind/signature/scope on results
    guards = true                   # #ifdef guard stacks on results
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on Python 3.10
    import tomli as tomllib

PROJECT_CONFIG_NAME = ".gtags-mcp.toml"

_VALID_KEYS = frozenset(
    {"root", "label", "bin_dir", "skip_globs", "respect_gitignore", "enrich", "guards"}
)

# Caches: project configs keyed by directory, user config loaded once.
_project_cache: dict[Path, dict] = {}
_user_cache: dict | None = None
_user_cache_loaded = False


def reset_cache() -> None:
    """Forget every cached config file (used by tests)."""
    global _user_cache, _user_cache_loaded
    _project_cache.clear()
    _user_cache = None
    _user_cache_loaded = False


def user_config_path() -> Path:
    """Path of the user-level config file (may not exist)."""
    base = os.environ.get("XDG_CONFIG_HOME", "")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "gtags-mcp" / "config.toml"


def _normalize(value):
    """Coerce a raw TOML value into a supported type, or None to drop it."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (str, int)):
        return str(value)
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    return None


def _load_toml(path: Path) -> dict:
    """Parse a TOML file, keeping only recognised keys. Empty dict on error."""
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        print(f"gtags-mcp: warning: ignoring bad config {path}: {exc}", file=sys.stderr)
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        k: normalized
        for k, v in data.items()
        if k in _VALID_KEYS and (normalized := _normalize(v)) is not None
    }


def load_user_config() -> dict:
    """Settings from ``~/.config/gtags-mcp/config.toml`` (cached)."""
    global _user_cache, _user_cache_loaded
    if not _user_cache_loaded:
        path = user_config_path()
        _user_cache = _load_toml(path) if path.is_file() else {}
        _user_cache_loaded = True
    return dict(_user_cache or {})


def load_project_config(root: Path | None) -> dict:
    """Settings from ``<root>/.gtags-mcp.toml`` (cached per directory)."""
    if root is None:
        return {}
    root = root.expanduser().resolve()
    if root not in _project_cache:
        path = root / PROJECT_CONFIG_NAME
        _project_cache[root] = _load_toml(path) if path.is_file() else {}
    return dict(_project_cache[root])


def _raw_setting(key: str, project_root: Path | None):
    value = load_project_config(project_root).get(key)
    if value is None:
        value = load_user_config().get(key)
    return value


def get_setting(key: str, project_root: Path | None = None) -> str | None:
    """Resolve one string setting from project config, then user config.

    Only the *config file* layers live here; argument/flag/env precedence is
    applied by the callers, which pass through to this as their fallback.
    """
    value = _raw_setting(key, project_root)
    return value if isinstance(value, str) else None


def get_list_setting(key: str, project_root: Path | None = None) -> list[str]:
    """Resolve one list-of-strings setting (empty list when unset)."""
    value = _raw_setting(key, project_root)
    return value if isinstance(value, list) else []


def get_bool_setting(
    key: str, project_root: Path | None = None, default: bool = False
) -> bool:
    """Resolve one boolean setting."""
    value = _raw_setting(key, project_root)
    return value if isinstance(value, bool) else default
