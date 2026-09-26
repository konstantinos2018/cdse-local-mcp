"""Where downloads land by default: somewhere visible, under the name the desktop uses."""

from __future__ import annotations

from pathlib import Path

import pytest

from cdse_local_mcp.config import DOWNLOAD_FOLDER_NAME, Settings, default_download_dir


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway home directory, with no XDG configuration unless a test writes one."""
    fake = tmp_path / "home"
    (fake / ".config").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake))
    monkeypatch.setenv("USERPROFILE", str(fake))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return fake


def _user_dirs(home: Path, line: str) -> None:
    (home / ".config" / "user-dirs.dirs").write_text(f"# written by xdg-user-dirs-update\n{line}\n")


def test_without_xdg_configuration_it_uses_downloads(home: Path) -> None:
    assert default_download_dir() == home / "Downloads" / DOWNLOAD_FOLDER_NAME


def test_a_localised_downloads_folder_is_honoured(home: Path) -> None:
    """Hardcoding ~/Downloads would create a second folder beside the real one."""
    _user_dirs(home, 'XDG_DOWNLOAD_DIR="$HOME/Λήψεις"')
    assert default_download_dir() == home / "Λήψεις" / DOWNLOAD_FOLDER_NAME


def test_an_absolute_xdg_path_is_used_as_is(home: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "data" / "incoming"
    _user_dirs(home, f'XDG_DOWNLOAD_DIR="{elsewhere}"')
    assert default_download_dir() == elsewhere / DOWNLOAD_FOLDER_NAME


def test_xdg_config_home_is_respected(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom = tmp_path / "custom-config"
    custom.mkdir()
    (custom / "user-dirs.dirs").write_text('XDG_DOWNLOAD_DIR="$HOME/Incoming"\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(custom))

    assert default_download_dir() == home / "Incoming" / DOWNLOAD_FOLDER_NAME


def test_a_download_dir_set_to_home_means_disabled(home: Path) -> None:
    """The XDG spec's way of switching a user folder off; do not dump files in $HOME."""
    _user_dirs(home, 'XDG_DOWNLOAD_DIR="$HOME/"')
    assert default_download_dir() == home / "Downloads" / DOWNLOAD_FOLDER_NAME


def test_the_default_is_not_hidden(home: Path) -> None:
    """Regression guard: the old default, ~/.cache/cdse-local-mcp, hid people's downloads."""
    relative = default_download_dir().relative_to(home)
    assert not any(part.startswith(".") for part in relative.parts)


def test_the_environment_overrides_the_default(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chosen = tmp_path / "my-eo-data"
    monkeypatch.setenv("CDSE_DOWNLOAD_DIR", str(chosen))
    assert Settings().download_dir == chosen


def test_the_default_applies_when_unset(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CDSE_DOWNLOAD_DIR", raising=False)
    assert Settings().download_dir == home / "Downloads" / DOWNLOAD_FOLDER_NAME
