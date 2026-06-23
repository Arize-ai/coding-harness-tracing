"""Tests for kiro support in install.sh / install.bat router scripts.

These tests verify that the shell and batch installer dispatchers
recognise 'kiro' as a valid harness and map it to tracing/kiro.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"
INSTALL_BAT = REPO_ROOT / "install.bat"


# ---------------------------------------------------------------------------
# install.sh — harness_dir() mapping
# ---------------------------------------------------------------------------


def test_install_sh_harness_dir_has_kiro():
    """harness_dir() case statement must map kiro -> tracing/kiro."""
    text = INSTALL_SH.read_text()
    assert "tracing/kiro" in text, "install.sh harness_dir() must contain a 'kiro) echo \"tracing/kiro\"' clause"


def test_install_sh_harness_dir_kiro_after_gemini():
    """kiro clause must appear after gemini and before the *) default in harness_dir()."""
    text = INSTALL_SH.read_text()
    gemini_pos = text.find('gemini)  echo "tracing/gemini"')
    kiro_pos = text.find("kiro)")
    default_pos = text.find("*)       return 1")
    assert gemini_pos != -1, "gemini clause not found in harness_dir()"
    assert kiro_pos != -1, "kiro clause not found in harness_dir()"
    assert default_pos != -1, "*) default clause not found in harness_dir()"
    assert gemini_pos < kiro_pos < default_pos, "kiro clause must be between gemini and *) default in harness_dir()"


# ---------------------------------------------------------------------------
# install.sh — main() case dispatch
# ---------------------------------------------------------------------------


def test_install_sh_main_dispatch_includes_kiro():
    """The first case in main() must include kiro in the harness list."""
    text = INSTALL_SH.read_text()
    # The dispatch line looks like: claude|codex|copilot|cursor|gemini|kiro)
    assert "gemini|kiro" in text, "main() case dispatch must include 'kiro' after 'gemini'"


# ---------------------------------------------------------------------------
# install.sh — usage() help text
# ---------------------------------------------------------------------------


def test_install_sh_usage_mentions_kiro():
    """usage() heredoc must list kiro as a supported command."""
    text = INSTALL_SH.read_text()
    # Look for the kiro line in the usage block
    assert (
        "kiro" in text.split("Commands:")[1].split("update")[0] if "Commands:" in text else False
    ), "usage() must list kiro between gemini and update"


@pytest.mark.skipif(os.name == "nt", reason="bash not available on Windows")
def test_install_sh_help_output_shows_kiro():
    """Running install.sh --help must mention kiro."""
    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert "kiro" in result.stdout, "--help output must mention kiro"


def test_install_sh_usage_kiro_description():
    """The kiro usage line must describe it as Kiro CLI tracing."""
    text = INSTALL_SH.read_text()
    # Match the pattern from the task spec
    lines = text.splitlines()
    kiro_lines = [line for line in lines if "kiro" in line.lower() and "Kiro CLI" in line]
    assert kiro_lines, "usage() must contain a line describing kiro as 'Install and configure tracing for Kiro CLI'"


# ---------------------------------------------------------------------------
# install.bat — parse_args harness loop
# ---------------------------------------------------------------------------


def test_install_bat_parse_args_includes_kiro():
    """parse_args harness loop must include kiro."""
    text = INSTALL_BAT.read_text()
    # The line looks like: for %%C in (claude codex copilot cursor gemini kiro) do ...
    # Find lines with 'for %%C in' that list harnesses
    lines = text.splitlines()
    parse_lines = [line for line in lines if "for %%C in" in line and "claude" in line and "COMMAND" in line]
    assert parse_lines, "parse_args harness loop not found"
    assert "kiro" in parse_lines[0], "parse_args harness loop must include kiro"


# ---------------------------------------------------------------------------
# install.bat — uninstall dispatch loop
# ---------------------------------------------------------------------------


def test_install_bat_uninstall_loop_includes_kiro():
    """Uninstall harness loop must include kiro."""
    text = INSTALL_BAT.read_text()
    lines = text.splitlines()
    # Find the uninstall dispatch for %%C line
    uninstall_lines = [line for line in lines if "for %%C in" in line and "claude" in line and "UNINSTALL" in line]
    assert uninstall_lines, "uninstall harness loop not found"
    assert "kiro" in uninstall_lines[0], "uninstall harness loop must include kiro"


# ---------------------------------------------------------------------------
# install.bat — REM mapping comment
# ---------------------------------------------------------------------------


def test_install_bat_mapping_comment_includes_kiro():
    """The REM harness mapping comment must include kiro->tracing\\kiro."""
    text = INSTALL_BAT.read_text()
    lines = text.splitlines()
    mapping_lines = [line for line in lines if line.startswith("REM") and "claude->tracing" in line]
    assert mapping_lines, "REM harness mapping comment not found"
    assert "kiro->tracing\\kiro" in mapping_lines[0], "REM mapping comment must include kiro->tracing\\kiro"


# ---------------------------------------------------------------------------
# install.bat — :resolve_dir
# ---------------------------------------------------------------------------


def test_install_bat_resolve_dir_has_kiro():
    """resolve_dir must map kiro to tracing\\kiro."""
    text = INSTALL_BAT.read_text()
    assert "tracing\\kiro" in text, "install.bat :resolve_dir must contain a kiro -> tracing\\kiro mapping"


def test_install_bat_resolve_dir_kiro_after_gemini():
    """kiro mapping must appear after gemini in :resolve_dir."""
    text = INSTALL_BAT.read_text()
    # We look for the gemini line in resolve_dir specifically
    gemini_in_resolve = text.find('=="gemini"', text.find(":resolve_dir"))
    kiro_in_resolve = text.find('=="kiro"', text.find(":resolve_dir"))
    assert gemini_in_resolve != -1, "gemini not found in :resolve_dir"
    assert kiro_in_resolve != -1, "kiro not found in :resolve_dir"
    assert gemini_in_resolve < kiro_in_resolve, "kiro must appear after gemini in :resolve_dir"


def test_install_bat_resolve_dir_kiro_before_empty_check():
    """kiro mapping must appear before the HARNESS_DIR empty check."""
    text = INSTALL_BAT.read_text()
    resolve_start = text.find(":resolve_dir")
    assert resolve_start != -1
    remaining = text[resolve_start:]
    kiro_pos = remaining.find('"kiro"')
    empty_check = remaining.find('HARNESS_DIR%"==""')
    assert kiro_pos != -1, "kiro not found in :resolve_dir"
    assert empty_check != -1, "HARNESS_DIR empty check not found in :resolve_dir"
    assert kiro_pos < empty_check, "kiro mapping must appear before the HARNESS_DIR empty check"


# ---------------------------------------------------------------------------
# install.bat — usage block
# ---------------------------------------------------------------------------


def test_install_bat_usage_mentions_kiro():
    """Usage block must list kiro."""
    text = INSTALL_BAT.read_text()
    usage_start = text.find(":usage")
    assert usage_start != -1
    usage_text = text[usage_start:]
    assert "kiro" in usage_text, "usage block must mention kiro"


def test_install_bat_usage_kiro_description():
    """The kiro usage line must describe it as Kiro CLI tracing."""
    text = INSTALL_BAT.read_text()
    usage_start = text.find(":usage")
    assert usage_start != -1
    usage_text = text[usage_start:]
    lines = usage_text.splitlines()
    kiro_lines = [line for line in lines if "kiro" in line.lower() and "Kiro CLI" in line]
    assert kiro_lines, "usage block must have a line describing kiro as 'Install tracing for Kiro CLI'"


def test_install_bat_usage_kiro_after_gemini():
    """kiro must appear after gemini in the usage block."""
    text = INSTALL_BAT.read_text()
    usage_start = text.find(":usage")
    assert usage_start != -1
    usage_text = text[usage_start:]
    gemini_pos = usage_text.find("gemini")
    kiro_pos = usage_text.find("kiro")
    update_pos = usage_text.find("update")
    assert kiro_pos != -1, "kiro not in usage block"
    assert gemini_pos < kiro_pos, "kiro must come after gemini in usage"
    assert kiro_pos < update_pos, "kiro must come before update in usage"


# ---------------------------------------------------------------------------
# Consistency: both scripts agree on the harness set
# ---------------------------------------------------------------------------


def test_both_scripts_list_same_harnesses():
    """install.sh and install.bat must support the same set of harnesses including kiro."""
    expected = {"claude", "codex", "copilot", "cursor", "gemini", "kiro"}
    sh_text = INSTALL_SH.read_text()
    bat_text = INSTALL_BAT.read_text()
    for harness in expected:
        assert harness in sh_text, f"install.sh missing harness: {harness}"
        assert harness.lower() in bat_text.lower(), f"install.bat missing harness: {harness}"


def test_both_scripts_map_kiro_to_tracing_kiro():
    """Both scripts must map kiro to the tracing/kiro directory."""
    sh_text = INSTALL_SH.read_text()
    bat_text = INSTALL_BAT.read_text()
    assert "tracing/kiro" in sh_text, "install.sh must map kiro -> tracing/kiro"
    assert "tracing\\kiro" in bat_text, "install.bat must map kiro -> tracing\\kiro"
