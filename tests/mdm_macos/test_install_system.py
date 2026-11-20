"""scripts/mdm/macos/install.sh: preflight, the pinned uv and the system install (part 1 of Arize-ai/arize#95899)."""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Dict, Optional

import pytest
from conftest import INSTALL_SH, TEST_KEY, _snapshot, _tree

from core import install_table


def _files(root: Path) -> list:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def _assert_nothing_installed(machine) -> None:
    assert not (machine.arize_dir / "bin").exists()
    assert not (machine.arize_dir / "tools").exists()
    assert not (machine.arize_dir / "mdm-wheel.sha256").exists()
    assert not (machine.arize_dir / "wheel").exists()
    assert not machine.bin_dir.exists() or list(machine.bin_dir.iterdir()) == []
    assert not machine.credentials_dir.exists()


def _assert_quiet_key(result) -> None:
    assert TEST_KEY not in result.stdout + result.stderr


def _error_lines(result) -> list:
    return [line for line in result.stderr.splitlines() if line.strip()]


def _rezip(
    src: Path, dest_dir: Path, metadata_extra: str = "", files: Optional[Dict[str, bytes]] = None, version: str = ""
) -> Path:
    """A copy of wheel ``src`` in ``dest_dir``: extra METADATA lines, replaced or added files, or a new version."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    old_version = src.name.split("-")[1]
    dest = dest_dir / (src.name.replace(f"-{old_version}-", f"-{version}-") if version else src.name)
    files = dict(files or {})
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = files.pop(item.filename, None) or zin.read(item)
            name = item.filename
            if version:
                name = name.replace(f"-{old_version}.dist-info/", f"-{version}.dist-info/")
            if name.endswith(".dist-info/METADATA"):
                if version:
                    data = data.replace(f"Version: {old_version}\n".encode(), f"Version: {version}\n".encode(), 1)
                if metadata_extra:
                    data = re.sub(rb"(?m)^(Version: .*)$", rb"\1\n" + metadata_extra.encode(), data, count=1)
            zout.writestr(name, data)
        for name, data in files.items():
            zout.writestr(name, data)
    return dest


def _site_packages(machine) -> Path:
    (site,) = (machine.arize_dir / "tools" / "coding-harness-tracing" / "lib").glob("python*/site-packages")
    return site


# ---------------------------------------------------------------------------
# Preflight: exit 2, one line, nothing installed
# ---------------------------------------------------------------------------

PREFLIGHT_CASES = [
    ("not-root", {"FAKE_UID": "501"}, "must run as root"),
    ("not-macos", {"FAKE_UNAME_S": "Linux"}, "macOS only"),
    ("no-key", {"ARIZE_API_KEY": None}, "missing API key"),
    ("no-space", {"ARIZE_SPACE_ID": None}, "missing space ID"),
    ("no-package", {"ARIZE_MDM_PACKAGE_SOURCE": None}, "missing package source"),
    ("no-harnesses", {"ARIZE_MDM_HARNESSES": None}, "empty harness list"),
    ("blank-harnesses", {"ARIZE_MDM_HARNESSES": " , "}, "empty harness list"),
    ("unknown-harness", {"ARIZE_MDM_HARNESSES": "claude-code vscode"}, "unknown harness: vscode"),
    ("copilot", {"ARIZE_MDM_HARNESSES": "cursor,copilot"}, "copilot is not supported"),
    ("pypi-version", {"ARIZE_MDM_PACKAGE_SOURCE": "coding-harness-tracing==1.2.3"}, "unsupported PACKAGE_SOURCE"),
    ("ftp-url", {"ARIZE_MDM_PACKAGE_SOURCE": "ftp://example.com/x.whl"}, "unsupported PACKAGE_SOURCE"),
    (
        "sdist-path",
        {"ARIZE_MDM_PACKAGE_SOURCE": "/tmp/coding_harness_tracing-1.0.tar.gz"},
        "unsupported PACKAGE_SOURCE",
    ),
    ("unsupported-cpu", {"FAKE_UNAME_M": "ppc"}, "unsupported CPU: ppc"),
    ("missing-wheel", {"ARIZE_MDM_PACKAGE_SOURCE": "/nonexistent/x.whl"}, "wheel not found"),
]


@pytest.mark.parametrize("overrides,message", [c[1:] for c in PREFLIGHT_CASES], ids=[c[0] for c in PREFLIGHT_CASES])
def test_preflight_exits_2_with_nothing_installed(machine, preflight_env, overrides, message):
    result = machine.run(INSTALL_SH, **preflight_env(**overrides))
    assert result.returncode == 2, result.stdout + result.stderr
    lines = _error_lines(result)
    assert len(lines) == 1 and message in lines[0], lines
    assert _files(machine.root) == []
    _assert_quiet_key(result)


def test_script_settings_win_over_environment(machine, preflight_env, tmp_path):
    script = tmp_path / "install.sh"
    script.write_text(INSTALL_SH.read_text().replace('\nHARNESSES=""', '\nHARNESSES="copilot"', 1))
    result = machine.run(script, **preflight_env(ARIZE_MDM_HARNESSES="cursor"))
    assert result.returncode == 2
    assert "copilot is not supported" in result.stderr


def test_supported_harnesses_match_setup():
    match = re.search(r'^SUPPORTED_HARNESSES="([^"]*)"', INSTALL_SH.read_text(), re.M)
    assert match
    expected = (set(install_table.INSTALLERS) | set(install_table.ALIASES)) - {"copilot"}
    assert set(match.group(1).split()) == expected


# ---------------------------------------------------------------------------
# uv and wheel staging: exit 1, nothing installed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "use_tarball,message",
    [(True, "uv checksum mismatch"), (False, "uv file not found")],
    ids=["checksum-mismatch", "missing-local-uv"],
)
def test_bad_uv_exits_1(machine, preflight_env, uv_release, use_tarball, message):
    """Runs the real install.sh: the local uv doesn't match its pins."""
    overrides: Dict[str, str] = {}
    if use_tarball:
        overrides["ARIZE_MDM_UV_SOURCE"] = str(uv_release.tarball)
    result = machine.run(INSTALL_SH, **preflight_env(**overrides))
    assert result.returncode == 1
    assert message in result.stderr
    assert _files(machine.root) == []
    _assert_quiet_key(result)


def _bad_wheel_sources(work: Path) -> dict:
    garbage = work / "coding_harness_tracing-1.0-py3-none-any.whl"
    garbage.write_bytes(b"not a zip")
    return {
        "garbage": (str(garbage), "not a valid wheel"),
        "unreachable-url": ("http://127.0.0.1:9/coding_harness_tracing-1.0-py3-none-any.whl", "cannot download"),
    }


@pytest.mark.parametrize("case", ["garbage", "unreachable-url"])
def test_bad_wheel_exits_1(machine, preflight_env, fake_uv_pin, case):
    source, message = _bad_wheel_sources(machine.work)[case]
    result = machine.run(
        INSTALL_SH,
        **preflight_env(
            ARIZE_MDM_UV_SOURCE=str(fake_uv_pin.tarball),
            ARIZE_MDM_PACKAGE_SOURCE=source,
        ),
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert message in result.stderr
    assert _files(machine.root) == []
    _assert_quiet_key(result)


# ---------------------------------------------------------------------------
# The system install (slow: builds the wheel and runs uv tool install)
# ---------------------------------------------------------------------------


def _harness_version(machine) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(machine.harness_bin), "--version"], capture_output=True, text=True, timeout=60, check=False
    )


@pytest.mark.slow
class TestSystemInstall:
    def test_installs_uv_and_a_runnable_arize_harness(self, machine, install_env, wheel):
        result = machine.run(INSTALL_SH, **install_env())
        assert result.returncode == 0, result.stdout + result.stderr
        _assert_quiet_key(result)

        version = _harness_version(machine)
        assert version.returncode == 0, version.stderr
        assert "9.9.9" in version.stdout + version.stderr
        assert machine.harness_bin.resolve().is_relative_to(machine.arize_dir / "tools" / "coding-harness-tracing")
        receipt = (machine.arize_dir / "tools" / "coding-harness-tracing" / "uv-receipt.toml").read_text()
        assert str(machine.arize_dir / "wheel" / wheel.name) in receipt
        unreadable = [p for p in machine.arize_dir.rglob("*") if not p.is_symlink() and not p.stat().st_mode & 0o004]
        assert unreadable == []

        uv = machine.arize_dir / "bin" / "uv"
        assert uv.stat().st_mode & 0o777 == 0o755
        assert subprocess.run([str(uv), "--version"], capture_output=True, text=True).stdout.startswith("uv ")
        assert list((machine.arize_dir / "tools" / "coding-harness-tracing").rglob("*.pyc"))

    def test_rerun_changes_no_files(self, machine, install_env):
        env = install_env()
        assert machine.run(INSTALL_SH, **env).returncode == 0
        before = (_tree(machine.root), _snapshot(machine.root))
        again = machine.run(INSTALL_SH, **env)
        assert again.returncode == 0, again.stdout + again.stderr
        assert "system install is current" in again.stdout
        assert (_tree(machine.root), _snapshot(machine.root)) == before

    @pytest.mark.parametrize("version", ["", "9.9.10"], ids=["same-version", "new-version"])
    def test_new_wheel_upgrades(self, machine, install_env, wheel, version):
        assert machine.run(INSTALL_SH, **install_env()).returncode == 0
        newer = _rezip(wheel, machine.work / "newer", files={"core/_mdm_marker.py": b""}, version=version)
        result = machine.run(INSTALL_SH, **install_env(ARIZE_MDM_PACKAGE_SOURCE=str(newer)))
        assert result.returncode == 0, result.stdout + result.stderr
        assert "system install ready" in result.stdout
        assert (_site_packages(machine) / "core" / "_mdm_marker.py").is_file()
        assert [p.name for p in (machine.arize_dir / "wheel").iterdir()] == [newer.name]
        mark = (machine.arize_dir / "mdm-wheel.sha256").read_text().strip()
        assert mark == hashlib.sha256(newer.read_bytes()).hexdigest()
        assert _harness_version(machine).returncode == 0

    def _unresolvable(self, machine, wheel) -> Path:
        return _rezip(wheel, machine.work / "broken", metadata_extra="Requires-Dist: arize-mdm-no-such-package")

    def test_failed_first_install_on_an_empty_root_leaves_nothing(self, machine, install_env, wheel):
        env = install_env()
        shutil.rmtree(machine.root / "opt")  # no seeded Python: the offline install fails
        result = machine.run(INSTALL_SH, **env)
        assert result.returncode == 1, result.stdout + result.stderr
        assert _files(machine.root) == []

    def test_unusable_wheel_on_first_install_leaves_nothing(self, machine, install_env, wheel):
        machine.bin_dir.mkdir(parents=True)
        (machine.bin_dir / "other-tool").write_text("#!/bin/sh\n")
        source = self._unresolvable(machine, wheel)
        result = machine.run(INSTALL_SH, **install_env(ARIZE_MDM_PACKAGE_SOURCE=str(source)))
        assert result.returncode == 1, result.stdout + result.stderr
        assert "uv tool install failed" in result.stderr
        assert [p.name for p in machine.bin_dir.iterdir()] == ["other-tool"]
        machine.bin_dir.joinpath("other-tool").unlink()
        _assert_nothing_installed(machine)

    @pytest.mark.parametrize(
        "kind,version",
        [("unresolvable", ""), ("crashes", ""), ("crashes", "9.9.10")],
        ids=["unresolvable", "crashes-same-version", "crashes-new-version"],
    )
    def test_unusable_wheel_on_upgrade_keeps_the_install(self, machine, install_env, wheel, kind, version):
        assert machine.run(INSTALL_SH, **install_env()).returncode == 0
        if kind == "unresolvable":
            source = self._unresolvable(machine, wheel)
        else:
            source = _rezip(
                wheel, machine.work / "broken", files={"core/cli.py": b"raise SystemExit(3)\n"}, version=version
            )
        result = machine.run(INSTALL_SH, **install_env(ARIZE_MDM_PACKAGE_SOURCE=str(source)))
        assert result.returncode == 1
        assert "could not restore" not in result.stderr
        assert [p.name for p in (machine.arize_dir / "wheel").iterdir()] == [wheel.name]
        assert (machine.arize_dir / "bin" / "uv").is_file()
        assert _harness_version(machine).returncode == 0
        assert (machine.arize_dir / "wheel" / wheel.name).read_bytes() == wheel.read_bytes()
        assert not list((machine.arize_dir / "wheel").glob("*.bak"))
        again = machine.run(INSTALL_SH, **install_env())
        assert again.returncode == 0 and "system install is current" in again.stdout
