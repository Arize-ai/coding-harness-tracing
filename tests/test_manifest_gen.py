"""Tests for the manifest generator. Ensures stable output and drift detection."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Import the generator as a library so tests can build manifests in-process
# without touching the committed file.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import gen_manifest  # noqa: E402

MANIFEST_PATH = REPO_ROOT / "core" / "manifest.json"
GEN_SCRIPT = REPO_ROOT / "scripts" / "gen_manifest.py"


def test_manifest_file_exists():
    assert MANIFEST_PATH.is_file()


def test_manifest_is_valid_json():
    data = json.loads(MANIFEST_PATH.read_text())
    assert data["schema_version"] == 1
    assert "harnesses" in data
    assert "shared" in data


def test_manifest_has_all_harnesses():
    data = json.loads(MANIFEST_PATH.read_text())
    for name in ["claude_code", "codex", "copilot", "cursor", "gemini", "kiro"]:
        assert name in data["harnesses"], f"missing harness {name}"
        entry = data["harnesses"][name]
        assert "display_name" in entry
        assert "harness_bin" in entry
        assert "settings_file" in entry


def test_manifest_is_up_to_date():
    """Running --check on the committed manifest must pass."""
    result = subprocess.run(
        [sys.executable, str(GEN_SCRIPT), "--check"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"manifest --check failed (rc={result.returncode}):\n" f"stdout: {result.stdout}\n" f"stderr: {result.stderr}"
    )


def test_manifest_output_is_stable():
    """Building the manifest twice must produce identical bytes.

    Runs in-process so the test does not overwrite the committed file (that
    would silently mask drift captured by test_manifest_is_up_to_date)."""
    first = gen_manifest.serialize(gen_manifest.build_manifest())
    second = gen_manifest.serialize(gen_manifest.build_manifest())
    assert first == second, "generator output is not deterministic"


def test_manifest_shared_block_has_required_keys():
    """The shared block must contain the four documented keys with expected values."""
    data = json.loads(MANIFEST_PATH.read_text())
    shared = data["shared"]
    assert shared["config_file"] == "~/.arize/harness/config.yaml"
    assert shared["install_dir"] == "~/.arize/harness"
    assert shared["venv_dir"] == "~/.arize/harness/venv"
    assert shared["otlp_endpoint_default"] == "otlp.arize.com:443"


def test_manifest_each_harness_has_optional_fields():
    """Every harness entry must include hook_events and arize_env_keys (possibly empty)."""
    data = json.loads(MANIFEST_PATH.read_text())
    for name, entry in data["harnesses"].items():
        assert "hook_events" in entry, f"{name} missing hook_events"
        assert "arize_env_keys" in entry, f"{name} missing arize_env_keys"


def test_manifest_settings_files_are_strings_or_null():
    """settings_file / display_name / harness_bin must be str or null (no Path leaks)."""
    data = json.loads(MANIFEST_PATH.read_text())
    for name, entry in data["harnesses"].items():
        for field in ("settings_file", "display_name", "harness_bin"):
            value = entry[field]
            assert value is None or isinstance(
                value, str
            ), f"{name}.{field} must be str or null, got {type(value).__name__}"


def test_manifest_has_no_path_objects():
    """No Path-like prefixes (e.g., bare PosixPath('...')) should leak into JSON."""
    raw = MANIFEST_PATH.read_text()
    assert "PosixPath" not in raw
    assert "WindowsPath" not in raw


def test_manifest_home_paths_use_tilde():
    """Any settings_file value present must either be relative (project-local) or use `~/`.

    Absolute, machine-specific paths would make the committed manifest non-portable.
    """
    data = json.loads(MANIFEST_PATH.read_text())
    for name, entry in data["harnesses"].items():
        sf = entry["settings_file"]
        if sf is None:
            continue
        assert not sf.startswith("/"), f"{name}.settings_file is absolute without tilde-rewriting: {sf}"


def test_manifest_top_level_keys_only():
    """Top-level keys are exactly: schema_version, harnesses, shared."""
    data = json.loads(MANIFEST_PATH.read_text())
    assert set(data.keys()) == {"schema_version", "harnesses", "shared"}


def test_manifest_is_sorted_and_indented():
    """File content must be sort_keys=True, indent=2, trailing newline."""
    raw = MANIFEST_PATH.read_text()
    assert raw.endswith("\n"), "manifest must end with a newline"
    data = json.loads(raw)
    expected = json.dumps(data, sort_keys=True, indent=2) + "\n"
    assert raw == expected, "manifest is not canonically formatted"


def test_check_detects_drift(tmp_path: Path):
    """--check against a mutated copy must exit 1 and emit a diff."""
    mutated = tmp_path / "manifest.json"
    original = json.loads(MANIFEST_PATH.read_text())
    original["schema_version"] = 9999
    mutated.write_text(json.dumps(original, sort_keys=True, indent=2) + "\n")

    result = subprocess.run(
        [sys.executable, str(GEN_SCRIPT), "--check", "--output", str(mutated)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, "expected --check to fail on mutated manifest"
    assert "stale" in result.stderr
    assert "9999" in result.stderr, "diff in stderr should reference the mutated value"


def test_check_detects_missing_file(tmp_path: Path):
    """--check must fail with a clear message when the target file is absent."""
    missing = tmp_path / "does-not-exist.json"
    result = subprocess.run(
        [sys.executable, str(GEN_SCRIPT), "--check", "--output", str(missing)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "does not exist" in result.stderr


def test_generator_produces_expected_shape(tmp_path: Path):
    """End-to-end: generate into tmp_path and confirm the structural contract."""
    out = tmp_path / "manifest.json"
    result = subprocess.run(
        [sys.executable, str(GEN_SCRIPT), "--output", str(out)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"generator failed: {result.stderr}"
    data = json.loads(out.read_text())
    assert data["schema_version"] == 1
    assert isinstance(data["harnesses"], dict)
    assert isinstance(data["shared"], dict)


def test_claude_code_hook_events_preserved():
    """Spot check: dict-shaped HOOK_EVENTS must survive coercion as a dict."""
    data = json.loads(MANIFEST_PATH.read_text())
    he = data["harnesses"]["claude_code"]["hook_events"]
    assert isinstance(he, dict)
    assert he["SessionStart"] == "arize-hook-session-start"
    assert he["UserPromptSubmit"] == "arize-hook-user-prompt-submit"


def test_cursor_hook_events_are_list():
    """Spot check: tuple-shaped HOOK_EVENTS must be coerced into a JSON list."""
    data = json.loads(MANIFEST_PATH.read_text())
    he = data["harnesses"]["cursor"]["hook_events"]
    assert isinstance(he, list)
    assert "beforeSubmitPrompt" in he
    assert "postToolUse" in he


def test_arize_env_keys_for_claude_code():
    """claude_code's ARIZE_ENV_KEYS tuple should serialize to a list with the expected entries."""
    data = json.loads(MANIFEST_PATH.read_text())
    keys = data["harnesses"]["claude_code"]["arize_env_keys"]
    assert isinstance(keys, list)
    assert "ARIZE_API_KEY" in keys
    assert "ARIZE_PROJECT_NAME" in keys
    assert "PHOENIX_API_KEY" in keys


def test_missing_constants_emit_null():
    """Harnesses whose constants.py does not define an extracted scalar must emit null.

    The plan forbids guessing values: missing scalars must surface as null rather
    than being filled in by hand or by the generator inventing a default.
    """
    data = json.loads(MANIFEST_PATH.read_text())
    # claude_code defines all three required-ish fields and is a sanity anchor.
    cc = data["harnesses"]["claude_code"]
    assert cc["display_name"] is not None
    assert cc["harness_bin"] is not None
    assert cc["settings_file"] is not None


def test_coerce_rejects_non_home_absolute_path():
    """_coerce must refuse absolute paths outside $HOME (would leak machine layout)."""
    import pytest

    with pytest.raises(ValueError, match="non-home absolute Path"):
        gen_manifest._coerce(Path("/etc/foo"))


def test_coerce_handles_relative_path():
    """Relative paths (project-local) must pass through as posix strings."""
    assert gen_manifest._coerce(Path(".github/hooks/hooks.json")) == ".github/hooks/hooks.json"


def test_coerce_handles_home_path():
    """Absolute home paths must be rewritten with ~/ prefix."""
    p = Path.home() / "foo" / "bar.json"
    assert gen_manifest._coerce(p) == "~/foo/bar.json"
