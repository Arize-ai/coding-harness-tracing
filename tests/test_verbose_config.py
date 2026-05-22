"""Tests for verbose-mode config wiring (config.yaml + _is_verbose() interaction)."""

from __future__ import annotations

import pytest
import yaml

from core import common


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    """Redirect CONFIG_FILE to a tmp path and provide a writer."""
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr("core.constants.CONFIG_FILE", str(config_path))
    monkeypatch.setattr("core.config.CONFIG_FILE", str(config_path))

    def write(data: dict) -> None:
        config_path.write_text(yaml.safe_dump(data))

    return write


def test_verbose_false_by_default(tmp_config, monkeypatch):
    monkeypatch.delenv("ARIZE_VERBOSE", raising=False)
    tmp_config({})
    assert common._is_verbose() is False


def test_verbose_true_from_config(tmp_config, monkeypatch):
    monkeypatch.delenv("ARIZE_VERBOSE", raising=False)
    tmp_config({"verbose": True})
    assert common._is_verbose() is True


def test_env_var_overrides_config_true(tmp_config, monkeypatch):
    monkeypatch.setenv("ARIZE_VERBOSE", "true")
    tmp_config({"verbose": False})
    assert common._is_verbose() is True


def test_env_var_overrides_config_false(tmp_config, monkeypatch):
    monkeypatch.setenv("ARIZE_VERBOSE", "false")
    tmp_config({"verbose": True})
    assert common._is_verbose() is False


def test_write_verbose_config_creates_key(tmp_config, monkeypatch):
    from core.setup import write_verbose_config

    tmp_config({})
    write_verbose_config(True)
    from core.config import load_config

    cfg = load_config()
    assert cfg.get("verbose") is True
