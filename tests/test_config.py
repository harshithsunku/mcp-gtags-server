"""Tests for the layered TOML config (project + user files, precedence)."""

from pathlib import Path

import pytest

from gtags_mcp import config


@pytest.fixture(autouse=True)
def fresh_config_cache():
    config.reset_cache()
    yield
    config.reset_cache()


@pytest.fixture
def user_config(tmp_path, monkeypatch):
    """Point XDG_CONFIG_HOME at a temp dir and return the config file path."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    path = tmp_path / "xdg" / "gtags-mcp" / "config.toml"
    path.parent.mkdir(parents=True)
    return path


def test_no_config_files(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-xdg"))
    assert config.get_setting("label", tmp_path) is None
    assert config.get_setting("root") is None


def test_user_config(user_config):
    user_config.write_text('label = "pygments"\nbin_dir = "~/tools/bin"\n')
    assert config.get_setting("label") == "pygments"
    assert config.get_setting("bin_dir") == "~/tools/bin"


def test_project_config_overrides_user(tmp_path, user_config):
    user_config.write_text('label = "pygments"\n')
    (tmp_path / config.PROJECT_CONFIG_NAME).write_text('label = "default"\n')
    assert config.get_setting("label", tmp_path) == "default"
    # Without a project root, only the user layer applies.
    assert config.get_setting("label") == "pygments"


def test_unknown_keys_ignored(tmp_path, user_config):
    (tmp_path / config.PROJECT_CONFIG_NAME).write_text(
        'label = "default"\nnot_a_real_key = "boom"\n'
    )
    loaded = config.load_project_config(tmp_path)
    assert loaded == {"label": "default"}


def test_malformed_toml_is_ignored(tmp_path, user_config, capsys):
    (tmp_path / config.PROJECT_CONFIG_NAME).write_text("label = [unclosed\n")
    assert config.get_setting("label", tmp_path) is None
    assert "ignoring bad config" in capsys.readouterr().err


def test_list_and_bool_settings(tmp_path, user_config):
    (tmp_path / config.PROJECT_CONFIG_NAME).write_text(
        'skip_globs = ["*.gen.c", "third_party/*"]\nrespect_gitignore = false\n'
    )
    assert config.get_list_setting("skip_globs", tmp_path) == ["*.gen.c", "third_party/*"]
    assert config.get_bool_setting("respect_gitignore", tmp_path, default=True) is False
    # Unset keys fall back to their defaults.
    assert config.get_list_setting("skip_globs") == []
    assert config.get_bool_setting("respect_gitignore", default=True) is True


def test_wrong_typed_values_are_dropped(tmp_path, user_config):
    (tmp_path / config.PROJECT_CONFIG_NAME).write_text(
        'label = ["not", "a", "string"]\nskip_globs = "not-a-list"\n'
    )
    assert config.get_setting("label", tmp_path) is None
    assert config.get_list_setting("skip_globs", tmp_path) == []


def test_user_config_path_respects_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config.user_config_path() == tmp_path / "gtags-mcp" / "config.toml"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert config.user_config_path() == Path.home() / ".config" / "gtags-mcp" / "config.toml"


def test_project_config_cached_per_directory(tmp_path, user_config):
    cfg = tmp_path / config.PROJECT_CONFIG_NAME
    cfg.write_text('label = "default"\n')
    assert config.get_setting("label", tmp_path) == "default"
    cfg.write_text('label = "changed"\n')
    # Cached until reset.
    assert config.get_setting("label", tmp_path) == "default"
    config.reset_cache()
    assert config.get_setting("label", tmp_path) == "changed"


def test_cwd_fallback_is_a_recognised_setting(tmp_path, monkeypatch):
    """The knob is advertised by --no-cwd-fallback's help, so a config file
    must actually be able to set it (unknown keys are silently dropped)."""
    from gtags_mcp import server

    (tmp_path / config.PROJECT_CONFIG_NAME).write_text("cwd_fallback = false\n")
    assert config.load_project_config(tmp_path) == {"cwd_fallback": False}

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(server, "_no_cwd_fallback", False)
    monkeypatch.delenv("GTAGS_MCP_CWD_FALLBACK", raising=False)
    # A user-config-only knob: the project config for a root we haven't resolved
    # yet can't gate root resolution, so read it the way the server does.
    assert config.get_bool_setting("cwd_fallback", tmp_path, default=True) is False
