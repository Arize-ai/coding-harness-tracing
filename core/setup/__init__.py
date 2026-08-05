#!/usr/bin/env python3
"""Shared setup utilities for all harness setup wizards."""

from __future__ import annotations

import os
import shutil
import sys
from getpass import getpass
from pathlib import Path
from typing import Optional

from core.config import delete_value, load_config, save_config, set_value

# ---------------------------------------------------------------------------
# Shared path constants
# ---------------------------------------------------------------------------

INSTALL_DIR = Path.home() / ".arize" / "harness"
VENV_DIR = INSTALL_DIR / "venv"
CONFIG_FILE = INSTALL_DIR / "config.json"
BIN_DIR = INSTALL_DIR / "bin"
RUN_DIR = INSTALL_DIR / "run"
LOG_DIR = INSTALL_DIR / "logs"
STATE_DIR = INSTALL_DIR / "state"

# Legacy collector artefacts to clean up
_LEGACY_ARTEFACTS = ("bin/arize-collector", "run/collector.pid", "logs/collector.log")


# ---------------------------------------------------------------------------
# Output helpers (unchanged)
# ---------------------------------------------------------------------------


def print_color(msg: str, color: str = "") -> None:
    """Print with ANSI color. No-op on Windows if terminal doesn't support it."""
    codes = {
        "green": "\033[0;32m",
        "yellow": "\033[1;33m",
        "blue": "\033[0;34m",
        "red": "\033[0;31m",
    }
    nc = "\033[0m"

    use_color = color in codes and sys.stdout.isatty() and os.name != "nt"
    if use_color:
        print(f"{codes[color]}{msg}{nc}")
    else:
        print(msg)


def info(msg: str) -> None:
    """Print an info message with [arize] prefix."""
    if sys.stdout.isatty() and os.name != "nt":
        print(f"\033[0;32m[arize]\033[0m {msg}")
    else:
        print(f"[arize] {msg}")


def err(msg: str) -> None:
    """Print an error message with [arize] prefix to stderr."""
    if sys.stderr.isatty() and os.name != "nt":
        sys.stderr.write(f"\033[0;31m[arize]\033[0m {msg}\n")
    else:
        sys.stderr.write(f"[arize] {msg}\n")


# ---------------------------------------------------------------------------
# Harness presence check (soft signal)
# ---------------------------------------------------------------------------


def is_harness_installed(
    home_subdir: Optional[str] = None,
    bin_name: Optional[str] = None,
) -> bool:
    """True if ``~/<home_subdir>`` exists OR ``<bin_name>`` is on PATH.

    ``Path.home()`` is resolved at call time so tests can monkeypatch it.
    """
    if home_subdir and (Path.home() / home_subdir).exists():
        return True
    if bin_name and shutil.which(bin_name):
        return True
    return False


def ensure_harness_installed(
    display_name: str,
    home_subdir: Optional[str] = None,
    bin_name: Optional[str] = None,
) -> bool:
    """Soft check that the harness appears installed on this machine.

    If yes, return ``True`` silently.  If no, warn and either prompt the user
    (interactive) or proceed with a note (non-interactive).  Return ``True`` to
    proceed with install, ``False`` to abort.
    """
    if is_harness_installed(home_subdir=home_subdir, bin_name=bin_name):
        return True

    print_color(f"warning: {display_name} does not appear to be installed", "yellow")
    checks = []
    if home_subdir:
        checks.append(str(Path.home() / home_subdir))
    if bin_name:
        checks.append(f"'{bin_name}' on PATH")
    if checks:
        info(f"  (not found: {', '.join(checks)})")

    if non_interactive() or not sys.stdout.isatty():
        info("  non-interactive — proceeding anyway")
        return True

    try:
        reply = input(f"Install tracing for {display_name} anyway? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return reply in ("y", "yes")


# ---------------------------------------------------------------------------
# Interactive prompts (unchanged)
# ---------------------------------------------------------------------------


def prompt_backend(
    existing_harnesses: dict | None = None,
) -> tuple[str, dict]:
    """Interactive backend selection with optional copy-from.

    existing_harnesses is the value of cfg['harnesses'] (or None).  After the
    user picks a target ("phoenix" or "arize"), find entries in
    existing_harnesses whose ``target`` matches.  If any exist, offer a menu
    to copy credentials from one.

    Returns (target, credentials).  credentials keys:
      phoenix: {"endpoint", "api_key"}
      arize:   {"endpoint", "api_key", "space_id"}
    """
    if non_interactive():
        return _backend_from_env()

    print("Which backend do you want to use?")
    print("")
    print("  1) Phoenix (self-hosted)")
    print("  2) Arize AX (cloud)")
    print("")
    choice = input("Enter choice [1/2]: ").strip()

    if choice in ("1", "phoenix", "Phoenix", ""):
        target = "phoenix"
    elif choice in ("2", "arize", "ax", "AX"):
        target = "arize"
    else:
        err("Invalid choice. Run setup again.")
        sys.exit(1)

    # --- copy-from logic ---
    copied = _try_copy_from(target, existing_harnesses)
    if copied is not None:
        return (target, copied)

    # --- fresh credential prompts ---
    if target == "phoenix":
        print("")
        phoenix_endpoint = input("Phoenix endpoint [http://localhost:6006]: ").strip()
        if not phoenix_endpoint:
            phoenix_endpoint = "http://localhost:6006"
        api_key = getpass("Phoenix API Key (blank for no auth): ").strip()
        return ("phoenix", {"endpoint": phoenix_endpoint, "api_key": api_key})

    # arize
    print("")
    api_key = getpass("Arize API Key: ").strip()
    space_id = input("Arize Space ID: ").strip()

    if not api_key or not space_id:
        err("API key and Space ID are required for Arize AX")
        sys.exit(1)

    print("")
    if sys.stdout.isatty() and os.name != "nt":
        print("\033[1;33mOTLP Endpoint\033[0m (for hosted Arize instances, leave blank for default):")
    else:
        print("OTLP Endpoint (for hosted Arize instances, leave blank for default):")
    otlp_endpoint = input("OTLP Endpoint [otlp.arize.com:443]: ").strip()
    if not otlp_endpoint:
        otlp_endpoint = "otlp.arize.com:443"

    return (
        "arize",
        {
            "endpoint": otlp_endpoint,
            "api_key": api_key,
            "space_id": space_id,
        },
    )


def _backend_from_env() -> tuple[str, dict]:
    """Resolve backend + credentials from the environment, for non-interactive installs.

    Backend selection is explicit via ``ARIZE_BACKEND``, otherwise inferred: a
    space ID means Arize AX, a Phoenix endpoint means Phoenix. Nothing is
    inferred from the API key alone — both backends use one.

    Exits with an actionable message when a required value is missing; this
    path must never fall back to a prompt, because there is nobody to answer it.
    """
    target = _env("ARIZE_BACKEND").lower()
    if target in ("ax", "arize"):
        target = "arize"
    elif target and target != "phoenix":
        err(f"Unknown ARIZE_BACKEND '{target}' — use 'arize' or 'phoenix'.")
        sys.exit(1)

    if not target:
        if _env("ARIZE_SPACE_ID"):
            target = "arize"
        elif _env("PHOENIX_ENDPOINT"):
            target = "phoenix"
        elif _env("ARIZE_API_KEY"):
            # Both backends take an API key, so a key on its own is ambiguous.
            err(
                "Found an API key but cannot tell which backend it is for. Add "
                "ARIZE_SPACE_ID for Arize AX, or PHOENIX_ENDPOINT for Phoenix."
            )
            sys.exit(1)
        else:
            err(
                "No credentials found. Set ARIZE_API_KEY and ARIZE_SPACE_ID for Arize AX "
                "(in the environment or a .env file), or PHOENIX_ENDPOINT for Phoenix."
            )
            sys.exit(1)

    if target == "phoenix":
        endpoint = _env("PHOENIX_ENDPOINT") or "http://localhost:6006"
        info(f"Backend: Phoenix at {endpoint}")
        return ("phoenix", {"endpoint": endpoint, "api_key": _env("PHOENIX_API_KEY")})

    # Arize AX. Read the key but never echo it — only report that it was found.
    api_key = _require_env("ARIZE_API_KEY", "An Arize API key")
    space_id = _require_env("ARIZE_SPACE_ID", "An Arize space ID")
    endpoint = _env("ARIZE_OTLP_ENDPOINT") or "otlp.arize.com:443"
    info(f"Backend: Arize AX at {endpoint} (space {space_id}, API key from ARIZE_API_KEY)")
    return ("arize", {"endpoint": endpoint, "api_key": api_key, "space_id": space_id})


def _try_copy_from(target: str, existing_harnesses: dict | None) -> dict | None:
    """Show copy-from menu if matching harnesses exist.  Returns credentials or None."""
    if not existing_harnesses:
        return None

    # Required fields per target
    if target == "phoenix":
        # api_key must be present but may be empty string
        def _valid(entry: dict) -> bool:
            return "endpoint" in entry and "api_key" in entry

    else:
        _required_arize = {"endpoint", "api_key", "space_id"}

        def _valid(entry: dict) -> bool:
            return all(k in entry and entry[k] for k in _required_arize)

    matches: list[tuple[str, dict]] = []
    for name, entry in existing_harnesses.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("target") != target:
            continue
        if not _valid(entry):
            continue
        matches.append((name, entry))

    if not matches:
        return None

    # Display menu
    target_label = "Phoenix" if target == "phoenix" else "Arize AX"
    print("")
    print(f"Found existing harnesses using {target_label}:")
    for i, (name, entry) in enumerate(matches, 1):
        detail = f"endpoint: {entry.get('endpoint', '')}"
        if target == "arize":
            detail += f", space_id: {entry.get('space_id', '')}"
        print(f"  {i}) {name}  ({detail})")
    last = len(matches) + 1
    print(f"  {last}) Enter new credentials")
    print("")

    attempts = 0
    while attempts < 2:
        raw = input(f"Copy from [1-{last}]: ").strip()
        try:
            idx = int(raw)
            if idx == last:
                return None  # fall through to fresh prompts
            if 1 <= idx <= len(matches):
                name, entry = matches[idx - 1]
                info(f"Reusing {target} credentials from '{name}'.")
                creds: dict = {"endpoint": entry["endpoint"], "api_key": entry["api_key"]}
                if target == "arize":
                    creds["space_id"] = entry["space_id"]
                return creds
        except (ValueError, TypeError):
            pass
        attempts += 1
        if attempts < 2:
            print("Invalid input, please try again.")

    # Two invalid attempts — default to new credentials
    return None


def prompt_project_name(default: str) -> str:
    """Prompt for project name. Returns default if blank."""
    if non_interactive():
        name = _env("ARIZE_PROJECT_NAME") or default
        info(f"Project name: {name}")
        return name

    print("")
    name = input(f"Project name [{default}]: ").strip()
    return name if name else default


def prompt_content_logging() -> dict:
    """Prompt for content logging settings. Returns the dict to write under `logging:`.

    All three default to True to match the kit's existing capture-everything
    behavior. Users opt out per category.
    """
    if non_interactive():
        block = {
            "prompts": env_flag("ARIZE_LOG_PROMPTS"),
            "tool_details": env_flag("ARIZE_LOG_TOOL_DETAILS"),
            "tool_content": env_flag("ARIZE_LOG_TOOL_CONTENT"),
        }
        enabled = ", ".join(f"{k}={'on' if v else 'off'}" for k, v in block.items())
        info(f"Content logging: {enabled}")
        return block

    print("")
    if sys.stdout.isatty() and os.name != "nt":
        print("\033[1;33mSecurity:\033[0m Traces can contain sensitive data — credentials, PII, file contents.")
    else:
        print("Security: Traces can contain sensitive data — credentials, PII, file contents.")
    print("All content is logged by default. Opt out per category to match your security needs.")
    print("")

    log_prompts = input("  Log user prompts? [Y/n]: ").strip().lower()
    log_tool_details = input("  Log what tools were asked to do (commands, file paths, URLs)? [Y/n]: ").strip().lower()
    log_tool_content = input("  Log what tools returned (file contents, command output)? [Y/n]: ").strip().lower()

    return {
        "prompts": log_prompts not in ("n", "no"),
        "tool_details": log_tool_details not in ("n", "no"),
        "tool_content": log_tool_content not in ("n", "no"),
    }


def write_logging_config(logging_block: dict, config_path: str | None = None) -> None:
    """Merge a logging block into the top-level `logging:` key in config.json."""
    config = load_config(config_path)
    if not config:
        config = {}
    set_value(config, "logging", logging_block)
    if dry_run():
        info("would write logging block to config.json")
        return
    save_config(config, config_path)


def prompt_user_id() -> str:
    """Optional user ID prompt. Returns "" if skipped."""
    if non_interactive():
        return _env("ARIZE_USER_ID")

    print("")
    if sys.stdout.isatty() and os.name != "nt":
        print("\033[0;34mOptional:\033[0m Set a user ID to identify your spans (useful for teams).")
    else:
        print("Optional: Set a user ID to identify your spans (useful for teams).")
    user_id = input("User ID (leave blank to skip): ").strip()
    return user_id


def write_config(
    target: str,
    credentials: dict,
    harness_name: str,
    project_name: str,
    user_id: str = "",
    collector: dict | None = None,
    config_path: Optional[str] = None,
) -> None:
    """Write or merge config.json with a fully-flattened harnesses.<name> entry.

    Writes harnesses.<harness_name>.{project_name, target, endpoint, api_key,
    [space_id], [collector]}.  If user_id is non-empty, sets top-level user_id.
    Read-merge-write: preserves other harnesses and top-level keys.
    """
    config = load_config(config_path)

    if not config:
        config = {"harnesses": {}}

    # Strip legacy top-level keys if they leaked in from a prior save
    config.pop("backend", None)
    config.pop("collector", None)

    # Build the harness entry
    entry: dict = {
        "project_name": project_name,
        "target": target,
        "endpoint": credentials.get("endpoint", ""),
        "api_key": credentials.get("api_key", ""),
    }
    if target == "arize" and "space_id" in credentials:
        entry["space_id"] = credentials["space_id"]

    if collector is not None:
        entry["collector"] = collector

    set_value(config, f"harnesses.{harness_name}", entry)

    if user_id:
        set_value(config, "user_id", user_id)

    save_config(config, config_path)


# ---------------------------------------------------------------------------
# New shared helpers
# ---------------------------------------------------------------------------


def dry_run() -> bool:
    """True when ARIZE_DRY_RUN env var is set to a truthy value ('1','true','yes')."""
    return os.environ.get("ARIZE_DRY_RUN", "").lower() in ("1", "true", "yes")


def non_interactive() -> bool:
    """True when ARIZE_NONINTERACTIVE is set to a truthy value ('1','true','yes').

    In this mode the setup wizards never call ``input()``/``getpass()``: every
    value is resolved from the environment (or a dotenv file, see
    ``_dotenv_values``) and a missing required value is a hard error instead of
    a prompt. Deliberately opt-in — without it, an exported ``ARIZE_API_KEY``
    would silently stop the interactive wizard from asking its questions.
    """
    return os.environ.get("ARIZE_NONINTERACTIVE", "").lower() in ("1", "true", "yes")


# Keys we will read out of a dotenv file. Everything else in the file is
# ignored, so pointing at an app's .env can't inject unrelated settings.
_DOTENV_KEYS = (
    "ARIZE_API_KEY",
    "ARIZE_SPACE_ID",
    "ARIZE_BACKEND",
    "ARIZE_OTLP_ENDPOINT",
    "ARIZE_PROJECT_NAME",
    "ARIZE_USER_ID",
    "ARIZE_LOG_PROMPTS",
    "ARIZE_LOG_TOOL_DETAILS",
    "ARIZE_LOG_TOOL_CONTENT",
    "PHOENIX_ENDPOINT",
    "PHOENIX_API_KEY",
)

_dotenv_cache: Optional[dict] = None


def _dotenv_candidates() -> list:
    """Dotenv files to consider, in order. ARIZE_ENV_FILE overrides the search."""
    explicit = os.environ.get("ARIZE_ENV_FILE", "").strip()
    if explicit:
        return [Path(explicit).expanduser()]
    cwd = Path.cwd()
    return [cwd / ".env", cwd / ".env.local"]


def _parse_dotenv(path: Path) -> dict:
    """Extract _DOTENV_KEYS from a dotenv file. Unreadable file → {}.

    Handles ``export KEY=value``, surrounding quotes, comments and blank lines.
    Values are not shell-expanded — a literal ``$FOO`` stays literal.
    """
    values: dict = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return values

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, raw = line.partition("=")
        if key.strip() not in _DOTENV_KEYS:
            continue
        value = raw.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key.strip()] = value.strip()

    return values


def _dotenv_values() -> dict:
    """Load Arize/Phoenix values from a dotenv file, once per process.

    Lets ``ax api-keys create --env-file .env`` feed the installer directly:
    the key goes CLI → file → installer, never through argv, shell history, or
    a coding agent's transcript. Only consulted when the real environment
    doesn't already supply the value, so exported vars still win.
    """
    global _dotenv_cache
    if _dotenv_cache is not None:
        return _dotenv_cache

    _dotenv_cache = {}
    for path in _dotenv_candidates():
        if not path.is_file():
            continue
        found = _parse_dotenv(path)
        if found:
            info(f"Reading configuration from {path} ({len(found)} value(s))")
            _dotenv_cache = found
            break

    return _dotenv_cache


def _reset_dotenv_cache() -> None:
    """Clear the dotenv cache. For tests, which vary cwd and file contents."""
    global _dotenv_cache
    _dotenv_cache = None


def _env(name: str) -> str:
    """Resolve a config value: real environment first, then the dotenv file."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    return _dotenv_values().get(name, "").strip()


def env_value(name: str) -> str:
    """Resolve a config value from the environment or dotenv file.

    Public entry point for harness installers that have a prompt of their own
    to resolve (currently only Kiro's agent name).
    """
    return _env(name)


def env_flag(name: str, default: bool = True) -> bool:
    """Read a boolean setting from the environment or dotenv file.

    Only explicit falsey words turn a default-on setting off, and only explicit
    truthy words turn a default-off setting on.
    """
    raw = _env(name).lower()
    if not raw:
        return default
    if default:
        return raw not in ("0", "false", "no", "n", "off")
    return raw in ("1", "true", "yes", "y", "on")


def _require_env(name: str, what: str) -> str:
    """Resolve a required value, or exit with an actionable message."""
    value = _env(name)
    if not value:
        err(f"{what} is required for a non-interactive install — set {name} or put it in a .env file.")
        sys.exit(1)
    return value


def ensure_shared_runtime() -> None:
    """Create ~/.arize/harness/{bin,run,logs,state} if missing. Idempotent.

    Also removes any legacy collector artefacts (bin/arize-collector,
    run/collector.pid, logs/collector.log) left over from pre-buffer-service
    installs.
    """
    install_dir = INSTALL_DIR
    subdirs = [BIN_DIR, RUN_DIR, LOG_DIR, STATE_DIR]

    for d in subdirs:
        if not d.exists():
            if dry_run():
                info(f"would create {d}")
            else:
                d.mkdir(parents=True, exist_ok=True)

    # Remove legacy collector artefacts
    for rel in _LEGACY_ARTEFACTS:
        legacy = install_dir / rel
        if legacy.exists():
            if dry_run():
                info(f"would remove legacy artefact {legacy}")
            else:
                legacy.unlink()


def venv_bin(name: str) -> Path:
    """Return the full path to a venv binary.

    On POSIX: VENV_DIR/bin/<name>. On Windows: VENV_DIR/Scripts/<name>.exe.
    Does NOT verify the file exists.
    """
    if os.name == "nt":
        return VENV_DIR / "Scripts" / f"{name}.exe"
    return VENV_DIR / "bin" / name


def merge_harness_entry(
    name: str,
    project_name: str,
    target: str | None = None,
    credentials: dict | None = None,
    collector: dict | None = None,
) -> None:
    """Read config.json, add/update harnesses.<name>, write back with 0o600.

    If target + credentials are provided, writes the full entry.
    If only project_name, updates only that field (leaves other fields alone).
    If the file doesn't exist, creates it with just this entry under
    harnesses:.
    """
    config_path = str(CONFIG_FILE)
    config = load_config(config_path)

    if not config:
        config = {"harnesses": {}}

    if target is not None and credentials is not None:
        entry: dict = {
            "project_name": project_name,
            "target": target,
            "endpoint": credentials.get("endpoint", ""),
            "api_key": credentials.get("api_key", ""),
        }
        if target == "arize" and "space_id" in credentials:
            entry["space_id"] = credentials["space_id"]
        if collector is not None:
            entry["collector"] = collector
        set_value(config, f"harnesses.{name}", entry)
    else:
        set_value(config, f"harnesses.{name}.project_name", project_name)
        if collector is not None:
            set_value(config, f"harnesses.{name}.collector", collector)

    if dry_run():
        info(f"would write harness entry '{name}' to {config_path}")
        return

    save_config(config, config_path)


def remove_harness_entry(name: str) -> None:
    """Read config.json, remove harnesses.<name> if present, write back.

    No-op if the file doesn't exist or the key isn't present.
    """
    config_path = str(CONFIG_FILE)
    config = load_config(config_path)

    if not config:
        return

    harnesses = config.get("harnesses")
    if not isinstance(harnesses, dict) or name not in harnesses:
        return

    if dry_run():
        info(f"would remove harness entry '{name}' from {config_path}")
        return

    delete_value(config, f"harnesses.{name}")
    save_config(config, config_path)


def list_installed_harnesses() -> list[str]:
    """Return the list of keys under harnesses.* in config.json.

    Returns empty list if config is missing.
    """
    config_path = str(CONFIG_FILE)
    config = load_config(config_path)

    if not config:
        return []

    harnesses = config.get("harnesses")
    if not isinstance(harnesses, dict):
        return []

    return list(harnesses.keys())


def harness_dir(harness: str) -> Path:
    """Return the absolute path of <install-dir>/tracing/<harness>/.

    Maps a harness alias (e.g. ``claude-code``) to its directory name
    (``claude_code``) under ``~/.arize/harness/tracing/``.
    """
    sub_name = harness.replace("-", "_")
    return INSTALL_DIR / "tracing" / sub_name


def symlink_skills(harness: str, target_dir: Path | None = None) -> None:
    """Symlink <install-dir>/tracing/<harness>/skills/* into target_dir/.agents/skills/.

    target_dir defaults to the current working directory. Idempotent (skip
    existing links pointing at the right target). Does nothing if the harness
    has no skills/ directory.
    """
    hdir = harness_dir(harness)
    skills_src = hdir / "skills"

    if not skills_src.is_dir():
        return

    if target_dir is None:
        target_dir = Path.cwd()

    dest = target_dir / ".agents" / "skills"

    if dry_run():
        for item in skills_src.iterdir():
            info(f"would symlink {dest / item.name} -> {item}")
        return

    dest.mkdir(parents=True, exist_ok=True)

    for item in skills_src.iterdir():
        link = dest / item.name
        if link.is_symlink():
            if link.resolve() == item.resolve():
                continue  # already correct
            link.unlink()
        elif link.exists():
            continue  # regular file — don't overwrite
        link.symlink_to(item)


def unlink_skills(harness: str, target_dir: Path | None = None) -> None:
    """Remove symlinks created by symlink_skills() for <harness>.

    Only removes symlinks, never regular files. Idempotent.
    """
    hdir = harness_dir(harness)
    skills_src = hdir / "skills"

    if not skills_src.is_dir():
        return

    if target_dir is None:
        target_dir = Path.cwd()

    dest = target_dir / ".agents" / "skills"

    if not dest.is_dir():
        return

    for item in skills_src.iterdir():
        link = dest / item.name
        if link.is_symlink():
            if dry_run():
                info(f"would unlink {link}")
            else:
                link.unlink()
