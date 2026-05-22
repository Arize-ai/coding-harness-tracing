"""Tests for the manifest generator. Ensures stable output and drift detection."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_manifest_file_exists():
    assert (REPO_ROOT / "core" / "manifest.json").is_file()


def test_manifest_is_valid_json():
    content = (REPO_ROOT / "core" / "manifest.json").read_text()
    data = json.loads(content)
    assert data["schema_version"] == 1
    assert "harnesses" in data
    assert "shared" in data


def test_manifest_has_all_harnesses():
    data = json.loads((REPO_ROOT / "core" / "manifest.json").read_text())
    for name in ["claude_code", "codex", "copilot", "cursor", "gemini", "kiro"]:
        assert name in data["harnesses"], f"missing harness {name}"
        entry = data["harnesses"][name]
        assert "display_name" in entry
        assert "harness_bin" in entry
        assert "settings_file" in entry


def test_manifest_is_up_to_date():
    """Running --check on the committed manifest must pass."""
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "gen_manifest.py"), "--check"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"manifest is stale: {result.stderr}")


def test_manifest_output_is_stable():
    """Running the generator twice must produce identical bytes."""
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "gen_manifest.py")],
        capture_output=True,
        text=True,
        check=True,
    )
    content1 = (REPO_ROOT / "core" / "manifest.json").read_text()
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "gen_manifest.py")],
        capture_output=True,
        text=True,
        check=True,
    )
    content2 = (REPO_ROOT / "core" / "manifest.json").read_text()
    assert content1 == content2, "generator output is not deterministic"
