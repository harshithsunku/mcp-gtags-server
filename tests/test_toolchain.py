"""Tests for user-space binary discovery, relocation, and setup plumbing."""

import hashlib
import os
import shutil
import stat
from pathlib import Path

import pytest

from gtags_mcp import config, toolchain


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch, tmp_path):
    """Isolate managed home, config, and env for every test."""
    config.reset_cache()
    monkeypatch.setenv("GTAGS_MCP_HOME", str(tmp_path / "managed"))
    monkeypatch.delenv("GTAGS_MCP_BIN_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    yield
    config.reset_cache()


def _fake_exe(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    exe = directory / name
    exe.write_text("#!/bin/sh\necho fake\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe


def test_managed_home_env_override(tmp_path):
    assert toolchain.managed_home() == tmp_path / "managed"
    assert toolchain.managed_bin() == tmp_path / "managed" / "bin"


def test_explicit_bin_dir_wins(tmp_path):
    explicit = tmp_path / "explicit"
    _fake_exe(explicit, "global")
    _fake_exe(toolchain.managed_bin(), "global")
    assert toolchain.find_global(bin_dir=str(explicit)) == str(explicit / "global")


def test_managed_dir_beats_path(tmp_path):
    managed = _fake_exe(toolchain.managed_bin(), "global")
    # Even if a system `global` exists, the managed one wins.
    assert toolchain.find_global() == str(managed)


def test_env_bin_dir(tmp_path, monkeypatch):
    envdir = tmp_path / "envbin"
    _fake_exe(envdir, "gtags")
    monkeypatch.setenv("GTAGS_MCP_BIN_DIR", str(envdir))
    assert toolchain.find_gtags() == str(envdir / "gtags")


def test_config_bin_dir(tmp_path):
    confdir = tmp_path / "confbin"
    _fake_exe(confdir, "gtags")
    project = tmp_path / "proj"
    project.mkdir()
    (project / config.PROJECT_CONFIG_NAME).write_text(f'bin_dir = "{confdir}"\n')
    assert toolchain.find_gtags(project_root=project) == str(confdir / "gtags")


def test_find_falls_back_to_path(tmp_path):
    # No managed/explicit binaries: result must match plain PATH lookup.
    assert toolchain.find_global() == shutil.which("global")


def test_find_ctags_tries_all_names(tmp_path):
    exe = _fake_exe(toolchain.managed_bin(), "universal-ctags")
    assert toolchain.find_ctags() == str(exe)


def test_runtime_env_only_for_managed_install(tmp_path):
    managed_global = _fake_exe(toolchain.managed_bin(), "global")
    assert toolchain.runtime_env("/usr/bin/global") == {}
    assert toolchain.runtime_env(None) == {}
    # Managed binary but no generated conf yet: still nothing.
    assert toolchain.runtime_env(str(managed_global)) == {}
    toolchain.managed_conf().write_text("dummy")
    assert toolchain.runtime_env(str(managed_global)) == {
        "GTAGSCONF": str(toolchain.managed_conf())
    }


def test_patch_embedded_prefix(tmp_path):
    old = toolchain.PLACEHOLDER_PREFIX
    new = str(tmp_path / "home")
    blob = (
        b"HEAD\0" + f"{old}/share/gtags/script/x.py".encode() + b"\0TAIL\0"
        + old.encode() + b"\0"
    )
    binary = tmp_path / "fake.so"
    binary.write_bytes(blob)
    toolchain._patch_embedded_prefix(binary, old, new)
    patched = binary.read_bytes()
    assert len(patched) == len(blob)  # in-place, size preserved
    assert f"{new}/share/gtags/script/x.py".encode() + b"\0" in patched
    assert patched.startswith(b"HEAD\0")
    assert patched.endswith(b"\0")


def test_patch_embedded_prefix_rejects_longer_path(tmp_path):
    binary = tmp_path / "fake.so"
    binary.write_bytes(b"x")
    with pytest.raises(toolchain.SetupError):
        toolchain._patch_embedded_prefix(binary, "/short", "/much-longer-prefix")


def test_download_checksum_mismatch(tmp_path, monkeypatch):
    payload = b"payload"

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def __init__(self):
            self._data = [payload, b""]

        def read(self, _size):
            return self._data.pop(0)

    monkeypatch.setattr(
        toolchain.urllib.request, "urlopen", lambda *a, **k: FakeResponse()
    )
    dest = tmp_path / "file"
    with pytest.raises(toolchain.SetupError, match="checksum mismatch"):
        toolchain._download("https://example.invalid/x", dest, expected_sha256="0" * 64)
    assert not dest.exists()  # bad download must be removed

    good = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(
        toolchain.urllib.request, "urlopen", lambda *a, **k: FakeResponse()
    )
    toolchain._download("https://example.invalid/x", dest, expected_sha256=good)
    assert dest.read_bytes() == payload


def test_extract_rejects_path_traversal(tmp_path):
    import io
    import tarfile

    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as tar:
        info = tarfile.TarInfo("../escape.txt")
        data = b"pwned"
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    with pytest.raises(toolchain.SetupError, match="unsafe path"):
        toolchain._extract_tarball(evil, tmp_path / "out")


def test_doctor_report_mentions_setup_when_missing(tmp_path, monkeypatch):
    # Hide any system global from the report.
    monkeypatch.setattr(toolchain.shutil, "which", lambda name: None)
    report = toolchain.doctor_report()
    assert "gtags-mcp setup" in report
    assert "NOT FOUND" in report


def test_platform_tag():
    tag = toolchain._platform_tag()
    assert tag is None or tag.split("-")[0] in ("linux", "macos")
