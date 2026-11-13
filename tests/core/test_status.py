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
    monkeypatch.setattr(status, "CONFIG_FILE", config_file)
    monkeypatch.setattr(setup, "INSTALL_DIR", install_dir)
    monkeypatch.setattr(setup, "VENV_DIR", install_dir / "venv")

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

    def test_installed_reflects_the_install_dir_not_the_venv(self, status_paths):
        from core.setup.status import collect_status

        assert not (status_paths["install_dir"] / "venv").exists()
        assert collect_status()["installed"] is True
        status_paths["config_file"].parent.rmdir()
        assert collect_status()["installed"] is False

    def test_reports_the_running_arize_harness(self, status_paths, monkeypatch, tmp_path):
        from core.setup import status as status_mod

        monkeypatch.setattr(status_mod, "arize_harness_path", lambda: tmp_path / "arize-harness")
        assert status_mod.collect_status()["arize_harness"] == str(tmp_path / "arize-harness")

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
    """Tests for whether a harness's hooks are actually wired up.

    Ours: an ``arize-harness hook <harness>`` command (from any arize-harness)
    or an old ``~/.arize/harness/venv`` ``arize-hook-*`` one. Registered only
    when every such command exists.
    """

    @pytest.fixture
    def exe(self, tmp_path) -> Path:
        path = tmp_path / "local bin" / "arize-harness"
        path.parent.mkdir(parents=True)
        path.write_text("")
        return path

    def _settings(self, tmp_path, monkeypatch, data, name="settings.json") -> Path:
        from core.setup import status as status_mod

        settings = tmp_path / name
        settings.write_text(data if isinstance(data, str) else json.dumps(data))
        monkeypatch.setattr(status_mod, "_registration_paths", lambda h: [settings])
        return settings

    def test_registered_when_our_command_exists(self, status_paths, tmp_path, monkeypatch, exe):
        from core.setup import status as status_mod

        cmd = f"'{exe.as_posix()}' hook claude stop"
        settings = self._settings(tmp_path, monkeypatch, {"hooks": {"Stop": [{"hooks": [{"command": cmd}]}]}})
        assert status_mod._registration_state("claude-code") == (True, str(settings), None)

    def test_not_registered_when_the_command_is_gone(self, status_paths, tmp_path, monkeypatch, exe):
        from core.setup import status as status_mod

        cmd = f"'{exe.as_posix()}' hook claude stop"
        settings = self._settings(tmp_path, monkeypatch, {"hooks": {"Stop": [{"hooks": [{"command": cmd}]}]}})
        exe.unlink()
        assert status_mod._registration_state("claude-code") == (False, str(settings), exe.as_posix())

    def test_another_harnesses_command_does_not_count(self, status_paths, tmp_path, monkeypatch, exe):
        from core.setup import status as status_mod

        settings = self._settings(tmp_path, monkeypatch, {"hooks": {"stop": [{"command": f"'{exe}' hook cursor"}]}})
        assert status_mod._registration_state("claude-code") == (False, str(settings), None)

    def test_old_venv_entry_counts_while_the_venv_exists(self, status_paths, tmp_path, monkeypatch):
        from core.setup import status as status_mod

        old = status_paths["install_dir"] / "venv" / "bin" / "arize-hook-stop"
        settings = self._settings(tmp_path, monkeypatch, {"hooks": {"Stop": [{"hooks": [{"command": str(old)}]}]}})
        assert status_mod._registration_state("claude-code") == (False, str(settings), str(old))
        old.parent.mkdir(parents=True)
        old.write_text("")
        assert status_mod._registration_state("claude-code") == (True, str(settings), None)

    def test_not_registered_when_file_is_someone_elses(self, status_paths, tmp_path, monkeypatch):
        from core.setup import status as status_mod

        settings = self._settings(tmp_path, monkeypatch, {"hooks": {"Stop": "/usr/bin/unrelated"}})
        assert status_mod._registration_state("claude-code") == (False, str(settings), None)

    def test_not_registered_when_file_absent(self, status_paths, tmp_path, monkeypatch):
        from core.setup import status as status_mod

        missing = tmp_path / "nope.json"
        monkeypatch.setattr(status_mod, "_registration_paths", lambda h: [missing])

        registered, path, _ = status_mod._registration_state("claude-code")

        assert registered is False
        assert path == str(missing)

    def test_windows_path_inside_json(self, status_paths, tmp_path, monkeypatch):
        """JSON escapes backslashes; status reads the decoded string, so either spelling is found."""
        from core.setup import status as status_mod

        exe = tmp_path / "arize-harness.exe"
        exe.write_text("")
        cmd = "& '" + exe.as_posix() + "' hook gemini before-tool"
        settings = self._settings(tmp_path, monkeypatch, {"hooks": {"BeforeTool": [{"hooks": [{"command": cmd}]}]}})
        assert status_mod._registration_state("gemini") == (True, str(settings), None)

    def test_jsonc_comments_do_not_hide_a_registration(self, status_paths, tmp_path, monkeypatch, exe):
        from core.setup import status as status_mod

        text = (
            '// devin config\n{"hooks": {"Stop": [{"hooks": [{"command": "\''
            + exe.as_posix()
            + "' hook devin\"}]}],}}\n"
        )
        settings = self._settings(tmp_path, monkeypatch, text)
        assert status_mod._registration_state("devin") == (True, str(settings), None)

    def test_codex_notify_argv(self, status_paths, tmp_path, monkeypatch, exe):
        from core.setup import status as status_mod

        text = f'notify = ["{exe.as_posix()}", "hook", "codex", "notify"]\n'
        settings = self._settings(tmp_path, monkeypatch, text, name="config.toml")
        assert status_mod._registration_state("codex") == (True, str(settings), None)

    def test_generated_ts_plugin(self, status_paths, tmp_path, monkeypatch, exe):
        from core.setup import status as status_mod

        text = (
            "function binaryPath(): string {\n  return " + json.dumps(str(exe)) + "\n}\n"
            'const child = spawn(binaryPath(), ["hook","opencode"], {\n'
        )
        plugin = self._settings(tmp_path, monkeypatch, text, name="arize-tracing.ts")
        assert status_mod._registration_state("opencode") == (True, str(plugin), None)
        exe.unlink()
        assert status_mod._registration_state("opencode")[0] is False

    def test_old_ts_plugin_points_at_the_venv(self, status_paths, tmp_path, monkeypatch):
        from core.setup import status as status_mod

        text = (
            'const base = join(homedir(), ".arize", "harness", "venv")\n'
            'return platform() === "win32" ? join(base, "Scripts", "arize-hook-omp.exe") : join(base, "bin", "arize-hook-omp")\n'
        )
        plugin = self._settings(tmp_path, monkeypatch, text, name="arize-tracing.ts")
        registered, path, missing = status_mod._registration_state("omp")
        assert (registered, path) == (False, str(plugin))
        assert missing.endswith("arize-hook-omp")

    def test_sibling_install_dir_does_not_count(self, status_paths, tmp_path, monkeypatch):
        """`harness-old` is not `harness`: a stale install must not read as wired up."""
        from core.setup import status as status_mod

        stale = str(status_paths["install_dir"]) + "-old"
        settings = self._settings(tmp_path, monkeypatch, {"hook": f"{stale}/venv/bin/arize-hook-stop"})
        assert status_mod._registration_state("claude-code") == (False, str(settings), None)

    def test_unknown_harness_reports_none_not_false(self, status_paths):
        """Never claim 'not registered' for something we cannot check."""
        from core.setup.status import _registration_state

        assert _registration_state("not-a-real-harness") == (None, None, None)

    def test_later_path_can_match_when_first_does_not(self, status_paths, tmp_path, monkeypatch):
        """omp registers via settings.json OR a plugin file — either counts."""
        from core.setup import status as status_mod

        exe = tmp_path / "arize-harness"
        exe.write_text("")
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"unrelated": True}))
        plugin = tmp_path / "arize-tracing.ts"
        plugin.write_text(
            "function binaryPath(): string {\n  return " + json.dumps(str(exe)) + ";\n}\n"
            'spawn(binaryPath(), ["hook","omp"], {'
        )
        monkeypatch.setattr(status_mod, "_registration_paths", lambda h: [settings, plugin])

        assert status_mod._registration_state("omp") == (True, str(plugin), None)

    def test_reports_inspected_path_when_none_match(self, status_paths, tmp_path, monkeypatch):
        """A False verdict should name a file that actually exists."""
        from core.setup import status as status_mod

        missing = tmp_path / "absent.json"
        present = tmp_path / "settings.json"
        present.write_text(json.dumps({"unrelated": True}))
        monkeypatch.setattr(status_mod, "_registration_paths", lambda h: [missing, present])

        assert status_mod._registration_state("omp") == (False, str(present), None)

    def test_directory_scanned_for_our_command(self, status_paths, tmp_path, monkeypatch):
        """Kiro registers into an agents directory, not a single file."""
        from core.setup import status as status_mod

        exe = tmp_path / "arize-harness"
        exe.write_text("")
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "other.json").write_text("{}")
        (agents / "arize-traced.json").write_text(json.dumps({"hooks": {"stop": [{"command": f"'{exe}' hook kiro"}]}}))
        monkeypatch.setattr(status_mod, "_registration_paths", lambda h: [agents])

        assert status_mod._registration_state("kiro")[0] is True

    def test_every_installable_harness_is_in_the_map(self):
        """A harness install.sh can install must be one status can check.

        Devin landed on main while this was in review and was missing here, so
        `status` reported "registration unknown" for it — and since only an
        explicit False counts as unregistered, the install still reported healthy
        with its hooks absent. Derived from install.sh so the next harness fails
        here instead of going unnoticed.
        """
        import re

        from core.setup.status import _REGISTRATION

        install_sh = Path(__file__).resolve().parents[2] / "install.sh"
        dispatch = re.search(r"^        (claude\|[a-z|]+)\)$", install_sh.read_text(), re.MULTILINE)
        assert dispatch, "could not find the harness dispatch line in install.sh"
        cli_names = set(dispatch.group(1).split("|"))

        # status keys are config keys (HARNESS_NAME); claude is the one that differs.
        covered = set(_REGISTRATION) | {"claude"}
        missing = cli_names - covered
        assert not missing, f"harnesses installable but unknown to status: {sorted(missing)}"

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
        exe = tmp_path / "arize-harness"
        exe.write_text("")
        settings = tmp_path / "config.toml"
        settings.write_text(f'notify = ["{exe}", "hook", "codex", "notify"]\n')
        monkeypatch.setattr(status_mod, "_registration_paths", lambda h: [settings])

        assert status_mod.main([]) == 0

    def test_exit_2_and_names_the_missing_command(self, status_paths, tmp_path, monkeypatch, capsys):
        from core.setup import status as status_mod

        _write_config(status_paths, {"harnesses": {"codex": {"target": "arize", "api_key": "k"}}})
        gone = tmp_path / "gone" / "arize-harness"
        settings = tmp_path / "config.toml"
        settings.write_text(f'notify = ["{gone}", "hook", "codex", "notify"]\n')
        monkeypatch.setattr(status_mod, "_registration_paths", lambda h: [settings])

        assert status_mod.main([]) == 2
        out = capsys.readouterr().out
        assert "NOT registered" in out and f"{gone} does not exist" in out

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
