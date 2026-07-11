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
    binary.write_bytes(b"\0/short\0")
    with pytest.raises(toolchain.SetupError):
        toolchain._patch_embedded_prefix(binary, "/short", "/much-longer-prefix")


def test_patch_embedded_prefix_text_script(tmp_path):
    """Shell scripts in bin/ have no NUL bytes — plain replacement, any length."""
    old = toolchain.PLACEHOLDER_PREFIX
    script = tmp_path / "globash"
    script.write_text(f"#!/bin/sh\nGTAGSHOME={old}/share\nexec {old}/bin/global \"$@\"\n")
    toolchain._patch_embedded_prefix(script, old, "/home/u/.gtags-mcp")
    text = script.read_text()
    assert old not in text
    assert "/home/u/.gtags-mcp/bin/global" in text


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
    assert "mcp-gtags-server setup" in report
    assert "NOT FOUND" in report


def test_platform_tag():
    tag = toolchain._platform_tag()
    assert tag is None or tag.split("-")[0] in ("linux", "macos")


# ---------------------------------------------------------------------------
# v0.8.2: setup must not trust a ctags that can't power enrichment
# ---------------------------------------------------------------------------


def _probing_as(monkeypatch, existing: str, usable: bool):
    """Route find_ctags/probe_binary to a fake existing binary."""
    from gtags_mcp import enrich

    enrich.reset_cache()
    monkeypatch.setattr(toolchain, "find_ctags", lambda *a, **k: existing)
    monkeypatch.setattr(
        enrich, "probe_binary", lambda exe: (usable, f"probed {exe}")
    )


def test_install_ctags_skips_when_existing_is_universal_json(monkeypatch):
    _probing_as(monkeypatch, "/fake/universal-ctags", usable=True)

    def no_network(*args, **kwargs):
        raise AssertionError("install_ctags must not download when ctags is usable")

    monkeypatch.setattr(toolchain.urllib.request, "urlopen", no_network)
    logs = []
    assert toolchain.install_ctags(logs.append) is True
    assert any("already available (Universal +json)" in line for line in logs)


def test_install_ctags_replaces_exuberant(monkeypatch):
    """An Exuberant/json-less ctags must trigger a managed install attempt."""
    import urllib.error

    _probing_as(monkeypatch, "/usr/bin/ctags", usable=False)
    attempted = []

    def fake_urlopen(request, **kwargs):
        attempted.append(request.full_url)
        raise urllib.error.URLError("offline test")

    monkeypatch.setattr(toolchain.urllib.request, "urlopen", fake_urlopen)
    logs = []
    # Download fails (offline) -> graceful False, but the point is it TRIED.
    assert toolchain.install_ctags(logs.append) is False
    assert attempted, "expected a download attempt for universal-ctags"
    assert any("cannot emit JSON output" in line for line in logs)


# ---------------------------------------------------------------------------
# v1.4.2: prebuilt binaries must actually run on the host (glibc compat)
# ---------------------------------------------------------------------------


def _failing_exe(directory: Path, name: str, stderr: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    exe = directory / name
    exe.write_text(f'#!/bin/sh\necho "{stderr}" >&2\nexit 1\n')
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe


def test_host_glibc_detection(monkeypatch):
    monkeypatch.setattr(toolchain.sys, "platform", "linux")
    monkeypatch.setattr(toolchain.os, "confstr", lambda name: "glibc 2.28")
    assert toolchain._host_glibc() == (2, 28)

    # confstr unavailable: fall back to platform.libc_ver().
    def confstr_raises(name):
        raise ValueError(name)

    monkeypatch.setattr(toolchain.os, "confstr", confstr_raises)
    monkeypatch.setattr(toolchain.platform, "libc_ver", lambda: ("glibc", "2.17"))
    assert toolchain._host_glibc() == (2, 17)

    # musl / unknown libc: no version to compare, caller relies on the probe.
    monkeypatch.setattr(toolchain.platform, "libc_ver", lambda: ("", ""))
    assert toolchain._host_glibc() is None

    monkeypatch.setattr(toolchain.sys, "platform", "darwin")
    assert toolchain._host_glibc() is None


def test_prebuilt_skipped_on_old_glibc(monkeypatch):
    monkeypatch.setattr(toolchain, "_platform_tag", lambda: "linux-x86_64")
    monkeypatch.setattr(toolchain, "_host_glibc", lambda: (2, 17))

    def no_network(*args, **kwargs):
        raise AssertionError("must not download prebuilts on an old-glibc host")

    monkeypatch.setattr(toolchain.urllib.request, "urlopen", no_network)
    logs = []
    assert toolchain.install_global_prebuilt(logs.append) is False
    assert any("glibc 2.17" in line for line in logs)


def test_verify_managed_global(tmp_path):
    _fake_exe(toolchain.managed_bin(), "global")
    _fake_exe(toolchain.managed_bin(), "gtags")
    logs = []
    assert toolchain._verify_managed_global(logs.append) is True

    _failing_exe(
        toolchain.managed_bin(), "global", "global: version GLIBC_2.34 not found"
    )
    assert toolchain._verify_managed_global(logs.append) is False
    assert any("GLIBC_2.34" in line for line in logs)


def _prebuilt_release_tarball(tmp_path: Path) -> tuple[Path, str]:
    """A real release-shaped tarball whose binaries fail to execute."""
    import tarfile

    stage = tmp_path / f"global-{toolchain.GLOBAL_VERSION}-linux-x86_64"
    _failing_exe(stage / "bin", "global", "version GLIBC_2.34 not found")
    _failing_exe(stage / "bin", "gtags", "version GLIBC_2.34 not found")
    tarball = tmp_path / f"{stage.name}.tar.gz"
    with tarfile.open(tarball, "w:gz") as tar:
        tar.add(stage, arcname=stage.name)
    return tarball, hashlib.sha256(tarball.read_bytes()).hexdigest()


def test_prebuilt_probe_failure_raises(tmp_path, monkeypatch):
    tarball, sha = _prebuilt_release_tarball(tmp_path)
    monkeypatch.setattr(toolchain, "_platform_tag", lambda: "linux-x86_64")
    monkeypatch.setattr(toolchain, "_host_glibc", lambda: None)

    def fake_download(url, dest, expected_sha256=None):
        if url.endswith("checksums.txt"):
            dest.write_text(f"{sha}  {tarball.name}\n")
        else:
            assert expected_sha256 == sha
            shutil.copy2(tarball, dest)

    monkeypatch.setattr(toolchain, "_download", fake_download)
    logs = []
    with pytest.raises(toolchain.SetupError, match="not runnable"):
        toolchain.install_global_prebuilt(logs.append)
    assert any("does not run on this host" in line for line in logs)


def test_run_setup_falls_back_to_source_on_probe_failure(monkeypatch):
    sentinel = toolchain.managed_home() / "half-installed"

    def broken_prebuilt(log):
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("partial")
        raise toolchain.SetupError("prebuilt binaries are not runnable on this host")

    source_calls = []

    def fake_source_build(log):
        source_calls.append(True)
        _fake_exe(toolchain.managed_bin(), "global")
        _fake_exe(toolchain.managed_bin(), "gtags")

    monkeypatch.setattr(toolchain.shutil, "which", lambda name: None)
    monkeypatch.setattr(toolchain, "install_global_prebuilt", broken_prebuilt)
    monkeypatch.setattr(toolchain, "install_global_from_source", fake_source_build)
    monkeypatch.setattr(toolchain, "install_ctags", lambda log: True)
    monkeypatch.setattr(toolchain, "install_pygments", lambda log: True)
    monkeypatch.setattr(toolchain, "write_relocated_conf", lambda log: None)
    monkeypatch.setattr(toolchain, "doctor_report", lambda: "ok")

    logs = []
    assert toolchain.run_setup(log=logs.append) == 0
    assert source_calls, "expected fallback to the source build"
    assert not sentinel.exists(), "managed home must be wiped before the fallback"
    assert any("falling back to source build" in line for line in logs)


def test_run_setup_self_heals_broken_managed_install(monkeypatch):
    """A managed `global` that cannot run (old glibc) must be wiped, not trusted."""
    _failing_exe(
        toolchain.managed_bin(), "global", "global: version GLIBC_2.34 not found"
    )

    def fake_prebuilt(log):
        _fake_exe(toolchain.managed_bin(), "global")
        _fake_exe(toolchain.managed_bin(), "gtags")
        return True

    monkeypatch.setattr(toolchain.shutil, "which", lambda name: None)
    monkeypatch.setattr(toolchain, "install_global_prebuilt", fake_prebuilt)
    monkeypatch.setattr(toolchain, "install_ctags", lambda log: True)
    monkeypatch.setattr(toolchain, "install_pygments", lambda log: True)
    monkeypatch.setattr(toolchain, "write_relocated_conf", lambda log: None)
    monkeypatch.setattr(toolchain, "doctor_report", lambda: "ok")

    logs = []
    assert toolchain.run_setup(log=logs.append) == 0
    assert any("cannot run" in line for line in logs)
    # The reinstalled binary works again.
    ok, _ = toolchain._try_run([str(toolchain.managed_bin() / "global"), "--version"])
    assert ok


def _ctags_release_tarball(tmp_path: Path) -> Path:
    import tarfile

    stage = tmp_path / "uctags-2026.01.01-linux-x86_64"
    _fake_exe(stage / "bin", "ctags")
    tarball = tmp_path / f"{stage.name}.release.tar.gz"
    with tarfile.open(tarball, "w:gz") as tar:
        tar.add(stage, arcname=stage.name)
    return tarball


def _ctags_nightly_env(monkeypatch, tmp_path):
    """Fake the nightly-release API + download for install_ctags."""
    import io
    import json as jsonlib

    tarball = _ctags_release_tarball(tmp_path)
    release = {
        "assets": [
            {
                "name": tarball.name,
                "browser_download_url": "https://example.invalid/ctags.tar.gz",
            }
        ]
    }

    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(toolchain.sys, "platform", "linux")
    monkeypatch.setattr(toolchain.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(toolchain, "find_ctags", lambda *a, **k: None)
    monkeypatch.setattr(
        toolchain.urllib.request,
        "urlopen",
        lambda *a, **k: FakeResponse(jsonlib.dumps(release).encode()),
    )
    monkeypatch.setattr(
        toolchain,
        "_download",
        lambda url, dest, expected_sha256=None: shutil.copy2(tarball, dest),
    )


def test_install_ctags_removes_broken_download(tmp_path, monkeypatch):
    from gtags_mcp import enrich

    _ctags_nightly_env(monkeypatch, tmp_path)
    enrich.reset_cache()
    monkeypatch.setattr(enrich, "probe_binary", lambda exe: (False, "did not run"))
    logs = []
    assert toolchain.install_ctags(logs.append) is False
    assert not (toolchain.managed_bin() / "ctags").exists()
    assert any("does not work on this host" in line for line in logs)


def test_install_ctags_keeps_working_download(tmp_path, monkeypatch):
    from gtags_mcp import enrich

    _ctags_nightly_env(monkeypatch, tmp_path)
    enrich.reset_cache()
    monkeypatch.setattr(
        enrich, "probe_binary", lambda exe: (True, "Universal Ctags 6.x")
    )
    logs = []
    assert toolchain.install_ctags(logs.append) is True
    assert (toolchain.managed_bin() / "ctags").is_file()


def test_doctor_report_hints_on_glibc_error(tmp_path, monkeypatch):
    _failing_exe(
        toolchain.managed_bin(), "global", "global: version GLIBC_2.34 not found"
    )
    monkeypatch.setattr(toolchain.shutil, "which", lambda name: None)
    report = toolchain.doctor_report()
    assert "setup --force" in report


def test_write_relocated_conf_fixes_pygments_shebang(tmp_path):
    """The shipped parser script's `env python` shebang must become python3."""
    share = toolchain.managed_home() / "share" / "gtags"
    script_dir = share / "script"
    script_dir.mkdir(parents=True)
    (share / "gtags.conf").write_text("default:\\\n\t:tc=native:\n")
    script = script_dir / "pygments_parser.py"
    script.write_text("#!/usr/bin/env python\nimport sys\n")

    toolchain.write_relocated_conf(lambda *_: None)

    assert script.read_text().startswith("#!/usr/bin/env python3\n")
    assert "import sys" in script.read_text()
    # Idempotent: a second run leaves the fixed script alone.
    toolchain.write_relocated_conf(lambda *_: None)
    assert script.read_text().count("python3") == 1
