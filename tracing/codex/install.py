#!/usr/bin/env python3
"""Codex harness install / uninstall module.

Self-contained module that handles:
- Writing ~/.codex/arize-env.sh (env file)
- Updating ~/.codex/config.toml (TOML config with notify + otel exporter)
- Starting/stopping the codex buffer service
- Managing the shared config.yaml harness entry
- Symlinking skills
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import sys
from pathlib import Path

from core.config import get_value, load_config
from core.setup import (
    BIN_DIR,
    CONFIG_FILE,
    dry_run,
    ensure_harness_installed,
    ensure_shared_runtime,
    info,
    merge_harness_entry,
    prompt_backend,
    prompt_content_logging,
    prompt_project_name,
    prompt_user_id,
    remove_harness_entry,
    symlink_skills,
    unlink_skills,
    venv_bin,
    write_config,
    write_logging_config,
)
from tracing.codex.codex_buffer_ctl import buffer_start, buffer_status, buffer_stop
from tracing.codex.constants import OTEL_ENDPOINT  # noqa: F401 — re-exported for backwards compat
from tracing.codex.constants import (
    BUFFER_PORT,
    CODEX_CONFIG_DIR,
    CODEX_CONFIG_FILE,
    CODEX_ENV_FILE,
    DISPLAY_NAME,
    HARNESS_BIN,
    HARNESS_HOME,
    HARNESS_NAME,
    NOTIFY_BIN_NAME,
)

# Try to import tomllib (3.11+), then tomli, then fall back to None
_tomllib = None
try:
    import tomllib as _tomllib  # type: ignore[no-redef]
except ImportError:
    try:
        import tomli as _tomllib  # type: ignore[no-redef]
    except ImportError:
        pass


# ---------------------------------------------------------------------------
# TOML helpers
# ---------------------------------------------------------------------------


def _toml_load(path: Path) -> dict:
    """Load a TOML file into a dict. Falls back to line-based parsing.

    If the file is malformed (e.g. another tool wrote unquoted keys with
    `@` or `/`), fall back to the lenient line parser rather than crashing
    so install/uninstall can still proceed.
    """
    if not path.is_file():
        return {}
    text = path.read_text()
    if _tomllib is not None:
        try:
            return _tomllib.loads(text)
        except Exception:
            pass
    return _toml_line_parse(text)


def _toml_extract_section(line: str) -> str | None:
    """Extract the inner path from a ``[section]`` header, quote-aware.

    Returns ``None`` when *line* is not a valid section header.
    """
    if not line.startswith("[") or line.startswith("[["):
        return None
    in_quotes = False
    escape = False
    for i, ch in enumerate(line):
        if i == 0:
            continue  # skip opening '['
        if escape:
            escape = False
            continue
        if in_quotes:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_quotes = False
        else:
            if ch == '"':
                in_quotes = True
            elif ch == "]":
                if line[i + 1 :].strip() == "":
                    return line[1:i]
                return None
    return None


def _toml_split_kv(line: str) -> tuple[str, str] | None:
    """Split ``key = value`` respecting quoted keys (e.g. ``"a=b" = 'x'``).

    Returns ``(raw_key, raw_value)`` or ``None`` if the line isn't a kv pair.
    """
    in_quotes = False
    escape = False
    for i, ch in enumerate(line):
        if escape:
            escape = False
            continue
        if in_quotes:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_quotes = False
        else:
            if ch == '"':
                in_quotes = True
            elif ch == "=":
                key = line[:i].strip()
                val = line[i + 1 :].strip()
                if key:
                    return (key, val)
                return None
    return None


def _toml_line_parse(text: str) -> dict:
    """Minimal TOML parser — handles flat keys and sections for our use case."""
    result: dict = {}
    current_section: dict = result
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Section header (quote-aware — handles ] inside quoted keys)
        section_inner = _toml_extract_section(line)
        if section_inner is not None:
            keys = _toml_split_key_path(section_inner)
            current_section = result
            for k in keys:
                if k not in current_section:
                    current_section[k] = {}
                current_section = current_section[k]
            continue
        # Key = value (quote-aware — handles = inside quoted keys)
        kv = _toml_split_kv(line)
        if kv:
            key = _toml_unkey(kv[0])
            val_raw = kv[1]
            # Handle array values like ["cmd"] or ['cmd']
            if val_raw.startswith("["):
                items = []
                for item in re.findall(r'"([^"]*)"|\'([^\']*)\'', val_raw):
                    items.append(item[0] or item[1])
                current_section[key] = items
            elif (val_raw.startswith('"') and val_raw.endswith('"')) or (
                val_raw.startswith("'") and val_raw.endswith("'")
            ):
                current_section[key] = val_raw[1:-1]
            elif val_raw.lower() in ("true", "false"):
                current_section[key] = val_raw.lower() == "true"
            else:
                try:
                    current_section[key] = int(val_raw)
                except ValueError:
                    current_section[key] = val_raw
    return result


def _toml_write(data: dict, path: Path) -> None:
    """Write a dict as TOML. Hand-rolled — no tomli-w dependency."""
    lines: list[str] = []
    _toml_write_section(data, [], lines)
    path.write_text("\n".join(lines) + "\n")


_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_key(key: str) -> str:
    """Quote a TOML key if it contains characters not allowed in bare keys."""
    if _BARE_KEY_RE.match(key):
        return key
    escaped = key.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_unkey(key: str) -> str:
    """Inverse of _toml_key — strip quotes and unescape a TOML key."""
    if len(key) >= 2 and key.startswith('"') and key.endswith('"'):
        inner = key[1:-1]
        inner = inner.replace('\\"', '"')
        inner = inner.replace("\\\\", "\\")
        return inner
    return key


def _toml_split_key_path(path: str) -> list[str]:
    """Split a dotted TOML key path respecting quoted segments.

    Examples:
        'a.b.c' -> ['a', 'b', 'c']
        'mcp_servers."@scope/server"' -> ['mcp_servers', '@scope/server']
        'mcp_servers."a.b.c"' -> ['mcp_servers', 'a.b.c']
    """
    segments: list[str] = []
    buf: list[str] = []
    in_quotes = False
    escape = False
    for ch in path:
        if escape:
            buf.append(ch)
            escape = False
            continue
        if in_quotes:
            if ch == "\\":
                buf.append(ch)
                escape = True
            elif ch == '"':
                buf.append(ch)
                in_quotes = False
            else:
                buf.append(ch)
        else:
            if ch == '"':
                buf.append(ch)
                in_quotes = True
            elif ch == ".":
                segments.append(_toml_unkey("".join(buf).strip()))
                buf = []
            else:
                buf.append(ch)
    # Flush remaining buffer
    segments.append(_toml_unkey("".join(buf).strip()))
    return segments


def _toml_write_section(data: dict, prefix: list[str], lines: list[str]) -> None:
    """Recursively write TOML sections."""
    # Write scalar/array keys first
    for key, val in data.items():
        if isinstance(val, dict):
            continue
        _toml_write_value(key, val, lines)

    # Then nested sections
    for key, val in data.items():
        if not isinstance(val, dict):
            continue
        section_path = prefix + [key]
        # Check if this section has direct scalar values
        has_scalars = any(not isinstance(v, dict) for v in val.values())
        if has_scalars or not val:
            if lines and lines[-1] != "":
                lines.append("")
            lines.append(f"[{'.'.join(_toml_key(k) for k in section_path)}]")
        _toml_write_section(val, section_path, lines)


def _toml_write_value(key: str, val: object, lines: list[str]) -> None:
    """Write a single TOML key-value pair."""
    k = _toml_key(key)
    if isinstance(val, list):
        items = ", ".join(_toml_string_literal(v) for v in val)
        lines.append(f"{k} = [{items}]")
    elif isinstance(val, bool):
        lines.append(f"{k} = {'true' if val else 'false'}")
    elif isinstance(val, int):
        lines.append(f"{k} = {val}")
    else:
        lines.append(f"{k} = {_toml_string_literal(val)}")


def _toml_string_literal(val: object) -> str:
    """Render a string as a TOML literal '...' — no escape handling needed,
    which matches `_toml_line_parse` semantics and is safe for Windows paths
    with backslashes. Falls back to an escaped basic string if the value
    contains a single quote or newline (which literal strings cannot carry).
    """
    s = str(val)
    if "'" in s or "\n" in s or "\r" in s:
        escaped = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
        return f'"{escaped}"'
    return f"'{s}'"


# ---------------------------------------------------------------------------
# Codex TOML config management
# ---------------------------------------------------------------------------


def _codex_toml_add(path: Path, notify_cmd: str, otel_endpoint: str) -> None:
    """Add notify command and otel exporter to codex config.toml. Idempotent."""
    if dry_run():
        info(f"would update {path} with notify and otel exporter")
        return

    data = _toml_load(path)

    # Set notify — array of commands
    existing_notify = data.get("notify", [])
    if not isinstance(existing_notify, list):
        existing_notify = [existing_notify] if existing_notify else []
    if notify_cmd not in existing_notify:
        existing_notify.append(notify_cmd)
    data["notify"] = existing_notify

    # Set otel exporter
    if "otel" not in data:
        data["otel"] = {}
    otel = data["otel"]
    if "exporter" not in otel:
        otel["exporter"] = {}
    otel["exporter"]["otlp-http"] = {
        "endpoint": otel_endpoint,
        "protocol": "json",
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    _toml_write(data, path)


def _codex_toml_remove(path: Path, notify_cmd: str, otel_endpoint: str) -> None:
    """Remove our notify command and otel exporter from codex config.toml. Idempotent."""
    if not path.is_file():
        return

    if dry_run():
        info(f"would revert {path}: remove notify={notify_cmd} and otel exporter")
        return

    data = _toml_load(path)
    changed = False

    # Remove our notify entry only if it matches
    existing_notify = data.get("notify", [])
    if isinstance(existing_notify, list) and notify_cmd in existing_notify:
        existing_notify.remove(notify_cmd)
        if existing_notify:
            data["notify"] = existing_notify
        else:
            del data["notify"]
        changed = True
    elif isinstance(existing_notify, str) and existing_notify == notify_cmd:
        del data["notify"]
        changed = True

    # Remove otel exporter only if it points at our endpoint
    if "otel" in data and "exporter" in data["otel"] and "otlp-http" in data["otel"]["exporter"]:
        otlp_http = data["otel"]["exporter"]["otlp-http"]
        if isinstance(otlp_http, dict) and otlp_http.get("endpoint") == otel_endpoint:
            del data["otel"]["exporter"]["otlp-http"]
            changed = True
            # Clean up empty parents
            if not data["otel"]["exporter"]:
                del data["otel"]["exporter"]
            if not data["otel"]:
                del data["otel"]

    if changed:
        _toml_write(data, path)


# ---------------------------------------------------------------------------
# Env file management
# ---------------------------------------------------------------------------


def _write_env_file(path: Path, user_id: str = "") -> None:
    """Write the codex env file with ARIZE env exports."""
    if dry_run():
        info(f"would write env file {path}")
        return

    lines = [
        "export ARIZE_TRACE_ENABLED=true",
        f"export ARIZE_CODEX_BUFFER_PORT={BUFFER_PORT}",
    ]
    if user_id:
        lines.append(f"export ARIZE_USER_ID={user_id}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _is_our_env_file(path: Path) -> bool:
    """Check if the env file is one we wrote (safe heuristic)."""
    if not path.is_file():
        return False
    try:
        text = path.read_text()
        lines = [ln for ln in text.strip().splitlines() if ln.strip()]
        if len(lines) > 10:
            return False
        return all(re.match(r"^export ARIZE_", line) for line in lines)
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Codex proxy shim helpers
# ---------------------------------------------------------------------------


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


_PATH_MARKER_BEGIN = "# >>> arize codex tracing PATH >>>"
_PATH_MARKER_END = "# <<< arize codex tracing PATH <<<"

_POSIX_PATH_BLOCK = f"""{_PATH_MARKER_BEGIN}
# Required so "codex exec" runs through the Arize tracing proxy.
case ":$PATH:" in
  *":$HOME/.arize/harness/bin:"*) ;;
  *) export PATH="$HOME/.arize/harness/bin:$PATH" ;;
esac
{_PATH_MARKER_END}
"""

_POWERSHELL_PATH_BLOCK = f"""{_PATH_MARKER_BEGIN}
# Required so "codex exec" runs through the Arize tracing proxy.
$arizeHarnessBin = Join-Path $HOME ".arize/harness/bin"
if (($env:PATH -split [System.IO.Path]::PathSeparator) -notcontains $arizeHarnessBin) {{
    $env:PATH = "$arizeHarnessBin$([System.IO.Path]::PathSeparator)$env:PATH"
}}
{_PATH_MARKER_END}
"""


def _posix_shell_profiles() -> list[Path]:
    """Return sh/bash/zsh profile files that should receive the PATH block."""
    home = Path.home()
    profiles = [
        home / ".profile",
        home / ".bashrc",
        home / ".zshrc",
    ]
    # These login-shell files can change shell startup precedence, so only
    # update them when the user already has them.
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


def _ensure_profile_block(path: Path, block: str) -> bool:
    """Append a managed PATH block to *path* if it is not already present."""
    if dry_run():
        info(f"would add Arize harness bin to PATH in {path}")
        return False

    try:
        text = path.read_text() if path.is_file() else ""
    except OSError as exc:
        info(f"Warning: could not read {path}: {exc}")
        return False

    if _profile_has_marker(text):
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    if not text:
        new_text = block
    elif text.endswith("\n"):
        new_text = f"{text}\n{block}"
    else:
        new_text = f"{text}\n\n{block}"
    try:
        path.write_text(new_text)
    except OSError as exc:
        info(f"Warning: could not update {path}: {exc}")
        return False
    return True


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


def _path_contains(path_value: str, entry: str, separator: str | None = None) -> bool:
    """Return whether *entry* is already present in a PATH-like string."""
    separator = separator or os.pathsep
    windows_style = separator == ";"

    def normalize(value: str) -> str:
        normalized = os.path.normpath(os.path.expandvars(os.path.expanduser(value)))
        if windows_style:
            normalized = normalized.replace("\\", "/").lower()
        else:
            normalized = os.path.normcase(normalized)
        return normalized.rstrip("/")

    expected = normalize(entry)
    for part in path_value.split(separator):
        if not part:
            continue
        if normalize(part) == expected:
            return True
    return False


def _prepend_process_path(path: Path) -> None:
    """Make the proxy visible to child processes spawned by this installer."""
    path_str = str(path)
    current = os.environ.get("PATH", "")
    if _path_contains(current, path_str):
        return
    os.environ["PATH"] = path_str + (os.pathsep + current if current else "")


def _ensure_windows_user_path(path: Path) -> bool:
    """Persist the harness bin directory in the Windows user PATH."""
    path_str = str(path)
    if dry_run():
        info(f"would add {path_str} to the Windows user PATH")
        return False

    try:
        import winreg
    except ImportError:
        info("Warning: could not update Windows user PATH: winreg is unavailable")
        return False

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
                current, value_type = "", reg_expand_sz

            if _path_contains(str(current), path_str, separator=";"):
                return False

            new_path = path_str + (";" + str(current) if current else "")
            if value_type not in (reg_expand_sz, reg_sz):
                value_type = reg_expand_sz
            set_value_ex(key, "Path", 0, value_type, new_path)
    except OSError as exc:
        info(f"Warning: could not update Windows user PATH: {exc}")
        return False

    try:
        import ctypes

        windll = getattr(ctypes, "windll", None)
        if windll is not None:
            windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0, 5000, None)
    except Exception:
        pass

    return True


def _ensure_codex_proxy_on_path(path: Path) -> None:
    """Persist and activate the harness bin directory for Codex proxy lookup."""
    changed_profiles: list[Path] = []

    for profile in _posix_shell_profiles():
        if _ensure_profile_block(profile, _POSIX_PATH_BLOCK):
            changed_profiles.append(profile)

    for profile in _powershell_profiles():
        if _ensure_profile_block(profile, _POWERSHELL_PATH_BLOCK):
            changed_profiles.append(profile)

    if os.name == "nt":
        _ensure_windows_user_path(path)

    _prepend_process_path(path)

    if changed_profiles:
        joined = ", ".join(str(p) for p in changed_profiles)
        info(f"Added Arize harness bin to PATH in: {joined}")
        info("Open a new shell, or source your profile, for parent shells to pick up the PATH update.")
    elif not dry_run():
        info("Arize harness bin is already configured on PATH for supported shell profiles.")


def _remove_codex_proxy_path_blocks() -> None:
    """Remove shell profile PATH blocks written by the Codex installer."""
    profiles = list(dict.fromkeys(_posix_shell_profiles() + _powershell_profiles()))
    removed = [profile for profile in profiles if _remove_profile_block(profile)]
    if removed:
        joined = ", ".join(str(p) for p in removed)
        info(f"Removed Arize harness bin PATH block from: {joined}")


def _is_our_codex_proxy_shim(path: Path) -> bool:
    """Return True only if *path* exists and is an Arize-managed codex shim."""
    if not path.is_file():
        return False
    try:
        text = path.read_text()
        return "arize-codex-proxy" in text and "Arize Codex proxy shim" in text
    except OSError:
        return False


def _write_codex_proxy_shim(path: Path, proxy_cmd: Path) -> None:
    """Create the codex proxy shim at *path* pointing to *proxy_cmd*.

    Honors ``dry_run()`` — logs intent without writing.
    """
    if dry_run():
        info(f"would write codex proxy shim at {path}")
        return

    # Never overwrite a file that isn't ours
    if path.is_file() and not _is_our_codex_proxy_shim(path):
        info(f"Skipping codex proxy shim — {path} exists and is not ours")
        return

    if path.suffix.lower() == ".cmd":
        content = "@echo off\r\n" "REM Arize Codex proxy shim\r\n" f'"{proxy_cmd}" %*\r\n'
    else:
        shell_proxy_cmd = str(proxy_cmd).replace("\\", "/")
        content = "#!/bin/sh\n" "# Arize Codex proxy shim\n" f"exec '{shell_proxy_cmd}' \"$@\"\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)

    if path.suffix.lower() != ".cmd":
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


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


def _codex_proxy_path_status(shim_path: Path) -> tuple[str, "str | None"]:
    """Check whether the shim is the ``codex`` that the user's shell will run.

    Returns one of:
    - ``("active", "<resolved_shim_path>")``
    - ``("shadowed", "<resolved_other_path>")``
    - ``("missing", None)``
    """
    resolved = shutil.which("codex")
    if resolved is None:
        return ("missing", None)

    shim_real = os.path.realpath(str(shim_path))
    which_real = os.path.realpath(resolved)

    if shim_real == which_real and _is_our_codex_proxy_shim(shim_path):
        return ("active", which_real)
    return ("shadowed", which_real)


# ---------------------------------------------------------------------------
# Install / Uninstall
# ---------------------------------------------------------------------------


def install_noninteractive(
    *,
    target: str,
    credentials: dict,
    project_name: str,
    user_id: str = "",
    with_skills: bool = False,
    logging_block: "dict | None" = None,
) -> None:
    """Install with no prompts. All decisions made by caller."""
    # 1. Ensure shared runtime directories
    ensure_shared_runtime()

    # 2. Write harness entry
    config = load_config(str(CONFIG_FILE))
    existing_entry = get_value(config, f"harnesses.{HARNESS_NAME}")
    collector = {"host": "127.0.0.1", "port": 4318}

    if existing_entry:
        existing_collector = existing_entry.get("collector")
        merge_harness_entry(HARNESS_NAME, project_name, collector=existing_collector or collector)
    else:
        if not dry_run():
            write_config(
                target=target,
                credentials=credentials,
                harness_name=HARNESS_NAME,
                project_name=project_name,
                user_id=user_id,
                collector=collector,
            )
        else:
            info("would write config.yaml with backend credentials")

    # Logging: use caller-supplied block, or default if absent from config.
    config = load_config(str(CONFIG_FILE))
    if (config.get("logging") if config else None) is None:
        effective_logging = (
            logging_block
            if logging_block is not None
            else {
                "prompts": True,
                "tool_details": True,
                "tool_content": True,
            }
        )
        write_logging_config(effective_logging)

    # 3. Ensure codex config dir exists
    if not dry_run():
        CODEX_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    else:
        info(f"would create {CODEX_CONFIG_DIR}")

    # 4. Write env file
    _write_env_file(CODEX_ENV_FILE, user_id=user_id)

    # 5. Update codex config.toml — collector port from new path
    config = load_config(str(CONFIG_FILE))
    collector_port = get_value(config, f"harnesses.{HARNESS_NAME}.collector.port") or 4318
    otel_endpoint = f"http://127.0.0.1:{collector_port}/v1/logs"
    notify_cmd = str(venv_bin(NOTIFY_BIN_NAME))
    _codex_toml_add(CODEX_CONFIG_FILE, notify_cmd, otel_endpoint)
    info(f"Updated TOML config: {CODEX_CONFIG_FILE}")

    # 6. Start buffer service
    status, _, _ = buffer_status()
    if status == "running":
        info("Buffer service already running — skipping start")
    elif not dry_run():
        ok = buffer_start()
        if ok:
            info("Buffer service started")
        else:
            info("Warning: buffer service failed to start (you can start it later)")
    else:
        info("would start buffer service")

    # 7. Write codex proxy shim
    shim_path = _codex_proxy_shim_path()
    for path in _codex_proxy_shim_paths():
        _write_codex_proxy_shim(path, venv_bin("arize-codex-proxy"))
    if dry_run() or any(_is_our_codex_proxy_shim(path) for path in _codex_proxy_shim_paths()):
        _ensure_codex_proxy_on_path(BIN_DIR)
    else:
        info(f"Codex proxy PATH not updated because {shim_path} is not an Arize-managed shim.")

    status, resolved = _codex_proxy_path_status(shim_path)
    if status == "active":
        info(f"Codex proxy active for codex exec (resolves to {resolved})")
    elif status == "shadowed":
        info(
            f"Codex proxy shim installed at {shim_path}, but PATH resolves codex"
            f" to {resolved}. Open a new shell after install so the managed PATH"
            " update can take effect."
        )
    else:
        info(f"Codex proxy shim installed at {shim_path}; open a new shell to activate codex exec tracing.")

    # 8. Symlink skills
    if with_skills:
        symlink_skills(HARNESS_NAME)
        info("Symlinked skills")

    info("Codex tracing installed successfully")


def uninstall_noninteractive() -> None:
    """Uninstall with no prompts."""
    # 1. Stop buffer service
    if not dry_run():
        buffer_stop()
        info("Stopped buffer service")
    else:
        info("would stop buffer service")

    # 2. Revert codex config.toml
    config = load_config(str(CONFIG_FILE))
    collector_port = get_value(config, f"harnesses.{HARNESS_NAME}.collector.port") or 4318
    otel_endpoint = f"http://127.0.0.1:{collector_port}/v1/logs"
    notify_cmd = str(venv_bin(NOTIFY_BIN_NAME))
    _codex_toml_remove(CODEX_CONFIG_FILE, notify_cmd, otel_endpoint)
    info(f"Reverted TOML config: {CODEX_CONFIG_FILE}")

    # 3. Remove codex proxy shim
    for path in _codex_proxy_shim_paths():
        _remove_codex_proxy_shim(path)

    # 4. Remove shell profile PATH blocks
    _remove_codex_proxy_path_blocks()

    # 5. Remove env file if it's ours
    if CODEX_ENV_FILE.is_file():
        if _is_our_env_file(CODEX_ENV_FILE):
            if dry_run():
                info(f"would remove {CODEX_ENV_FILE}")
            else:
                CODEX_ENV_FILE.unlink()
                info(f"Removed env file: {CODEX_ENV_FILE}")
        else:
            info(f"Skipping {CODEX_ENV_FILE} — does not look like our file")

    # 6. Remove harness entry
    remove_harness_entry(HARNESS_NAME)
    info("Removed codex harness entry from config.yaml")

    # 7. Unlink skills
    unlink_skills(HARNESS_NAME)
    info("Unlinked skills")

    info("Codex tracing uninstalled")


def install(with_skills: bool = False) -> None:
    """Install codex tracing harness."""
    if not ensure_harness_installed(DISPLAY_NAME, home_subdir=HARNESS_HOME, bin_name=HARNESS_BIN):
        info("Aborted.")
        return

    config = load_config(str(CONFIG_FILE))
    existing_entry = get_value(config, f"harnesses.{HARNESS_NAME}")

    project_name = prompt_project_name("codex")

    if existing_entry:
        info(f"Reusing existing backend: {existing_entry.get('target')}")
        target = existing_entry.get("target", "phoenix")
        credentials = {
            "endpoint": existing_entry.get("endpoint", ""),
            "api_key": existing_entry.get("api_key", ""),
        }
        if existing_entry.get("space_id"):
            credentials["space_id"] = existing_entry["space_id"]
        user_id = get_value(config, "user_id") or ""
    else:
        existing_harnesses = config.get("harnesses", {}) if config else {}
        target, credentials = prompt_backend(existing_harnesses=existing_harnesses)
        user_id = prompt_user_id()

    logging_block = None
    if (config.get("logging") if config else None) is None:
        logging_block = prompt_content_logging()
    else:
        info("Using existing logging settings from config.yaml")

    install_noninteractive(
        target=target,
        credentials=credentials,
        project_name=project_name,
        user_id=user_id,
        with_skills=with_skills,
        logging_block=logging_block,
    )


def uninstall() -> None:
    """Uninstall codex tracing harness."""
    uninstall_noninteractive()


# ---------------------------------------------------------------------------
# CLI dispatch
# ---------------------------------------------------------------------------


def cli_main(argv: list[str] | None = None) -> None:
    """Parse argv and dispatch to install/uninstall."""
    if argv is None:
        argv = sys.argv
    if len(argv) < 2 or argv[1] not in ("install", "uninstall"):
        print(f"usage: {argv[0]} <install|uninstall> [--with-skills]", file=sys.stderr)
        sys.exit(1)

    action = argv[1]
    flags = argv[2:]

    if action == "install":
        install(with_skills="--with-skills" in flags)
    else:
        uninstall()


if __name__ == "__main__":
    try:
        cli_main()
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
        sys.exit(1)
