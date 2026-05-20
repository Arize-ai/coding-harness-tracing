#!/usr/bin/env python3
"""Codex harness install / uninstall module.

Self-contained module that handles:
- Writing ~/.codex/arize-env.sh (env file)
- Updating ~/.codex/config.toml (notify + five hook entry points)
- Managing the shared config.yaml harness entry
- Symlinking skills
- Migrating legacy v1 installs via tracing.codex.install_legacy
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from core.config import get_value, load_config
from core.setup import BIN_DIR  # noqa: F401 — kept for tests that monkeypatch this attribute
from core.setup import (
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
    SESSION_BIN_NAME,
    STOP_BIN_NAME,
    TOOL_BIN_NAME,
)
from tracing.codex.install_legacy import cleanup_legacy_install

# Try to import tomllib (3.11+), then tomli, then fall back to None
_tomllib = None
try:
    import tomllib as _tomllib  # type: ignore[no-redef]
except ImportError:
    try:
        import tomli as _tomllib  # type: ignore[no-redef]
    except ImportError:
        pass


# Hook events written by the installer.
_HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PermissionRequest",
    "Stop",
)

# v1 OTLP endpoint pattern (used by `_codex_toml_apply` to strip a stale v1
# otel exporter block on upgrade — buffer service is gone in v2). Matches
# any 127.0.0.1 endpoint ending in /v1/logs to catch installs where the user
# changed the buffer port via config.yaml.
_V1_OTEL_ENDPOINT_RE = re.compile(r"^https?://127\.0\.0\.1:\d+/v1/logs$")


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
    # Pass 1: simple scalars and arrays of scalars.
    for key, val in data.items():
        if isinstance(val, dict) or _is_table_array(val):
            continue
        _toml_write_value(key, val, lines)

    # Pass 2: arrays-of-tables → emit [[prefix.key]] for each element.
    for key, val in data.items():
        if not _is_table_array(val):
            continue
        section_path = prefix + [key]
        header = f"[[{'.'.join(_toml_key(k) for k in section_path)}]]"
        for table in val:
            if lines and lines[-1] != "":
                lines.append("")
            lines.append(header)
            _toml_write_table_body(table, lines)

    # Pass 3: nested dict sections.
    for key, val in data.items():
        if not isinstance(val, dict):
            continue
        section_path = prefix + [key]
        # Emit [section] header only when there are direct scalars to anchor
        # (or the table is empty). If all children are dicts/table-arrays we
        # skip the header and let those nested writers emit their own headers.
        has_scalars = any(not isinstance(v, dict) and not _is_table_array(v) for v in val.values())
        if has_scalars or not val:
            if lines and lines[-1] != "":
                lines.append("")
            lines.append(f"[{'.'.join(_toml_key(k) for k in section_path)}]")
        _toml_write_section(val, section_path, lines)


def _is_table_array(val: object) -> bool:
    """Return True if val is a list whose elements are all dicts."""
    return isinstance(val, list) and len(val) > 0 and all(isinstance(v, dict) for v in val)


def _toml_write_table_body(table: dict, lines: list[str]) -> None:
    """Write a dict as the body of a ``[[section]]`` entry.

    Nested dicts render as inline tables; arrays of dicts render as arrays of
    inline tables. Scalars and arrays of scalars use the standard writer.
    """
    for key, val in table.items():
        if isinstance(val, dict):
            lines.append(f"{_toml_key(key)} = {_inline_table(val)}")
        elif _is_table_array(val):
            elems = ", ".join(_inline_table(d) for d in val)
            lines.append(f"{_toml_key(key)} = [{elems}]")
        else:
            _toml_write_value(key, val, lines)


def _inline_table(table: dict) -> str:
    """Render a dict as a TOML inline table: ``{ k = v, k2 = v2 }``."""
    parts: list[str] = []
    for k, v in table.items():
        kk = _toml_key(k)
        if isinstance(v, dict):
            parts.append(f"{kk} = {_inline_table(v)}")
        elif isinstance(v, bool):
            parts.append(f"{kk} = {'true' if v else 'false'}")
        elif isinstance(v, int):
            parts.append(f"{kk} = {v}")
        elif isinstance(v, list):
            if _is_table_array(v):
                items = ", ".join(_inline_table(d) for d in v)
            else:
                items = ", ".join(_toml_string_literal(item) for item in v)
            parts.append(f"{kk} = [{items}]")
        else:
            parts.append(f"{kk} = {_toml_string_literal(v)}")
    return "{ " + ", ".join(parts) + " }"


def _toml_write_value(key: str, val: object, lines: list[str]) -> None:
    """Write a single TOML key-value pair (scalars and arrays of scalars only)."""
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


def _hook_entry_for(cmd: str) -> dict:
    """Build the dict body for one ``[[hooks.<Event>]]`` array element."""
    return {"hooks": [{"type": "command", "command": cmd, "timeout": 30}]}


def _entry_targets_cmd(entry: object, cmd: str) -> bool:
    """Return True if *entry* is a hook-array element whose inner ``hooks``
    list contains a command equal to *cmd*.
    """
    if not isinstance(entry, dict):
        return False
    inner = entry.get("hooks")
    if not isinstance(inner, list):
        return False
    return any(isinstance(h, dict) and h.get("command") == cmd for h in inner)


def _codex_toml_apply(
    path: Path,
    notify_cmd: str,
    session_cmd: str,
    tool_cmd: str,
    stop_cmd: str,
) -> None:
    """Write the v2 hooks-based layout to ~/.codex/config.toml. Idempotent.

    Also strips any stale v1 ``[otel.exporter.otlp-http]`` block pointing at
    the local buffer service (the buffer service is gone in v2).
    """
    if dry_run():
        info(f"would update {path} with notify + 5 hook entries")
        return

    data = _toml_load(path)

    # Ensure notify list contains our notify_cmd exactly once.
    existing_notify = data.get("notify", [])
    if not isinstance(existing_notify, list):
        existing_notify = [existing_notify] if existing_notify else []
    if notify_cmd not in existing_notify:
        existing_notify.append(notify_cmd)
    data["notify"] = existing_notify

    # Strip v1 local-buffer otel exporter block if present.
    otel = data.get("otel")
    if isinstance(otel, dict):
        exporter = otel.get("exporter")
        if isinstance(exporter, dict):
            otlp = exporter.get("otlp-http")
            if isinstance(otlp, dict):
                endpoint = otlp.get("endpoint", "")
                if isinstance(endpoint, str) and _V1_OTEL_ENDPOINT_RE.match(endpoint):
                    del exporter["otlp-http"]
                    if not exporter:
                        del otel["exporter"]
                    if not otel:
                        del data["otel"]

    # Write [[hooks.<Event>]] entries. Replace any prior entry pointing at our cmd.
    hooks = data.setdefault("hooks", {})
    hook_specs = (
        ("SessionStart", session_cmd),
        ("UserPromptSubmit", session_cmd),
        ("PreToolUse", tool_cmd),
        ("PostToolUse", tool_cmd),
        ("PermissionRequest", tool_cmd),
        ("Stop", stop_cmd),
    )
    for event_name, cmd in hook_specs:
        existing = hooks.get(event_name, [])
        if not isinstance(existing, list):
            existing = []
        new_entry = _hook_entry_for(cmd)
        replaced = False
        for i, entry in enumerate(existing):
            if _entry_targets_cmd(entry, cmd):
                existing[i] = new_entry
                replaced = True
                break
        if not replaced:
            existing.append(new_entry)
        hooks[event_name] = existing

    path.parent.mkdir(parents=True, exist_ok=True)
    _toml_write(data, path)


def _codex_toml_remove(path: Path, notify_cmd: str, otel_endpoint: str) -> None:
    """Remove our notify command and otel exporter from codex config.toml. Idempotent.

    Kept for legacy v1 uninstall compatibility; not used by the v2 install path.
    """
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
            if not data["otel"]["exporter"]:
                del data["otel"]["exporter"]
            if not data["otel"]:
                del data["otel"]

    if changed:
        _toml_write(data, path)


def _codex_toml_remove_v2(path: Path, notify_cmd: str, hook_cmds: list[str]) -> None:
    """Remove v2 notify entry and all hook entries pointing at our commands. Idempotent."""
    if not path.is_file():
        return

    if dry_run():
        info(f"would revert {path}: remove notify={notify_cmd} and hook entries")
        return

    data = _toml_load(path)
    changed = False

    # Notify
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

    # Hooks
    hooks = data.get("hooks")
    if isinstance(hooks, dict):
        for event in _HOOK_EVENTS:
            existing = hooks.get(event)
            if not isinstance(existing, list):
                continue
            kept = [entry for entry in existing if not any(_entry_targets_cmd(entry, cmd) for cmd in hook_cmds)]
            if len(kept) != len(existing):
                changed = True
                if kept:
                    hooks[event] = kept
                else:
                    del hooks[event]
        if not hooks:
            del data["hooks"]
            changed = True

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
# Install / Uninstall
# ---------------------------------------------------------------------------


def install(with_skills: bool = False) -> None:
    """Install codex tracing harness (hooks-based v2 layout)."""
    if not ensure_harness_installed(DISPLAY_NAME, home_subdir=HARNESS_HOME, bin_name=HARNESS_BIN):
        info("Aborted.")
        return

    # 1. Migrate any v1 artifacts (idempotent; no-op on fresh installs).
    cleanup_legacy_install()

    # 2. Shared runtime + harness entry.
    ensure_shared_runtime()
    config = load_config(str(CONFIG_FILE))
    existing_entry = get_value(config, f"harnesses.{HARNESS_NAME}")
    project_name = prompt_project_name("codex")

    if existing_entry:
        info(f"Reusing existing backend: {existing_entry.get('target')}")
        merge_harness_entry(HARNESS_NAME, project_name)
        user_id = get_value(config, "user_id") or ""
    else:
        existing_harnesses = config.get("harnesses", {}) if config else {}
        target, credentials = prompt_backend(existing_harnesses=existing_harnesses)
        user_id = prompt_user_id()
        if not dry_run():
            write_config(
                target=target,
                credentials=credentials,
                harness_name=HARNESS_NAME,
                project_name=project_name,
                user_id=user_id,
            )
        else:
            info("would write config.yaml with backend credentials")

    # Logging settings are global. Prompt only if no `logging:` block exists yet.
    if (config.get("logging") if config else None) is None:
        write_logging_config(prompt_content_logging())
    else:
        info("Using existing logging settings from config.yaml")

    # 3. Codex config dir + env file.
    if not dry_run():
        CODEX_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    else:
        info(f"would create {CODEX_CONFIG_DIR}")
    _write_env_file(CODEX_ENV_FILE, user_id=user_id)

    # 4. Write the hooks-based TOML layout.
    notify_cmd = str(venv_bin(NOTIFY_BIN_NAME))
    session_cmd = str(venv_bin(SESSION_BIN_NAME))
    tool_cmd = str(venv_bin(TOOL_BIN_NAME))
    stop_cmd = str(venv_bin(STOP_BIN_NAME))
    _codex_toml_apply(CODEX_CONFIG_FILE, notify_cmd, session_cmd, tool_cmd, stop_cmd)
    info(f"Updated TOML config: {CODEX_CONFIG_FILE}")

    # 5. Skills.
    if with_skills:
        symlink_skills(HARNESS_NAME)
        info("Symlinked skills")

    # 6. Trust prompt message.
    info("")
    info("Codex tracing installed.")
    info("")
    info("  One-time setup: open codex, run `/hooks`, and approve the")
    info("  arize-hook-codex-* entries. Codex requires explicit trust")
    info("  before non-managed hooks fire.")


def uninstall() -> None:
    """Uninstall codex tracing harness."""
    # 1. Clean up any lingering v1 artifacts first (no-op if absent).
    cleanup_legacy_install()

    # 2. Revert TOML — remove our notify entry and hook entries.
    notify_cmd = str(venv_bin(NOTIFY_BIN_NAME))
    session_cmd = str(venv_bin(SESSION_BIN_NAME))
    tool_cmd = str(venv_bin(TOOL_BIN_NAME))
    stop_cmd = str(venv_bin(STOP_BIN_NAME))
    _codex_toml_remove_v2(CODEX_CONFIG_FILE, notify_cmd, [session_cmd, tool_cmd, stop_cmd])
    info(f"Reverted TOML config: {CODEX_CONFIG_FILE}")

    # 3. Remove env file if ours.
    if CODEX_ENV_FILE.is_file():
        if _is_our_env_file(CODEX_ENV_FILE):
            if dry_run():
                info(f"would remove {CODEX_ENV_FILE}")
            else:
                CODEX_ENV_FILE.unlink()
                info(f"Removed env file: {CODEX_ENV_FILE}")
        else:
            info(f"Skipping {CODEX_ENV_FILE} — does not look like our file")

    # 4. Remove harness entry + unlink skills.
    remove_harness_entry(HARNESS_NAME)
    info("Removed codex harness entry from config.yaml")
    unlink_skills(HARNESS_NAME)
    info("Unlinked skills")
    info("Codex tracing uninstalled")


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
