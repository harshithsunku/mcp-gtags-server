"""Collect the list of files an index build should cover.

Indexing feeds ``gtags`` an explicit file list (``gtags -f -``) instead of
letting it walk the tree, so junk never enters the index:

- In a git repository the list comes from ``git ls-files -co
  --exclude-standard`` — tracked plus untracked files, with ``.gitignore``
  semantics applied exactly as git does.
- Outside git (or when git is unavailable) the tree is walked with a
  denylist of well-known build/vendor/VCS directories.
- Both paths then drop anything matching the ``skip_globs`` config patterns
  (matched against the repo-relative path and against the basename).

Paths are returned relative to the root, which keeps everything the index
stores — and everything the tools report — repo-relative.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
from pathlib import Path

GIT_TIMEOUT_SECONDS = 60

# Directory names never worth indexing when walking a non-git tree.
# .gtags-mcp is our own index directory (server.INDEX_DIR_NAME).
DEFAULT_SKIP_DIRS = frozenset(
    ".git .hg .svn CVS node_modules dist out target "
    ".venv venv __pycache__ .tox .mypy_cache .pytest_cache .cache "
    ".idea .vscode .gtags-mcp".split()
)


def _git_ls_files(root: Path) -> list[str] | None:
    """File list from git, or None when root isn't a usable git work tree."""
    if not (root / ".git").exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-co", "--exclude-standard"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return [line for line in proc.stdout.splitlines() if line]


def _walk_files(root: Path) -> list[str]:
    """Fallback for non-git trees: os.walk minus well-known junk directories."""
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in DEFAULT_SKIP_DIRS)
        rel_dir = os.path.relpath(dirpath, root)
        for name in filenames:
            out.append(name if rel_dir == "." else os.path.join(rel_dir, name))
    return out


def _matches_any(path: str, globs: list[str]) -> bool:
    base = path.rsplit("/", 1)[-1]
    return any(
        fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(base, pat) for pat in globs
    )


def collect_files(
    root: Path,
    skip_globs: list[str] | None = None,
    respect_gitignore: bool = True,
) -> list[str]:
    """Root-relative paths of every file the index should cover."""
    files = _git_ls_files(root) if respect_gitignore else None
    if files is None:
        files = _walk_files(root)
    files = [f.replace(os.sep, "/") for f in files]
    if skip_globs:
        files = [f for f in files if not _matches_any(f, skip_globs)]
    # git lists tracked files even after deletion; gtags errors on them.
    return [f for f in files if (root / f).is_file()]
