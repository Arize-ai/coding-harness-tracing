#!/usr/bin/env python3
"""Tests for hook registration updates (task-13).

Validates that:
- plugin.json is valid JSON with correct CLI entry points
- No bash/jq/curl references remain in harness docs
- All CLI commands referenced in docs exist in pyproject.toml
- Documentation consistency across all three harnesses
"""

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent

# --- Fixtures ---

HARNESS_DIRS = ["tracing/claude_code", "tracing/codex", "tracing/cursor"]

EXPECTED_ENTRY_POINTS = {
    "arize-config": "core.config:main",
    "arize-hook-session-start": "tracing.claude_code.hooks.handlers:session_start",
    "arize-hook-pre-tool-use": "tracing.claude_code.hooks.handlers:pre_tool_use",
    "arize-hook-post-tool-use": "tracing.claude_code.hooks.handlers:post_tool_use",
    "arize-hook-user-prompt-submit": "tracing.claude_code.hooks.handlers:user_prompt_submit",
    "arize-hook-stop": "tracing.claude_code.hooks.handlers:stop",
    "arize-hook-subagent-stop": "tracing.claude_code.hooks.handlers:subagent_stop",
    "arize-hook-stop-failure": "tracing.claude_code.hooks.handlers:stop_failure",
    "arize-hook-notification": "tracing.claude_code.hooks.handlers:notification",
    "arize-hook-permission-request": "tracing.claude_code.hooks.handlers:permission_request",
    "arize-hook-session-end": "tracing.claude_code.hooks.handlers:session_end",
    "arize-hook-codex-notify": "tracing.codex.hooks.handlers:notify",
    "arize-hook-cursor": "tracing.cursor.hooks.handlers:main",
}


def _parse_pyproject_scripts():
    """Parse [project.scripts] from pyproject.toml."""
    content = (REPO_ROOT / "pyproject.toml").read_text()
    scripts = {}
    in_scripts = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped == "[project.scripts]":
            in_scripts = True
            continue
        if in_scripts:
            if stripped.startswith("[") and stripped.endswith("]"):
                break
            if stripped.startswith("#") or not stripped:
                continue
            key, _, value = stripped.partition("=")
            key = key.strip()
            value = value.strip().strip('"')
            if key and value:
                scripts[key] = value
    return scripts


def _collect_md_files():
    """Collect all .md files in harness directories."""
    files = []
    for d in HARNESS_DIRS:
        harness_dir = REPO_ROOT / d
        if harness_dir.exists():
            files.extend(harness_dir.rglob("*.md"))
    return files


def _collect_json_files():
    """Collect all .json files in harness directories."""
    files = []
    for d in HARNESS_DIRS:
        harness_dir = REPO_ROOT / d
        if harness_dir.exists():
            files.extend(harness_dir.rglob("*.json"))
    return files


# --- plugin.json tests ---


class TestPluginJson:
    """Tests for tracing.claude_code/.claude-plugin/plugin.json."""

    @pytest.fixture
    def plugin_data(self):
        path = REPO_ROOT / "tracing" / "claude_code" / ".claude-plugin" / "plugin.json"
        with open(path) as f:
            return json.load(f)

    def test_valid_json(self, plugin_data):
        """plugin.json must be valid JSON (implicitly tested by fixture loading)."""
        assert isinstance(plugin_data, dict)

    def test_has_required_metadata(self, plugin_data):
        """plugin.json must have name, description, version."""
        assert "name" in plugin_data
        assert "description" in plugin_data
        assert "version" in plugin_data


class TestHooksJson:
    """Tests for tracing.claude_code/hooks/hooks.json."""

    @pytest.fixture
    def hooks_data(self):
        path = REPO_ROOT / "tracing" / "claude_code" / "hooks" / "hooks.json"
        with open(path) as f:
            return json.load(f)

    def test_valid_json(self, hooks_data):
        """hooks.json must be valid JSON."""
        assert isinstance(hooks_data, dict)
        assert "hooks" in hooks_data

    def test_all_claude_events_registered(self, hooks_data):
        """All 10 Claude hook events must be present."""
        expected_events = {
            "SessionStart",
            "UserPromptSubmit",
            "PreToolUse",
            "PostToolUse",
            "Stop",
            "SubagentStop",
            "StopFailure",
            "Notification",
            "PermissionRequest",
            "SessionEnd",
        }
        actual_events = set(hooks_data["hooks"].keys())
        assert expected_events == actual_events

    def test_hook_commands_use_run_hook(self, hooks_data):
        """Each hook command must use the run-hook dispatcher."""
        for event, hook_list in hooks_data["hooks"].items():
            for hook_group in hook_list:
                for hook in hook_group["hooks"]:
                    cmd = hook["command"]
                    assert "run-hook" in cmd, f"{event}: command should use run-hook dispatcher: {cmd}"
                    assert "CLAUDE_PLUGIN_ROOT" in cmd, f"{event}: command should reference CLAUDE_PLUGIN_ROOT: {cmd}"

    def test_hook_commands_reference_entry_points(self, hooks_data):
        """Each hook command must pass an arize-hook-* entry point name."""
        for event, hook_list in hooks_data["hooks"].items():
            for hook_group in hook_list:
                for hook in hook_group["hooks"]:
                    cmd = hook["command"]
                    assert "arize-hook-" in cmd, f"{event}: command should reference arize-hook- entry point: {cmd}"

    def test_hook_entry_points_exist_in_pyproject(self, hooks_data):
        """Every entry point referenced in hooks must exist in pyproject.toml."""
        scripts = _parse_pyproject_scripts()
        for event, hook_list in hooks_data["hooks"].items():
            for hook_group in hook_list:
                for hook in hook_group["hooks"]:
                    cmd = hook["command"]
                    # Extract the entry point name (last argument)
                    entry_point = cmd.strip().split()[-1].strip('"')
                    assert entry_point in scripts, f"{event}: entry point '{entry_point}' not found in pyproject.toml"

    def test_event_to_entry_point_mapping(self, hooks_data):
        """Verify specific event-to-entry-point mappings."""
        mapping = {}
        for event, hook_list in hooks_data["hooks"].items():
            cmd = hook_list[0]["hooks"][0]["command"]
            entry_point = cmd.strip().split()[-1].strip('"')
            mapping[event] = entry_point

        assert mapping["SessionStart"] == "arize-hook-session-start"
        assert mapping["UserPromptSubmit"] == "arize-hook-user-prompt-submit"
        assert mapping["PreToolUse"] == "arize-hook-pre-tool-use"
        assert mapping["PostToolUse"] == "arize-hook-post-tool-use"
        assert mapping["Stop"] == "arize-hook-stop"
        assert mapping["SubagentStop"] == "arize-hook-subagent-stop"
        assert mapping["StopFailure"] == "arize-hook-stop-failure"
        assert mapping["Notification"] == "arize-hook-notification"
        assert mapping["PermissionRequest"] == "arize-hook-permission-request"
        assert mapping["SessionEnd"] == "arize-hook-session-end"

    def test_hook_type_is_command(self, hooks_data):
        """All hooks must have type 'command'."""
        for event, hook_list in hooks_data["hooks"].items():
            for hook_group in hook_list:
                for hook in hook_group["hooks"]:
                    assert hook["type"] == "command", f"{event}: hook type should be 'command', got '{hook['type']}'"

    def test_no_hardcoded_paths(self, hooks_data):
        """Hook commands must not use hardcoded venv or home directory paths."""
        for event, hook_list in hooks_data["hooks"].items():
            for hook_group in hook_list:
                for hook in hook_group["hooks"]:
                    cmd = hook["command"]
                    assert "~/.arize" not in cmd, f"{event}: hardcoded ~/.arize path: {cmd}"
                    assert "/home/" not in cmd, f"{event}: hardcoded /home/ path: {cmd}"


class TestRunHookScript:
    """Tests for tracing.claude_code/scripts/run-hook."""

    def test_run_hook_exists(self):
        path = REPO_ROOT / "tracing" / "claude_code" / "scripts" / "run-hook"
        assert path.is_file(), "scripts/run-hook must exist"

    def test_run_hook_is_executable(self):
        import os

        path = REPO_ROOT / "tracing" / "claude_code" / "scripts" / "run-hook"
        assert os.access(path, os.X_OK), "scripts/run-hook must be executable"

    def test_run_hook_has_sh_shebang(self):
        path = REPO_ROOT / "tracing" / "claude_code" / "scripts" / "run-hook"
        first_line = path.read_text().splitlines()[0]
        assert first_line.startswith("#!/bin/sh"), f"Expected sh shebang, got: {first_line}"

    def test_run_hook_references_plugin_vars(self):
        """run-hook must use CLAUDE_PLUGIN_ROOT and CLAUDE_PLUGIN_DATA."""
        text = (REPO_ROOT / "tracing" / "claude_code" / "scripts" / "run-hook").read_text()
        assert "CLAUDE_PLUGIN_ROOT" in text
        assert "CLAUDE_PLUGIN_DATA" in text


# --- No bash/jq/curl references in docs ---


class TestNoBashReferences:
    """Verify no stale bash/jq references in harness JSON and Markdown files."""

    def test_no_bash_in_json_files(self):
        """No 'bash ' in .json files under harness directories."""
        for f in _collect_json_files():
            content = f.read_text()
            assert "bash " not in content, f"{f.relative_to(REPO_ROOT)}: still contains 'bash ' reference"

    def test_no_sh_scripts_in_json_files(self):
        """No '.sh' script references in .json files under harness directories."""
        for f in _collect_json_files():
            content = f.read_text()
            # Check for .sh in command context (not in general text)
            for line in content.splitlines():
                if ".sh" in line and ("command" in line or "hook" in line):
                    assert False, f"{f.relative_to(REPO_ROOT)}: still references .sh script: {line.strip()}"

    def test_no_bash_hook_references_in_md(self):
        """No 'bash .../hooks/' patterns in .md files."""
        pattern = re.compile(r"bash\s+.*?/hooks/")
        for f in _collect_md_files():
            content = f.read_text()
            matches = pattern.findall(content)
            assert not matches, f"{f.relative_to(REPO_ROOT)}: still references bash hooks: {matches}"

    def test_no_jq_in_docs(self):
        """No 'jq ' references in harness .md files."""
        for f in _collect_md_files():
            content = f.read_text()
            assert "jq " not in content, f"{f.relative_to(REPO_ROOT)}: still references jq"

    def test_no_hook_sh_in_md(self):
        """No references to hook .sh scripts (like notify.sh, hook-handler.sh) in .md files."""
        # Match specific hook script filenames
        hook_scripts = [
            "session_start.sh",
            "session_end.sh",
            "stop.sh",
            "subagent_stop.sh",
            "notification.sh",
            "permission_request.sh",
            "pre_tool_use.sh",
            "post_tool_use.sh",
            "user_prompt_submit.sh",
            "notify.sh",
            "hook-handler.sh",
            "common.sh",
        ]
        for f in _collect_md_files():
            content = f.read_text()
            for script in hook_scripts:
                assert script not in content, f"{f.relative_to(REPO_ROOT)}: still references {script}"

    def test_no_source_collector_ctl_sh(self):
        """No 'collector_ctl.sh' references in markdown or shell files."""
        pattern = re.compile(r"collector_ctl\.sh")
        for f in _collect_md_files():
            content = f.read_text()
            matches = pattern.findall(content)
            assert not matches, f"{f.relative_to(REPO_ROOT)}: still references collector_ctl.sh: {matches}"

    def test_user_facing_docs_reference_install_sh(self):
        """User-facing docs should tell users to run install.sh, not install.py directly.

        Per-harness install.py files now exist (the router dispatches to them),
        so we only check that user-facing READMEs point to install.sh as the
        entry point.
        """
        user_facing = [
            REPO_ROOT / "README.md",
            *(REPO_ROOT.glob("*-tracing/README.md")),
        ]
        for f in user_facing:
            if not f.exists():
                continue
            content = f.read_text()
            if "install.py" in content:
                # install.py may appear in technical context; just ensure
                # install.sh is ALSO referenced as the user-facing command
                assert "install.sh" in content, (
                    f"{f.relative_to(REPO_ROOT)}: references install.py "
                    "but not install.sh — users should be told to run install.sh"
                )


# --- CLI entry points consistency ---


class TestEntryPointConsistency:
    """Verify all referenced CLI commands exist in pyproject.toml."""

    def test_pyproject_has_all_expected_entry_points(self):
        """pyproject.toml must define all expected entry points."""
        scripts = _parse_pyproject_scripts()
        for name, module in EXPECTED_ENTRY_POINTS.items():
            assert name in scripts, f"Missing entry point: {name}"
            assert scripts[name] == module, f"Entry point {name}: expected {module}, got {scripts[name]}"


# --- Documentation consistency ---


class TestDocumentationConsistency:
    """Verify documentation is internally consistent."""

    def test_cursor_skill_references_cli_entry_points(self):
        """Cursor SKILL.md should use CLI entry points for hooks."""
        skill = (REPO_ROOT / "tracing" / "cursor" / "skills" / "manage-cursor-tracing" / "SKILL.md").read_text()
        assert "arize-hook-cursor" in skill
        assert "send_span()" in skill
        assert "hook-handler.sh" not in skill

    def test_codex_skill_references_cli_entry_points(self):
        """Codex SKILL.md should use CLI entry points."""
        skill = (REPO_ROOT / "tracing" / "codex" / "skills" / "manage-codex-tracing" / "SKILL.md").read_text()
        assert "arize-hook-codex-notify" in skill
        assert "notify.sh" not in skill

    def test_claude_skill_references_cli_entry_points(self):
        """Claude SKILL.md should use CLI entry points."""
        skill = (
            REPO_ROOT / "tracing" / "claude_code" / "skills" / "manage-claude-code-tracing" / "SKILL.md"
        ).read_text()
        assert "send_span()" in skill
        assert "collector_ctl.sh" not in skill


# --- Codex config.toml pattern ---


class TestCodexHookReference:
    """Verify Codex docs reference the correct notify hook command."""

    def test_codex_skill_notify_command(self):
        """Codex SKILL.md notify hook should use the CLI entry point."""
        skill = (REPO_ROOT / "tracing" / "codex" / "skills" / "manage-codex-tracing" / "SKILL.md").read_text()
        assert "arize-hook-codex-notify" in skill


# --- Cursor hooks.json pattern ---


class TestCursorHookReference:
    """Verify Cursor docs reference the correct handler command."""

    def test_cursor_skill_hook_command(self):
        """Cursor SKILL.md should show arize-hook-cursor for all 12 events."""
        skill = (REPO_ROOT / "tracing" / "cursor" / "skills" / "manage-cursor-tracing" / "SKILL.md").read_text()
        # All events should reference the same entry point
        events = [
            "beforeSubmitPrompt",
            "afterAgentResponse",
            "afterAgentThought",
            "beforeShellExecution",
            "afterShellExecution",
            "beforeMCPExecution",
            "afterMCPExecution",
            "beforeReadFile",
            "afterFileEdit",
            "stop",
            "beforeTabFileRead",
            "afterTabFileEdit",
        ]
        for event in events:
            assert event in skill, f"Cursor SKILL.md missing event: {event}"
        # Count occurrences of arize-hook-cursor — should be at least 12 (one per event)
        count = skill.count("arize-hook-cursor")
        assert count >= 12, f"Expected at least 12 arize-hook-cursor references, got {count}"


# --- State file extension ---


class TestStateFileExtension:
    """Verify docs reference .json state files, not .yaml."""

    def test_state_files_use_json_extension(self):
        """All state file references in docs should use .json, not .yaml."""
        for f in _collect_md_files():
            content = f.read_text()
            if "state_*.yaml" in content:
                assert False, f"{f.relative_to(REPO_ROOT)}: still references state_*.yaml (should be state_*.json)"
