#!/usr/bin/env python3
"""Tests for core/setup/status.py — the machine-readable install report."""

import json
from pathlib import Path

import pytest


@pytest.fixture
def status_paths(tmp_path, monkeypatch):
    """Point status.py's install dir, venv and config file at tmp_path."""
    import core.setup as setup
    import core.setup.status as status

    install_dir = tmp_path / ".arize" / "harness"
    install_dir.mkdir(parents=True)
    config_file = install_dir / "config.json"

    monkeypatch.setattr(status, "INSTALL_DIR", install_dir)
    monkeypatch.setattr(status, "VENV_DIR", install_dir / "venv")
    monkeypatch.setattr(status, "CONFIG_FILE", config_file)
    monkeypatch.setattr(setup, "INSTALL_DIR", install_dir)

    # Registration lookups otherwise read the developer's real ~/.claude,
    # ~/.codex and friends, so results would vary by machine. Tests that care
    # about registration re-patch this with paths of their own.
    monkeypatch.setattr(status, "_registration_paths", lambda harness: [])

    return {"install_dir": install_dir, "config_file": config_file}


def _write_config(status_paths, config):
    status_paths["config_file"].write_text(json.dumps(config))


class TestCollectStatus:
    """Tests for collect_status()."""

    def test_empty_when_no_config(self, status_paths):
        from core.setup.status import collect_status

        result = collect_status()

        assert result["harnesses"] == []
        assert result["config_exists"] is False
        assert result["installed"] is False

    def test_installed_reflects_venv(self, status_paths):
        from core.setup.status import collect_status

        (status_paths["install_dir"] / "venv").mkdir()

        assert collect_status()["installed"] is True

    def test_reports_harness_fields(self, status_paths):
        from core.setup.status import collect_status

        _write_config(
            status_paths,
            {
                "harnesses": {
                    "claude-code": {
                        "project_name": "my-project",
                        "target": "arize",
                        "endpoint": "otlp.arize.com:443",
                        "api_key": "secret-value",
                        "space_id": "space-abc",
                    }
                },
                "user_id": "alice",
                "logging": {"prompts": True, "tool_details": False, "tool_content": True},
            },
        )

        result = collect_status()
        entry = result["harnesses"][0]

        assert entry["name"] == "claude-code"
        assert entry["project_name"] == "my-project"
        assert entry["target"] == "arize"
        assert entry["space_id"] == "space-abc"
        assert entry["api_key_present"] is True
        assert result["user_id"] == "alice"
        assert result["logging"]["tool_details"] is False

    def test_never_includes_the_api_key(self, status_paths):
        """The whole payload must be safe to paste into a bug report."""
        from core.setup.status import collect_status

        _write_config(
            status_paths,
            {"harnesses": {"claude-code": {"target": "arize", "api_key": "super-secret-key"}}},
        )

        blob = json.dumps(collect_status())

        assert "super-secret-key" not in blob
        assert "api_key_present" in blob

    def test_missing_api_key_reported_absent(self, status_paths):
        from core.setup.status import collect_status

        _write_config(status_paths, {"harnesses": {"codex": {"target": "arize", "api_key": ""}}})

        assert collect_status()["harnesses"][0]["api_key_present"] is False

    def test_no_space_id_key_for_phoenix(self, status_paths):
        from core.setup.status import collect_status

        _write_config(
            status_paths,
            {"harnesses": {"cursor": {"target": "phoenix", "endpoint": "http://localhost:6006"}}},
        )

        assert "space_id" not in collect_status()["harnesses"][0]

    def test_project_name_falls_back_to_harness_name(self, status_paths):
        from core.setup.status import collect_status

        _write_config(status_paths, {"harnesses": {"gemini": {"target": "arize"}}})

        assert collect_status()["harnesses"][0]["project_name"] == "gemini"

    def test_harnesses_sorted(self, status_paths):
        from core.setup.status import collect_status

        _write_config(
            status_paths,
            {"harnesses": {"omp": {"target": "arize"}, "codex": {"target": "arize"}}},
        )

        assert [h["name"] for h in collect_status()["harnesses"]] == ["codex", "omp"]

    def test_malformed_entry_skipped(self, status_paths):
        from core.setup.status import collect_status

        _write_config(status_paths, {"harnesses": {"codex": "not-a-dict"}})

        assert collect_status()["harnesses"] == []


class TestRegistrationDetection:
    """Tests for whether a harness's hooks are actually wired up."""

    def test_registered_when_file_references_install_dir(self, status_paths, tmp_path, monkeypatch):
        from core.setup import status as status_mod

        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"hooks": {"Stop": str(status_paths["install_dir"] / "venv" / "bin" / "hook")}}))
        monkeypatch.setattr(status_mod, "_registration_paths", lambda h: [settings])

        assert status_mod._registration_state("claude-code") == (True, str(settings))

    def test_not_registered_when_file_is_someone_elses(self, status_paths, tmp_path, monkeypatch):
        from core.setup import status as status_mod

        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"hooks": {"Stop": "/usr/bin/unrelated"}}))
        monkeypatch.setattr(status_mod, "_registration_paths", lambda h: [settings])

        assert status_mod._registration_state("claude-code") == (False, str(settings))

    def test_not_registered_when_file_absent(self, status_paths, tmp_path, monkeypatch):
        from core.setup import status as status_mod

        missing = tmp_path / "nope.json"
        monkeypatch.setattr(status_mod, "_registration_paths", lambda h: [missing])

        registered, path = status_mod._registration_state("claude-code")

        assert registered is False
        assert path == str(missing)

    def test_sibling_install_dir_does_not_count(self, status_paths, tmp_path, monkeypatch):
        """`harness-old` must not match the marker `harness` on prefix.

        Reporting a stale install as wired up is a false positive in the one
        command whose job is to tell the truth about that.
        """
        from core.setup import status as status_mod

        stale = str(status_paths["install_dir"]) + "-old"
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"hook": f"{stale}/venv/bin/arize-hook-stop"}))
        monkeypatch.setattr(status_mod, "_registration_paths", lambda h: [settings])

        assert status_mod._registration_state("claude-code") == (False, str(settings))

    def test_unknown_harness_reports_none_not_false(self, status_paths):
        """Never claim 'not registered' for something we cannot check."""
        from core.setup.status import _registration_state

        assert _registration_state("not-a-real-harness") == (None, None)

    def test_later_path_can_match_when_first_does_not(self, status_paths, tmp_path, monkeypatch):
        """omp registers via settings.json OR a plugin file — either counts.

        Stopping at the first existing path reported a false negative when the
        second was the one that matched.
        """
        from core.setup import status as status_mod

        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"unrelated": True}))
        plugin = tmp_path / "arize-tracing.ts"
        plugin.write_text(f"// hook at {status_paths['install_dir']}/venv/bin/arize-hook-omp")
        monkeypatch.setattr(status_mod, "_registration_paths", lambda h: [settings, plugin])

        assert status_mod._registration_state("omp") == (True, str(plugin))

    def test_reports_inspected_path_when_none_match(self, status_paths, tmp_path, monkeypatch):
        """A False verdict should name a file that actually exists."""
        from core.setup import status as status_mod

        missing = tmp_path / "absent.json"
        present = tmp_path / "settings.json"
        present.write_text(json.dumps({"unrelated": True}))
        monkeypatch.setattr(status_mod, "_registration_paths", lambda h: [missing, present])

        assert status_mod._registration_state("omp") == (False, str(present))

    def test_symlinked_plugin_counts_as_registered(self, status_paths, tmp_path, monkeypatch):
        from core.setup import status as status_mod

        source = status_paths["install_dir"] / "plugin.ts"
        source.write_text("// tracing plugin")
        link = tmp_path / "arize-tracing.ts"
        link.symlink_to(source)
        monkeypatch.setattr(status_mod, "_registration_paths", lambda h: [link])

        assert status_mod._registration_state("opencode")[0] is True

    def test_directory_scanned_for_marker(self, status_paths, tmp_path, monkeypatch):
        """Kiro registers into an agents directory, not a single file."""
        from core.setup import status as status_mod

        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "other.json").write_text("{}")
        (agents / "arize-traced.json").write_text(str(status_paths["install_dir"] / "venv" / "bin" / "arize-hook-kiro"))
        monkeypatch.setattr(status_mod, "_registration_paths", lambda h: [agents])

        assert status_mod._registration_state("kiro")[0] is True

    def test_every_known_harness_has_resolvable_paths(self):
        """Guards against a harness being added to the map with a bad constant."""
        from core.setup.status import _REGISTRATION, _registration_paths

        for harness in _REGISTRATION:
            paths = _registration_paths(harness)
            assert paths, f"{harness} resolved no registration paths"
            assert all(isinstance(p, Path) for p in paths)


class TestCli:
    """Tests for the command-line surface."""

    def test_json_output_parses(self, status_paths, capsys):
        from core.setup.status import main

        _write_config(status_paths, {"harnesses": {"codex": {"target": "arize", "api_key": "k"}}})

        assert main(["--json"]) == 0
        assert json.loads(capsys.readouterr().out)["harnesses"][0]["name"] == "codex"

    def test_exit_1_when_nothing_configured(self, status_paths, capsys):
        """So a caller can gate on it without parsing output."""
        from core.setup.status import main

        assert main([]) == 1
        assert "No harnesses configured" in capsys.readouterr().out

    def test_exit_2_when_hooks_missing(self, status_paths, tmp_path, monkeypatch, capsys):
        """Regression: this used to exit 0 while the payload said registered=false.

        Anything gating on the exit code alone concluded the install was fine.
        """
        from core.setup import status as status_mod

        _write_config(status_paths, {"harnesses": {"codex": {"target": "arize", "api_key": "k"}}})
        monkeypatch.setattr(status_mod, "_registration_paths", lambda h: [tmp_path / "absent.toml"])

        assert status_mod.main([]) == 2

        out = capsys.readouterr().out
        assert "Hooks are missing for: codex" in out

    def test_exit_0_when_everything_registered(self, status_paths, tmp_path, monkeypatch):
        from core.setup import status as status_mod

        _write_config(status_paths, {"harnesses": {"codex": {"target": "arize", "api_key": "k"}}})
        settings = tmp_path / "config.toml"
        settings.write_text(str(status_paths["install_dir"] / "venv" / "bin" / "arize-hook-codex-notify"))
        monkeypatch.setattr(status_mod, "_registration_paths", lambda h: [settings])

        assert status_mod.main([]) == 0

    def test_unknown_registration_is_not_treated_as_failure(self, status_paths):
        """Cannot-tell must not be reported as broken — that is guessing too."""
        from core.setup.status import collect_status, main

        _write_config(status_paths, {"harnesses": {"mystery-harness": {"target": "arize", "api_key": "k"}}})

        assert collect_status()["unregistered"] == []
        assert main([]) == 0

    def test_healthy_flag_mirrors_the_exit_code(self, status_paths, tmp_path, monkeypatch):
        """JSON consumers get the same verdict without inspecting each entry."""
        from core.setup import status as status_mod

        _write_config(status_paths, {"harnesses": {"codex": {"target": "arize", "api_key": "k"}}})
        monkeypatch.setattr(status_mod, "_registration_paths", lambda h: [tmp_path / "absent.toml"])

        status = status_mod.collect_status()

        assert status["healthy"] is False
        assert status["unregistered"] == ["codex"]

    def test_healthy_false_when_nothing_configured(self, status_paths):
        from core.setup.status import collect_status

        assert collect_status()["healthy"] is False

    def test_human_output_hides_the_key(self, status_paths, capsys):
        from core.setup.status import main

        _write_config(
            status_paths,
            {"harnesses": {"codex": {"target": "arize", "api_key": "super-secret-key", "space_id": "s"}}},
        )
        main([])

        out = capsys.readouterr().out
        assert "super-secret-key" not in out
        assert "API key:  present" in out
