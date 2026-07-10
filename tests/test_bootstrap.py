"""Lazy toolchain bootstrap: auto-install on first use, non-blocking status."""

import json
import threading
import time

import pytest

from gtags_mcp import server, toolchain


@pytest.fixture(autouse=True)
def fresh_bootstrap_state():
    def reset():
        thread = server._bootstrap_thread
        if thread is not None:
            thread.join(timeout=10)
        server._bootstrap_thread = None
        server._bootstrap_error = None
        server._bootstrap_log.clear()
        server._no_auto_setup = False

    reset()
    yield
    reset()


@pytest.fixture
def missing_toolchain(monkeypatch):
    """Make the server see no gtags/global anywhere."""
    monkeypatch.setattr(toolchain, "find_global", lambda *a, **k: None)
    monkeypatch.setattr(toolchain, "find_gtags", lambda *a, **k: None)


def _wait_for_bootstrap(timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while server._bootstrap_thread is not None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server._bootstrap_thread is None, "bootstrap thread did not finish"


def test_ready_toolchain_returns_none():
    if toolchain.find_global() is None:
        pytest.skip("GNU Global not installed")
    assert server._ensure_toolchain() is None
    assert server._bootstrap_thread is None  # no install started


def test_missing_toolchain_starts_install_and_reports_status(
    missing_toolchain, monkeypatch
):
    started = threading.Event()
    release = threading.Event()

    def fake_setup(with_ctags=True, force=False, log=print):
        log("* Installing GNU Global ...")
        started.set()
        release.wait(timeout=10)
        return 0

    monkeypatch.setattr(toolchain, "run_setup", fake_setup)
    try:
        first = server._ensure_toolchain()
        assert "being installed automatically" in first
        assert "Retry" in first
        assert started.wait(timeout=10)
        # Concurrent calls report status without spawning a second install.
        again = server._ensure_toolchain()
        assert "being installed automatically" in again
        assert "Installing GNU Global" in again  # last log line surfaces
        assert sum(
            1
            for t in threading.enumerate()
            if t.name == "gtags-mcp-bootstrap"
        ) == 1
    finally:
        release.set()
    _wait_for_bootstrap()
    assert server._bootstrap_error is None


def test_failed_install_reports_error_once_finished(missing_toolchain, monkeypatch):
    def fake_setup(with_ctags=True, force=False, log=print):
        log("error: download failed")
        return 1

    monkeypatch.setattr(toolchain, "run_setup", fake_setup)
    assert "being installed" in server._ensure_toolchain()
    _wait_for_bootstrap()
    message = server._ensure_toolchain()
    assert "automatic toolchain install failed" in message
    assert "mcp-gtags-server setup" in message
    # Sticky: no new install thread is spawned after a failure.
    assert server._bootstrap_thread is None


def test_auto_setup_disabled_returns_plain_error(missing_toolchain, monkeypatch):
    monkeypatch.setattr(server, "_no_auto_setup", True)
    message = server._ensure_toolchain()
    assert "was not found" in message
    assert server._bootstrap_thread is None


def test_auto_setup_env_toggle(missing_toolchain, monkeypatch):
    monkeypatch.setenv("GTAGS_MCP_AUTO_SETUP", "0")
    assert not server._auto_setup_enabled()
    message = server._ensure_toolchain()
    assert "was not found" in message


def test_tool_returns_initializing_envelope(missing_toolchain, monkeypatch):
    started = threading.Event()

    def fake_setup(with_ctags=True, force=False, log=print):
        started.set()
        return 0

    monkeypatch.setattr(toolchain, "run_setup", fake_setup)
    envelope = json.loads(server.find_definition("anything"))
    assert "being installed automatically" in envelope["error"]
    assert started.wait(timeout=10)
    _wait_for_bootstrap()


def test_successful_install_reprobes_parser_label(missing_toolchain, monkeypatch):
    monkeypatch.setattr(toolchain, "run_setup", lambda **kw: kw["log"]("ok") or 0)
    server._auto_label_resolved = True
    server._ensure_toolchain()
    _wait_for_bootstrap()
    assert server._auto_label_resolved is False
