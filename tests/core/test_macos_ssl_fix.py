"""Tests for the macOS SSL certificate fix in install.sh and .gitignore updates.

Validates:
- _fix_macos_ssl_certs() helper is defined and correctly structured
- The helper is called with the Darwin guard in setup_venv()
- The sitecustomize.py heredoc content is correct
- The helper uses graceful error handling (return 0)
- .gitignore has the new runtime artifact entries
"""

from __future__ import annotations

import os
import re

import pytest

INSTALL_SH = os.path.join(os.path.dirname(__file__), "..", "..", "install.sh")
GITIGNORE = os.path.join(os.path.dirname(__file__), "..", "..", ".gitignore")


def _read_install_sh() -> str:
    with open(INSTALL_SH) as f:
        return f.read()


def _read_gitignore() -> str:
    with open(GITIGNORE) as f:
        return f.read()


# ---------------------------------------------------------------------------
# _fix_macos_ssl_certs() function definition and structure
# ---------------------------------------------------------------------------


class TestFixMacOSSslCertsDefined:
    """Verify _fix_macos_ssl_certs() is defined with correct structure."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.text = _read_install_sh()

    def test_function_defined(self):
        pattern = r"^_fix_macos_ssl_certs\s*\(\)"
        assert re.search(pattern, self.text, re.MULTILINE), "_fix_macos_ssl_certs() not defined in install.sh"

    def test_accepts_pip_argument(self):
        assert 'local pip="$1"' in self.text

    def test_calls_venv_python(self):
        assert "venv_python" in self.text

    def test_installs_certifi(self):
        assert "install --quiet certifi" in self.text

    def test_warns_on_certifi_failure(self):
        assert "Could not install certifi" in self.text

    def test_gets_certifi_where(self):
        assert "import certifi; print(certifi.where())" in self.text

    def test_gets_site_packages_dir(self):
        assert "import site; print(site.getsitepackages()[0])" in self.text

    def test_writes_sitecustomize_py(self):
        assert "sitecustomize.py" in self.text

    def test_info_message_on_success(self):
        assert "SSL certificates configured via certifi" in self.text


# ---------------------------------------------------------------------------
# Graceful error handling
# ---------------------------------------------------------------------------


class TestGracefulErrorHandling:
    """_fix_macos_ssl_certs() must use return 0 for all failure paths."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.text = _read_install_sh()
        # Extract the function body
        match = re.search(
            r"^_fix_macos_ssl_certs\s*\(\)\s*\{(.+?)^\}",
            self.text,
            re.MULTILINE | re.DOTALL,
        )
        assert match, "_fix_macos_ssl_certs() body not found"
        self.body = match.group(1)

    def test_venv_python_failure_returns_zero(self):
        assert "venv_python 2>/dev/null) || return 0" in self.body

    def test_certifi_install_failure_returns_zero(self):
        """The warn + return 0 block after certifi install failure."""
        after_warn = self.body.split("Could not install certifi")[1]
        # return 0 should be within the next 2 lines after the warn
        next_lines = after_warn.split("\n")[0:3]
        assert any("return 0" in line for line in next_lines)

    def test_certifi_where_failure_returns_zero(self):
        assert 'certifi.where())" 2>/dev/null) || return 0' in self.body

    def test_empty_certifi_where_returns_zero(self):
        assert '[[ -z "$certifi_where" ]] && return 0' in self.body

    def test_site_dir_failure_returns_zero(self):
        assert 'getsitepackages()[0])" 2>/dev/null) || return 0' in self.body

    def test_no_exit_1_in_function(self):
        """The helper must never cause the script to abort."""
        assert "return 1" not in self.body
        assert "exit 1" not in self.body


# ---------------------------------------------------------------------------
# sitecustomize.py heredoc content
# ---------------------------------------------------------------------------


class TestSitecustomizeContent:
    """Verify the embedded sitecustomize.py is correct."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.text = _read_install_sh()
        # Extract content between PYEOF markers
        match = re.search(r"<<'PYEOF'\n(.+?)PYEOF", self.text, re.DOTALL)
        assert match, "PYEOF heredoc not found"
        self.heredoc = match.group(1)

    def test_imports_os(self):
        assert "import os as _os" in self.heredoc

    def test_imports_certifi_in_try(self):
        assert "import certifi as _certifi" in self.heredoc

    def test_calls_certifi_where(self):
        assert "_bundle = _certifi.where()" in self.heredoc

    def test_sets_ssl_cert_file(self):
        assert '_os.environ.setdefault("SSL_CERT_FILE", _bundle)' in self.heredoc

    def test_sets_requests_ca_bundle(self):
        assert '_os.environ.setdefault("REQUESTS_CA_BUNDLE", _bundle)' in self.heredoc

    def test_catches_import_error(self):
        assert "except ImportError:" in self.heredoc
        assert "pass" in self.heredoc

    def test_uses_setdefault_not_assignment(self):
        """setdefault respects user overrides; direct assignment would not."""
        assert "environ[" not in self.heredoc

    def test_uses_private_names(self):
        """Underscore-prefixed names avoid polluting the module namespace."""
        assert "_os" in self.heredoc
        assert "_certifi" in self.heredoc
        assert "_bundle" in self.heredoc

    def test_heredoc_is_single_quoted(self):
        """<<'PYEOF' prevents shell variable expansion in the heredoc."""
        assert "<<'PYEOF'" in self.text


# ---------------------------------------------------------------------------
# Darwin guard in setup_venv()
# ---------------------------------------------------------------------------


class TestDarwinGuardInSetupVenv:
    """Verify _fix_macos_ssl_certs is called with the Darwin guard."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.text = _read_install_sh()
        # Extract setup_venv body
        match = re.search(
            r"^setup_venv\s*\(\)\s*\{(.+?)^\}",
            self.text,
            re.MULTILINE | re.DOTALL,
        )
        assert match, "setup_venv() body not found"
        self.body = match.group(1)

    def test_darwin_guard_present(self):
        assert '$(uname)" == "Darwin"' in self.body

    def test_calls_fix_macos_ssl_certs(self):
        """SSL fix must be called from setup_venv()."""
        assert "_fix_macos_ssl_certs" in self.body

    def test_darwin_guard_wraps_all_ssl_calls(self):
        """Every _fix_macos_ssl_certs call must have a Darwin check on the same line."""
        for line in self.body.splitlines():
            if "_fix_macos_ssl_certs" in line:
                assert "Darwin" in line, "_fix_macos_ssl_certs must be guarded by Darwin check"

    def test_passes_pip_to_all_ssl_calls(self):
        """Every _fix_macos_ssl_certs call must receive $pip as its argument."""
        for line in self.body.splitlines():
            if "_fix_macos_ssl_certs" in line:
                assert '"$pip"' in line, '_fix_macos_ssl_certs must be called with "$pip"'

    def test_ssl_fix_before_venv_ready_message(self):
        """SSL fix runs before the final info message."""
        ssl_idx = self.body.rfind("_fix_macos_ssl_certs")
        ready_idx = self.body.find("Venv ready")
        assert ssl_idx < ready_idx, "_fix_macos_ssl_certs must be called before 'Venv ready' message"


# ---------------------------------------------------------------------------
# Function placement
# ---------------------------------------------------------------------------


class TestFunctionPlacement:
    """Verify _fix_macos_ssl_certs is defined before setup_venv."""

    def test_helper_defined_before_setup_venv(self):
        text = _read_install_sh()
        helper_idx = text.find("_fix_macos_ssl_certs()")
        setup_idx = text.find("setup_venv()")
        assert helper_idx < setup_idx, "_fix_macos_ssl_certs must be defined before setup_venv"

    def test_helper_in_venv_setup_section(self):
        """The helper should be in the 'Venv setup' section."""
        text = _read_install_sh()
        section_idx = text.find("Venv setup")
        helper_idx = text.find("_fix_macos_ssl_certs()")
        assert section_idx < helper_idx, "_fix_macos_ssl_certs should be in the 'Venv setup' section"


# ---------------------------------------------------------------------------
# .gitignore updates
# ---------------------------------------------------------------------------


class TestGitignoreUpdates:
    """Verify .gitignore has the new runtime artifact entries."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.text = _read_gitignore()
        self.lines = self.text.splitlines()

    def test_has_runtime_artifacts_comment(self):
        assert "# Runtime artifacts (generated at install/run time, not versioned)" in self.text

    def test_has_state_dir(self):
        assert "state/" in self.lines

    def test_has_config_json(self):
        assert "config.json" in self.lines

    def test_has_logs_dir(self):
        assert "logs/" in self.lines

    def test_has_run_dir(self):
        assert "run/" in self.lines

    def test_ends_with_newline(self):
        assert self.text.endswith("\n"), ".gitignore must end with a newline"

    def test_entries_grouped_under_comment(self):
        """All four entries should follow the comment header."""
        comment_idx = None
        for i, line in enumerate(self.lines):
            if "Runtime artifacts" in line:
                comment_idx = i
                break
        assert comment_idx is not None
        remaining = self.lines[comment_idx + 1 :]
        # Filter out blank lines
        entries = [line for line in remaining if line.strip()]
        expected = {"state/", "config.json", "logs/", "run/"}
        assert expected.issubset(set(entries)), f"Expected entries {expected} under comment, got {entries}"

    def test_no_duplicate_entries(self):
        """Entries should not appear twice."""
        for entry in ["state/", "config.json", "logs/", "run/"]:
            count = self.lines.count(entry)
            assert count == 1, f"{entry} appears {count} times in .gitignore"
