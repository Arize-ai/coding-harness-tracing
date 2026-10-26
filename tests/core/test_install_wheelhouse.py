"""Run install.sh update against a hand-built wheelhouse, so the pip bootstrap,
certifi refresh and -U behavior are exercised instead of grepped for."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
INSTALL_SH = ROOT / "install.sh"
PIP_WHEEL = "pip-26.2.2-py3-none-any.whl"
STALE_PYTHON = "#!/bin/sh\nexit 1\n"
PIP_SHA256 = "fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70"


def _pip_wheel_bytes() -> bytes | None:
    import ensurepip
    import re
    import urllib.request

    bundled = Path(ensurepip.__file__).parent / "_bundled" / PIP_WHEEL
    if bundled.is_file():
        return bundled.read_bytes()
    url = re.search(r'PIP_WHEEL_URL="([^"#]*)', INSTALL_SH.read_text())
    try:
        with urllib.request.urlopen(url.group(1).replace("${PIP_WHEEL}", PIP_WHEEL), timeout=30) as response:
            return response.read()
    except OSError:
        return None


def _build_wheel(directory: Path, name: str, version: str, files: dict[str, str]) -> None:
    dist_info = f"{name}-{version}.dist-info"
    members = {
        **files,
        f"{dist_info}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        f"{dist_info}/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = [f"{dist_info}/RECORD,,"]
    for path, content in members.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content.encode()).digest()).rstrip(b"=").decode()
        record.append(f"{path},sha256={digest},{len(content.encode())}")
    with zipfile.ZipFile(directory / f"{name}-{version}-py3-none-any.whl", "w") as wheel:
        for path, content in members.items():
            wheel.writestr(path, content)
        wheel.writestr(f"{dist_info}/RECORD", "\n".join(record) + "\n")


def _wheelhouse(directory: Path, project_version: str, certifi_version: str | None = "2026.10.19") -> Path:
    pip_wheel = _pip_wheel_bytes()
    if pip_wheel is None or hashlib.sha256(pip_wheel).hexdigest() != PIP_SHA256:
        pytest.skip(f"{PIP_WHEEL} is neither bundled nor downloadable")
    directory.mkdir()
    (directory / PIP_WHEEL).write_bytes(pip_wheel)
    _build_wheel(directory, "coding_harness_tracing", project_version, {"core/__init__.py": ""})
    if certifi_version:
        _build_wheel(
            directory,
            "certifi",
            certifi_version,
            {"certifi/__init__.py": "def where():\n    return '/dev/null'\n"},
        )
    return directory


@pytest.fixture
def sandbox(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python3").symlink_to(sys.executable)
    (bin_dir / "uname").write_text("#!/bin/sh\necho Darwin\n")
    (bin_dir / "uname").chmod(0o755)
    home = tmp_path / "home"
    (home / ".arize" / "harness").mkdir(parents=True)
    return tmp_path, home, bin_dir


def _update(
    sandbox, wheel_dir: Path, pip_config_file: str | None = os.devnull, env_extra: dict | None = None
) -> subprocess.CompletedProcess:
    _, home, bin_dir = sandbox
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith(("PIP_", "ARIZE_", "PYTHON"))},
        "HOME": str(home),
        "NO_COLOR": "1",
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
    }
    env.update(env_extra or {})
    if pip_config_file:
        env["PIP_CONFIG_FILE"] = pip_config_file
    return subprocess.run(
        ["bash", str(INSTALL_SH), "update", "--wheel-dir", str(wheel_dir), "--non-interactive"],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        stdin=subprocess.DEVNULL,
    )


def _installed_version(home: Path, package: str) -> str | None:
    venv_python = home / ".arize" / "harness" / "venv" / "bin" / "python"
    if not venv_python.exists():
        return None
    result = subprocess.run(
        [str(venv_python), "-I", "-c", f"from importlib.metadata import version; print(version('{package}'))"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() or None


def test_update_installs_a_newer_project_wheel(sandbox):
    tmp_path, home, _ = sandbox
    first = _update(sandbox, _wheelhouse(tmp_path / "v1", "0.1.0"))
    assert first.returncode == 0, first.stderr
    assert _installed_version(home, "coding-harness-tracing") == "0.1.0"

    second = _update(sandbox, _wheelhouse(tmp_path / "v2", "0.2.0"))

    assert second.returncode == 0, second.stderr
    assert _installed_version(home, "coding-harness-tracing") == "0.2.0"


def test_update_stops_when_the_certifi_refresh_fails(sandbox):
    tmp_path, home, _ = sandbox
    stale = _update(sandbox, _wheelhouse(tmp_path / "v1", "0.1.0", certifi_version="2026.2.25"))
    assert stale.returncode != 0
    assert "Update complete" not in stale.stdout


def test_update_stops_on_a_corrupt_pip_wheel(sandbox):
    tmp_path, home, _ = sandbox
    wheels = _wheelhouse(tmp_path / "v1", "0.1.0")
    (wheels / PIP_WHEEL).write_bytes(b"tampered")

    result = _update(sandbox, wheels)

    assert result.returncode != 0
    assert "failed SHA-256 verification" in result.stderr
    assert "Update complete" not in result.stdout
    assert _installed_version(home, "coding-harness-tracing") is None


def test_pip_bootstrap_ignores_venv_pip_configuration(sandbox):
    tmp_path, home, _ = sandbox
    venv = home / ".arize" / "harness" / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    (venv / "pip.conf").write_text("[install]\nconstraint = /nonexistent-constraints.txt\n")

    result = _update(sandbox, _wheelhouse(tmp_path / "v1", "0.1.0"), pip_config_file=None)

    assert "Securing pip" in result.stdout
    assert "Failed to install verified pip" not in result.stderr


def _stale_venv(home: Path) -> Path:
    venv = home / ".arize" / "harness" / "venv"
    (venv / "bin").mkdir(parents=True)
    python = venv / "bin" / "python"
    python.write_text(STALE_PYTHON)
    python.chmod(0o755)
    return venv


def test_corrupt_pip_wheel_keeps_the_old_venv(sandbox):
    tmp_path, home, _ = sandbox
    venv = _stale_venv(home)
    wheels = _wheelhouse(tmp_path / "v1", "0.1.0")
    (wheels / PIP_WHEEL).write_bytes(b"tampered")

    result = _update(sandbox, wheels)

    assert result.returncode != 0
    assert (venv / "bin" / "python").read_text() == STALE_PYTHON


@pytest.mark.parametrize("wheel_state", ["missing", "outdated", "corrupt"])
def test_unusable_certifi_wheel_keeps_the_old_venv(sandbox, wheel_state):
    tmp_path, home, _ = sandbox
    venv = _stale_venv(home)
    version = {"missing": None, "outdated": "2026.2.25", "corrupt": "2026.10.19"}[wheel_state]
    wheels = _wheelhouse(tmp_path / "v1", "0.1.0", certifi_version=version)
    if wheel_state == "corrupt":
        next(wheels.glob("certifi-*.whl")).write_bytes(b"tampered")

    result = _update(sandbox, wheels)

    assert result.returncode != 0
    assert (venv / "bin" / "python").read_text() == STALE_PYTHON
    assert f"certifi>=2026.10.19 wheel in {wheels}" in result.stderr


def test_missing_project_wheel_keeps_the_old_venv(sandbox):
    tmp_path, home, _ = sandbox
    venv = _stale_venv(home)
    wheels = _wheelhouse(tmp_path / "v1", "0.1.0")
    next(wheels.glob("coding_harness_tracing-*.whl")).unlink()

    result = _update(sandbox, wheels)

    assert result.returncode != 0
    assert (venv / "bin" / "python").read_text() == STALE_PYTHON


CA_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "PIP_CERT")
STALE_BUNDLE = "/stale/certifi/cacert.pem"
RECORDER = """\
import json, os, sys
with open({log!r}, "a") as log:
    log.write(json.dumps({{"argv": sys.orig_argv, "ca": {{k: os.environ.get(k) for k in {ca_vars!r}}}}}) + "\\n")
"""


def _pip_starts(log: Path) -> list[dict]:
    records = [json.loads(line) for line in log.read_text().splitlines()]
    return [r for r in records if "pip" in r["argv"][1:3] or r["argv"][1].endswith("/pip")]


def test_pip_never_sees_a_ca_bundle_override(sandbox):
    tmp_path, _, _ = sandbox
    log = tmp_path / "starts.log"
    recorder = tmp_path / "recorder"
    recorder.mkdir()
    (recorder / "sitecustomize.py").write_text(RECORDER.format(log=str(log), ca_vars=CA_VARS))
    overrides = {var: STALE_BUNDLE for var in CA_VARS} | {"PYTHONPATH": str(recorder)}

    result = _update(sandbox, _wheelhouse(tmp_path / "v1", "0.1.0"), env_extra=overrides)

    assert result.returncode == 0, result.stderr
    starts = _pip_starts(log)
    assert starts
    assert all(not any(start["ca"].values()) for start in starts)


def test_certifi_is_refreshed_before_the_project_installs(sandbox):
    tmp_path, _, _ = sandbox
    log = tmp_path / "starts.log"
    recorder = tmp_path / "recorder"
    recorder.mkdir()
    (recorder / "sitecustomize.py").write_text(RECORDER.format(log=str(log), ca_vars=CA_VARS))

    result = _update(sandbox, _wheelhouse(tmp_path / "v1", "0.1.0"), env_extra={"PYTHONPATH": str(recorder)})

    assert result.returncode == 0, result.stderr
    installs = [" ".join(start["argv"]) for start in _pip_starts(log) if "install" in start["argv"]]
    certifi_at = next(i for i, cmd in enumerate(installs) if "certifi" in cmd)
    project_at = next(i for i, cmd in enumerate(installs) if "coding-harness-tracing" in cmd)
    assert certifi_at < project_at


def test_a_stale_sitecustomize_does_not_reach_pip(sandbox):
    tmp_path, home, _ = sandbox
    log = tmp_path / "starts.log"
    venv = home / ".arize" / "harness" / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    site_packages = next(venv.glob("lib/python*/site-packages"))
    (site_packages / "sitecustomize.py").write_text(RECORDER.format(log=str(log), ca_vars=CA_VARS))

    result = _update(sandbox, _wheelhouse(tmp_path / "v1", "0.1.0"))

    assert result.returncode == 0, result.stderr
    assert not log.exists() or not _pip_starts(log)
