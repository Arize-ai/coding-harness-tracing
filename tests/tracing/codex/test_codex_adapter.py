#!/usr/bin/env python3
"""Tests for tracing.codex.hooks.adapter — env-file loader + check_requirements.

The earlier hook-based architecture is gone; the adapter is now a tiny module
holding constants, the env-file loader, and a tracing-enabled check.
"""
import os

from tracing.codex.hooks import adapter

# ---------------------------------------------------------------------------
# load_env_file
# ---------------------------------------------------------------------------


class TestLoadEnvFile:

    def test_loads_simple_vars(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TEST_CODEX_FOO", raising=False)
        monkeypatch.delenv("TEST_CODEX_BAZ", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("TEST_CODEX_FOO=bar\nTEST_CODEX_BAZ=qux\n")
        adapter.load_env_file(env_file)
        assert os.environ["TEST_CODEX_FOO"] == "bar"
        assert os.environ["TEST_CODEX_BAZ"] == "qux"
        monkeypatch.delenv("TEST_CODEX_FOO", raising=False)
        monkeypatch.delenv("TEST_CODEX_BAZ", raising=False)

    def test_handles_export_prefix(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TEST_CODEX_EXPORT_KEY", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("export TEST_CODEX_EXPORT_KEY=val\n")
        adapter.load_env_file(env_file)
        assert os.environ["TEST_CODEX_EXPORT_KEY"] == "val"
        monkeypatch.delenv("TEST_CODEX_EXPORT_KEY", raising=False)

    def test_strips_quotes(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TEST_CODEX_DQ", raising=False)
        monkeypatch.delenv("TEST_CODEX_SQ", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("TEST_CODEX_DQ=\"quoted\"\nTEST_CODEX_SQ='single'\n")
        adapter.load_env_file(env_file)
        assert os.environ["TEST_CODEX_DQ"] == "quoted"
        assert os.environ["TEST_CODEX_SQ"] == "single"
        monkeypatch.delenv("TEST_CODEX_DQ", raising=False)
        monkeypatch.delenv("TEST_CODEX_SQ", raising=False)

    def test_skips_comments_and_blanks(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TEST_CODEX_ONLY", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("# this is a comment\n\nTEST_CODEX_ONLY=val\n")
        adapter.load_env_file(env_file)
        assert os.environ["TEST_CODEX_ONLY"] == "val"
        monkeypatch.delenv("TEST_CODEX_ONLY", raising=False)

    def test_skips_lines_without_equals(self, tmp_path, monkeypatch):
        monkeypatch.delenv("NOEQUALS", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("NOEQUALS\n")
        adapter.load_env_file(env_file)
        assert "NOEQUALS" not in os.environ

    def test_missing_file_no_error(self, tmp_path):
        adapter.load_env_file(tmp_path / "does_not_exist.env")  # should not raise


# ---------------------------------------------------------------------------
# check_requirements
# ---------------------------------------------------------------------------


class TestCheckRequirements:

    def test_enabled_returns_true(self, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        assert adapter.check_requirements() is True

    def test_disabled_returns_false(self, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "false")
        assert adapter.check_requirements() is False
