"""Detection and removal of legacy v1-codex-install artifacts.

This module exists only to clean up the previous architecture (proxy shim
in ~/.arize/harness/bin, PATH blocks in shell profiles). The installer
calls into it once at the top of `install()` to migrate v1 installs to
the hooks-based layout. Delete this file in a future release once we're
confident no v1 installs remain.
"""

from __future__ import annotations

import os
import re
import signal
import time
from pathlib import Path

from core.setup import BIN_DIR, dry_run, info
from tracing.codex._toml import _toml_load, _toml_write
from tracing.codex.constants import CODEX_CONFIG_FILE

_PATH_MARKER_BEGIN = "# >>> arize codex tracing PATH >>>"
_PATH_MARKER_END = "# <<< arize codex tracing PATH <<<"

# v1 OTLP endpoint pattern. Matches any 127.0.0.1 endpoint ending in /v1/logs
# to catch installs where the user customized the buffer port via config.yaml.
_V1_OTEL_ENDPOINT_RE = re.compile(r"^https?://127\.0\.0\.1:\d+/v1/logs$")


def _codex_proxy_shim_path() -> Path:
    """Return the primary path where the Arize-managed ``codex`` shim should live."""
    if os.name == "nt":
        return BIN_DIR / "codex.cmd"
    return BIN_DIR / "codex"


def _codex_proxy_shim_paths() -> list[Path]:
    """Return all Codex shim paths needed for the current platform."""
    if os.name == "nt":
        return [BIN_DIR / "codex.cmd", BIN_DIR / "codex"]
    return [BIN_DIR / "codex"]


def _is_our_codex_proxy_shim(path: Path) -> bool:
    """Return True only if *path* exists and is an Arize-managed codex shim."""
    if not path.is_file():
        return False
    try:
        text = path.read_text()
        return "arize-codex-proxy" in text and "Arize Codex proxy shim" in text
    except OSError:
        return False


def _remove_codex_proxy_shim(path: Path) -> None:
    """Remove the codex proxy shim at *path* only if it is Arize-owned.

    Honors ``dry_run()`` — logs intent without deleting.
    """
    if not path.exists():
        return

    if not _is_our_codex_proxy_shim(path):
        info(f"Skipping removal of {path} — not an Arize-managed shim")
        return

    if dry_run():
        info(f"would remove codex proxy shim at {path}")
        return

    path.unlink()


def _posix_shell_profiles() -> list[Path]:
    """Return sh/bash/zsh profile files that should receive the PATH block."""
    home = Path.home()
    profiles = [
        home / ".profile",
        home / ".bashrc",
        home / ".zshrc",
    ]
    for name in (".bash_profile", ".bash_login", ".zprofile", ".zlogin"):
        path = home / name
        if path.exists():
            profiles.append(path)
    return profiles


def _powershell_profiles() -> list[Path]:
    """Return PowerShell profile files for the current platform."""
    home = Path.home()
    if os.name == "nt":
        documents = home / "Documents"
        return [
            documents / "PowerShell" / "Microsoft.PowerShell_profile.ps1",
            documents / "WindowsPowerShell" / "Microsoft.PowerShell_profile.ps1",
        ]
    return [home / ".config" / "powershell" / "Microsoft.PowerShell_profile.ps1"]


def _profile_has_marker(text: str) -> bool:
    return _PATH_MARKER_BEGIN in text and _PATH_MARKER_END in text


def _remove_profile_block(path: Path) -> bool:
    """Remove the managed PATH block from *path* when present."""
    if not path.is_file():
        return False

    try:
        text = path.read_text()
    except OSError as exc:
        info(f"Warning: could not read {path}: {exc}")
        return False

    pattern = re.compile(
        rf"\n?{re.escape(_PATH_MARKER_BEGIN)}.*?{re.escape(_PATH_MARKER_END)}\n?",
        re.DOTALL,
    )
    new_text, count = pattern.subn("\n", text)
    if count == 0:
        return False

    new_text = re.sub(r"\n{3,}", "\n\n", new_text).lstrip("\n")
    if dry_run():
        info(f"would remove Arize harness bin PATH block from {path}")
        return False

    try:
        path.write_text(new_text)
    except OSError as exc:
        info(f"Warning: could not update {path}: {exc}")
        return False
    return True


def _remove_codex_proxy_path_blocks() -> None:
    """Remove shell profile PATH blocks written by the Codex installer."""
    profiles = list(dict.fromkeys(_posix_shell_profiles() + _powershell_profiles()))
    removed = [profile for profile in profiles if _remove_profile_block(profile)]
    if removed:
        joined = ", ".join(str(p) for p in removed)
        info(f"Removed Arize harness bin PATH block from: {joined}")


def _remove_windows_user_path_block() -> None:
    """Remove ``BIN_DIR`` from the Windows user PATH registry entry.

    No-op on non-Windows or when winreg is unavailable. Never raises.
    """
    if os.name != "nt":
        return

    path_str = str(BIN_DIR)
    if dry_run():
        info(f"would remove {path_str} from the Windows user PATH")
        return

    try:
        import winreg
    except ImportError:
        info("Warning: could not update Windows user PATH: winreg is unavailable")
        return

    try:
        hkey_current_user = getattr(winreg, "HKEY_CURRENT_USER")
        key_read = getattr(winreg, "KEY_READ")
        key_write = getattr(winreg, "KEY_WRITE")
        reg_expand_sz = getattr(winreg, "REG_EXPAND_SZ")
        reg_sz = getattr(winreg, "REG_SZ")
        create_key_ex = getattr(winreg, "CreateKeyEx")
        query_value_ex = getattr(winreg, "QueryValueEx")
        set_value_ex = getattr(winreg, "SetValueEx")

        with create_key_ex(hkey_current_user, "Environment", 0, key_read | key_write) as key:
            try:
                current, value_type = query_value_ex(key, "Path")
            except FileNotFoundError:
                return

            if not current:
                return

            def _normalize(value: str) -> str:
                normalized = os.path.normpath(os.path.expandvars(os.path.expanduser(value)))
                return normalized.replace("\\", "/").lower().rstrip("/")

            expected = _normalize(path_str)
            parts = [p for p in str(current).split(";") if p]
            kept = [p for p in parts if _normalize(p) != expected]
            if len(kept) == len(parts):
                return

            new_path = ";".join(kept)
            if value_type not in (reg_expand_sz, reg_sz):
                value_type = reg_expand_sz
            set_value_ex(key, "Path", 0, value_type, new_path)
    except OSError as exc:
        info(f"Warning: could not update Windows user PATH: {exc}")
        return

    try:
        import ctypes

        windll = getattr(ctypes, "windll", None)
        if windll is not None:
            windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0, 5000, None)
    except Exception:
        pass


def _strip_v1_otel_block(path: Path) -> None:
    """Strip a stale v1 ``[otel.exporter.otlp-http]`` block pointing at the
    local buffer service from ~/.codex/config.toml. Idempotent; no-op if the
    file or block is absent, or if the endpoint targets a foreign collector.
    """
    if not path.is_file():
        return

    data = _toml_load(path)
    otel = data.get("otel")
    if not isinstance(otel, dict):
        return
    exporter = otel.get("exporter")
    if not isinstance(exporter, dict):
        return
    otlp = exporter.get("otlp-http")
    if not isinstance(otlp, dict):
        return
    endpoint = otlp.get("endpoint", "")
    if not (isinstance(endpoint, str) and _V1_OTEL_ENDPOINT_RE.match(endpoint)):
        return

    if dry_run():
        info(f"would strip legacy [otel.exporter.otlp-http] block from {path}")
        return

    del exporter["otlp-http"]
    if not exporter:
        del otel["exporter"]
    if not otel:
        del data["otel"]
    _toml_write(data, path)
    info(f"Removed legacy [otel.exporter.otlp-http] block from {path}")


def _stop_legacy_buffer(pid_file: Path) -> None:
    """Best-effort termination of a v1 codex-buffer process from its pidfile."""
    try:
        pid_text = pid_file.read_text().strip()
        pid = int(pid_text)
    except (OSError, ValueError):
        pid = 0

    if pid > 1 and pid != os.getpid():
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(50):
                time.sleep(0.1)
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
        except ProcessLookupError:
            pass
        except OSError as e:
            info(f"Could not stop legacy buffer service (pid {pid}): {e}")

    try:
        pid_file.unlink()
    except OSError:
        pass
    info("Stopped legacy codex-buffer service")


def cleanup_legacy_install(codex_config_file: Path | None = None) -> None:
    """Run all detection-based legacy-artifact removals. Idempotent.

    Called once at the top of install() and uninstall() so v1 artifacts are
    cleaned up on both paths. Each step is gated by an _is_our_* check or
    equivalent so it never touches anything that isn't ours.

    *codex_config_file* defaults to ``~/.codex/config.toml`` and is parameterized
    so callers (and tests) can redirect it without monkeypatching this module.
    """
    if codex_config_file is None:
        codex_config_file = CODEX_CONFIG_FILE

    # 1. Stop the buffer service if its PID file exists.
    pid_file = Path.home() / ".arize" / "harness" / "run" / "codex-buffer.pid"
    if pid_file.is_file():
        if dry_run():
            info(f"would stop legacy codex-buffer service (pid file: {pid_file})")
        else:
            _stop_legacy_buffer(pid_file)

    # 2. Remove proxy shim.
    for path in _codex_proxy_shim_paths():
        if _is_our_codex_proxy_shim(path):
            _remove_codex_proxy_shim(path)
            info(f"Removed legacy codex proxy shim at {path}")

    # 3. Strip PATH blocks from shell profiles + Windows registry.
    _remove_codex_proxy_path_blocks()
    if os.name == "nt":
        _remove_windows_user_path_block()

    # 4. Strip the legacy [otel.exporter.otlp-http] block from ~/.codex/config.toml.
    _strip_v1_otel_block(codex_config_file)
