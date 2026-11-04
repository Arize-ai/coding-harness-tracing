from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
PIP_WHEEL = "pip-26.2.2-py3-none-any.whl"
PIP_VERSION = "26.2.2"
PIP_SHA256 = "fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70"
CERTIFI_VERSION = "2026.10.23"
CERTIFI_SHA256 = "e9b54de76e530bb6cf0ccd036787103acb58fd42ba930ea8bf948ddbaa2f61d1"
PINNED_BOOTSTRAPS = [
    ".github/workflows/ci.yml",
    ".github/workflows/windows-hooks.yml",
    "install.bat",
    "install.sh",
    "scripts/test-in-container.sh",
    "tracing/cursor/scripts/run-hook",
]


@pytest.mark.parametrize("relative_path", PINNED_BOOTSTRAPS)
def test_pip_bootstraps_share_the_authenticated_wheel(relative_path):
    bootstrap = (ROOT / relative_path).read_text()

    assert PIP_WHEEL in bootstrap or (
        f'PIP_VERSION="{PIP_VERSION}"' in bootstrap and "pip-${PIP_VERSION}-py3-none-any.whl" in bootstrap
    )
    assert PIP_SHA256 in bootstrap


def test_installer_pins_certifi_by_hash():
    installer = (ROOT / "install.sh").read_text()
    assert f'CERTIFI_VERSION="{CERTIFI_VERSION}"' in installer
    assert CERTIFI_SHA256 in installer


@pytest.mark.parametrize("relative_path", [".github/workflows/ci.yml", "scripts/smoke-install.sh"])
def test_wheelhouses_fetch_the_pinned_certifi(relative_path):
    """install.sh looks for exactly this file in --wheel-dir, so a range here
    would drift to a newer wheel the installer refuses."""
    assert f"certifi=={CERTIFI_VERSION}" in (ROOT / relative_path).read_text()
