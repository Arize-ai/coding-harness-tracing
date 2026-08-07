"""Tests for tracing.devin.install — SessionEnd hook registration in Devin's config."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# The hook command string install.py should write into config.json.
FAKE_VENV_BIN = Path("/fake/venv/bin/arize-hook-devin")
HOOK_CMD = str(FAKE_VENV_BIN)


@pytest.fixture(autouse=True)
def _mock_venv_bin(monkeypatch):
    """Make venv_bin() always return our fake path."""
    monkeypatch.setattr("tracing.devin.install.venv_bin", lambda name: FAKE_VENV_BIN)


@pytest.fixture(autouse=True)
def _no_dry_run(monkeypatch):
    """Default: dry-run is off."""
    monkeypatch.delenv("ARIZE_DRY_RUN", raising=False)


@pytest.fixture()
def config_file(tmp_path, monkeypatch):
    """Point CONFIG_FILE at a temp path (not yet created)."""
    path = tmp_path / "config" / "devin" / "config.json"
    monkeypatch.setattr("tracing.devin.install.CONFIG_FILE", path)
    return path


def _event_commands(config: dict, event: str) -> list:
    """Collect all hook commands registered under hooks.<event>."""
    cmds = []
    for matcher in config.get("hooks", {}).get(event, []):
        for h in matcher.get("hooks", []):
            cmds.append(h.get("command"))
    return cmds


def _session_end_commands(config: dict) -> list:
    """Collect all hook commands registered under hooks.SessionEnd."""
    return _event_commands(config, "SessionEnd")


class TestRegistersAllEvents:
    def test_registers_stop_and_session_end(self, config_file):
        from tracing.devin.install import HOOK_EVENT_NAMES, _register_hooks

        _register_hooks()
        data = json.loads(config_file.read_text())
        assert set(HOOK_EVENT_NAMES) == {"Stop", "SessionEnd"}
        for event in HOOK_EVENT_NAMES:
            assert _event_commands(data, event) == [HOOK_CMD]

    def test_unregister_removes_all_events(self, config_file):
        from tracing.devin.install import _register_hooks, _unregister_hooks

        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(json.dumps({"version": 1}))
        _register_hooks()
        _unregister_hooks()

        data = json.loads(config_file.read_text())
        assert "hooks" not in data
        assert data["version"] == 1


class TestRegisterHooks:
    def test_preserves_unrelated_keys(self, config_file):
        from tracing.devin.install import _register_hooks

        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(json.dumps({"version": 1, "theme_mode": "dark"}))

        _register_hooks()

        data = json.loads(config_file.read_text())
        assert data["version"] == 1
        assert data["theme_mode"] == "dark"
        assert _session_end_commands(data) == [HOOK_CMD]

    def test_hook_entry_shape(self, config_file):
        from tracing.devin.install import HOOK_TIMEOUT, _register_hooks

        _register_hooks()

        data = json.loads(config_file.read_text())
        matcher = data["hooks"]["SessionEnd"][0]
        assert matcher["hooks"][0] == {
            "type": "command",
            "command": HOOK_CMD,
            "timeout": HOOK_TIMEOUT,
        }

    def test_idempotent(self, config_file):
        from tracing.devin.install import _register_hooks

        _register_hooks()
        _register_hooks()

        data = json.loads(config_file.read_text())
        assert _session_end_commands(data) == [HOOK_CMD]

    def test_creates_missing_config(self, config_file):
        from tracing.devin.install import _register_hooks

        assert not config_file.exists()
        _register_hooks()

        assert config_file.exists()
        data = json.loads(config_file.read_text())
        # Only the hooks block — nothing else invented.
        assert list(data.keys()) == ["hooks"]
        assert _session_end_commands(data) == [HOOK_CMD]

    def test_aborts_on_malformed_config_without_overwriting(self, config_file):
        from tracing.devin.install import _register_hooks

        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text('{"model": "swe-1.6", not valid json!!!')
        original = config_file.read_text()

        with pytest.raises(SystemExit) as excinfo:
            _register_hooks()

        assert excinfo.value.code == 1
        assert config_file.read_text() == original

    def test_aborts_when_config_root_is_not_an_object(self, config_file):
        from tracing.devin.install import _register_hooks

        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text('["not", "an", "object"]')
        original = config_file.read_text()

        with pytest.raises(SystemExit):
            _register_hooks()

        assert config_file.read_text() == original

    def test_empty_config_is_treated_as_no_settings(self, config_file):
        from tracing.devin.install import _register_hooks

        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text("   \n")

        _register_hooks()

        data = json.loads(config_file.read_text())
        assert _session_end_commands(data) == [HOOK_CMD]

    def test_dry_run_no_write(self, config_file, monkeypatch):
        from tracing.devin.install import _register_hooks

        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        _register_hooks()

        assert not config_file.exists()

    def test_preserves_other_events(self, config_file):
        from tracing.devin.install import _register_hooks

        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [{"hooks": [{"type": "command", "command": "/usr/bin/other"}]}],
                    }
                }
            )
        )

        _register_hooks()

        data = json.loads(config_file.read_text())
        assert data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "/usr/bin/other"
        assert _session_end_commands(data) == [HOOK_CMD]

    def test_appends_alongside_foreign_command_in_same_event(self, config_file):
        """A pre-existing foreign SessionEnd command is preserved; ours is appended."""
        from tracing.devin.install import _register_hooks

        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(
            json.dumps(
                {
                    "hooks": {
                        "SessionEnd": [{"hooks": [{"type": "command", "command": "/usr/bin/other"}]}],
                    }
                }
            )
        )

        _register_hooks()

        data = json.loads(config_file.read_text())
        cmds = _session_end_commands(data)
        assert "/usr/bin/other" in cmds
        assert HOOK_CMD in cmds
        assert cmds.count(HOOK_CMD) == 1

    def test_idempotent_with_foreign_command_present(self, config_file):
        """Re-running with a foreign command present still adds ours exactly once."""
        from tracing.devin.install import _register_hooks

        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(
            json.dumps(
                {
                    "hooks": {
                        "SessionEnd": [{"hooks": [{"type": "command", "command": "/usr/bin/other"}]}],
                    }
                }
            )
        )

        _register_hooks()
        _register_hooks()

        data = json.loads(config_file.read_text())
        cmds = _session_end_commands(data)
        assert cmds.count(HOOK_CMD) == 1
        assert cmds.count("/usr/bin/other") == 1


class TestCommentedConfig:
    """Devin accepts comments in config.json; stdlib json does not.

    Comments are stripped to parse, so settings survive a rewrite even though
    the comments themselves do not. A config we do not need to change is never
    rewritten, so comments there stay intact.
    """

    def test_line_comments_do_not_drop_settings(self, config_file):
        from tracing.devin.install import _register_hooks

        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(
            """{
  // pick the fast model
  "model": "swe-1.6",
  "theme_mode": "dark" // inline trailing comment
}
"""
        )

        _register_hooks()

        data = json.loads(config_file.read_text())
        assert data["model"] == "swe-1.6"
        assert data["theme_mode"] == "dark"
        assert _session_end_commands(data) == [HOOK_CMD]

    def test_block_comments_do_not_drop_settings(self, config_file):
        from tracing.devin.install import _register_hooks

        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(
            """{
  /* multi
     line
     comment */
  "model": "swe-1.6",
  "hooks": {
    /* existing hooks */
    "PreToolUse": [{"hooks": [{"type": "command", "command": "/usr/bin/other"}]}]
  }
}
"""
        )

        _register_hooks()

        data = json.loads(config_file.read_text())
        assert data["model"] == "swe-1.6"
        assert data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "/usr/bin/other"
        assert _session_end_commands(data) == [HOOK_CMD]

    def test_comment_markers_inside_strings_survive(self, config_file):
        from tracing.devin.install import _register_hooks

        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(
            json.dumps(
                {
                    "docs_url": "https://docs.devin.ai/cli",
                    "glob": "src/**/*.ts /* not a comment */",
                    "escaped": 'quote \\" then // not a comment',
                }
            )
        )

        _register_hooks()

        data = json.loads(config_file.read_text())
        assert data["docs_url"] == "https://docs.devin.ai/cli"
        assert data["glob"] == "src/**/*.ts /* not a comment */"
        assert data["escaped"] == 'quote \\" then // not a comment'

    def test_already_registered_commented_config_left_byte_identical(self, config_file):
        from tracing.devin.install import _register_hooks

        config_file.parent.mkdir(parents=True, exist_ok=True)
        commented = """{
  // arize tracing
  "hooks": {
    "Stop": [{"hooks": [{"type": "command", "command": "%(cmd)s", "timeout": 30}]}],
    "SessionEnd": [{"hooks": [{"type": "command", "command": "%(cmd)s", "timeout": 30}]}]
  }
}
""" % {
            "cmd": HOOK_CMD
        }
        config_file.write_text(commented)

        _register_hooks()

        assert config_file.read_text() == commented

    def test_unregister_from_commented_config(self, config_file):
        from tracing.devin.install import _unregister_hooks

        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(
            """{
  // keep me
  "model": "swe-1.6",
  "hooks": {
    "SessionEnd": [{"hooks": [{"type": "command", "command": "%(cmd)s"}]}]
  }
}
"""
            % {"cmd": HOOK_CMD}
        )

        _unregister_hooks()

        data = json.loads(config_file.read_text())
        assert data["model"] == "swe-1.6"
        assert "hooks" not in data


class TestStripJsonComments:
    def test_replaces_comments_with_whitespace_preserving_lines(self):
        from tracing.devin.install import _strip_json_comments

        text = '{\n  // a\n  "k": 1 /* b\nc */\n}'
        stripped = _strip_json_comments(text)

        assert json.loads(stripped) == {"k": 1}
        assert stripped.count("\n") == text.count("\n")
        assert len(stripped) == len(text)

    def test_unterminated_block_comment_is_dropped(self):
        from tracing.devin.install import _strip_json_comments

        stripped = _strip_json_comments('{"k": 1}\n/* unterminated')

        assert json.loads(stripped) == {"k": 1}


class TestUnregisterHooks:
    def test_removes_command_and_drops_empty_hooks(self, config_file):
        from tracing.devin.install import _register_hooks, _unregister_hooks

        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(json.dumps({"version": 1, "theme_mode": "dark"}))

        _register_hooks()
        _unregister_hooks()

        data = json.loads(config_file.read_text())
        # Our command gone; empty hooks key dropped; unrelated keys intact.
        assert "hooks" not in data
        assert data["version"] == 1
        assert data["theme_mode"] == "dark"

    def test_preserves_other_commands_in_event(self, config_file):
        from tracing.devin.install import _register_hooks, _unregister_hooks

        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(
            json.dumps(
                {
                    "hooks": {
                        "SessionEnd": [{"hooks": [{"type": "command", "command": "/usr/bin/other"}]}],
                    }
                }
            )
        )

        _register_hooks()
        _unregister_hooks()

        data = json.loads(config_file.read_text())
        assert _session_end_commands(data) == ["/usr/bin/other"]

    def test_preserves_other_events(self, config_file):
        from tracing.devin.install import _register_hooks, _unregister_hooks

        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [{"hooks": [{"type": "command", "command": "/usr/bin/other"}]}],
                    }
                }
            )
        )

        _register_hooks()
        _unregister_hooks()

        data = json.loads(config_file.read_text())
        assert "SessionEnd" not in data["hooks"]
        assert data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "/usr/bin/other"

    def test_no_op_when_config_missing(self, config_file):
        from tracing.devin.install import _unregister_hooks

        # Should not raise or create the file.
        _unregister_hooks()
        assert not config_file.exists()

    def test_no_op_on_malformed_config(self, config_file):
        from tracing.devin.install import _unregister_hooks

        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text("{not valid json!!!")
        original = config_file.read_text()

        _unregister_hooks()

        assert config_file.read_text() == original

    def test_dry_run_no_write(self, config_file, monkeypatch):
        from tracing.devin.install import _register_hooks, _unregister_hooks

        _register_hooks()
        before = config_file.read_text()

        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        _unregister_hooks()

        assert config_file.read_text() == before


class TestUninstallEntryPoint:
    def test_uninstall_cleans_config_and_calls_harness_cleanup(self, config_file, monkeypatch):
        """Public uninstall(): strips our hook and runs the harness-entry cleanup chain."""
        from tracing.devin import install as install_mod
        from tracing.devin.install import _register_hooks, uninstall

        calls = {}
        monkeypatch.setattr(install_mod, "remove_harness_entry", lambda name: calls.__setitem__("remove", name))
        monkeypatch.setattr(install_mod, "unlink_skills", lambda name: calls.__setitem__("unlink", name))

        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(json.dumps({"version": 1}))
        _register_hooks()

        uninstall()

        data = json.loads(config_file.read_text())
        assert "hooks" not in data
        assert data["version"] == 1
        assert calls == {"remove": "devin", "unlink": "devin"}
