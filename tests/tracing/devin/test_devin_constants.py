"""Tests for tracing.devin.constants — per-platform Devin path resolution.

Devin's user config lives at ``~/.config/devin/config.json`` on macOS/Linux but
at ``%APPDATA%\\devin\\config.json`` on Windows (their docs call out that it is
*not* under ``~\\.config``), so registering hooks at the POSIX path on Windows
would write to a file Devin never reads.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tracing.devin import constants


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def _as_windows(monkeypatch):
    monkeypatch.setattr(constants.platform, "system", lambda: "Windows")


def _as_posix(monkeypatch):
    monkeypatch.setattr(constants.platform, "system", lambda: "Darwin")


class TestConfigDir:
    def test_posix_uses_xdg_config_home_layout(self, fake_home, monkeypatch):
        _as_posix(monkeypatch)

        assert constants.config_dir() == fake_home / ".config" / "devin"

    def test_windows_uses_appdata(self, fake_home, monkeypatch):
        _as_windows(monkeypatch)
        monkeypatch.setenv("APPDATA", str(fake_home / "AppData" / "Roaming"))

        assert constants.config_dir() == fake_home / "AppData" / "Roaming" / "devin"

    def test_windows_falls_back_when_appdata_unset(self, fake_home, monkeypatch):
        _as_windows(monkeypatch)
        monkeypatch.delenv("APPDATA", raising=False)

        assert constants.config_dir() == fake_home / "AppData" / "Roaming" / "devin"


class TestDataDir:
    def test_posix_uses_local_share(self, fake_home, monkeypatch):
        _as_posix(monkeypatch)

        assert constants.data_dir() == fake_home / ".local" / "share" / "devin" / "cli"

    def test_windows_uses_same_home_relative_path(self, fake_home, monkeypatch):
        """Unlike the config file, the DB stays home-relative on Windows:
        %USERPROFILE%\\.local\\share\\devin\\cli, not %APPDATA% or %LOCALAPPDATA%."""
        _as_windows(monkeypatch)
        monkeypatch.setenv("LOCALAPPDATA", str(fake_home / "AppData" / "Local"))
        monkeypatch.setenv("APPDATA", str(fake_home / "AppData" / "Roaming"))

        assert constants.data_dir() == fake_home / ".local" / "share" / "devin" / "cli"


class TestHarnessHomeSubdir:
    def test_posix(self, fake_home, monkeypatch):
        _as_posix(monkeypatch)

        assert Path(constants.harness_home_subdir()) == Path(".config/devin")

    def test_windows_is_relative_to_home(self, fake_home, monkeypatch):
        _as_windows(monkeypatch)
        monkeypatch.setenv("APPDATA", str(fake_home / "AppData" / "Roaming"))

        assert Path(constants.harness_home_subdir()) == Path("AppData/Roaming/devin")

    def test_config_dir_outside_home_falls_back(self, fake_home, monkeypatch, tmp_path):
        _as_windows(monkeypatch)
        monkeypatch.setenv("APPDATA", str(tmp_path.parent / "elsewhere" / "Roaming"))

        # Not under home -> the soft check falls back to finding devin on PATH.
        assert Path(constants.harness_home_subdir()) == Path(".config/devin")
