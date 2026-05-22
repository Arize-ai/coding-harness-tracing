#!/usr/bin/env python3
"""Shared setup utilities for all harness setup wizards."""

from __future__ import annotations

import os
import shutil
import sys
from getpass import getpass
from pathlib import Path
from typing import Optional

try:
    import yaml  # noqa: F401  # presence check; writing configs needs PyYAML at runtime
except ImportError:
    sys.stderr.write("error: PyYAML not installed. Install it in the collector venv.\n")
    sys.exit(1)

from core.config import delete_value, load_config, save_config, set_value

# ---------------------------------------------------------------------------
# Env-var helpers for non-interactive prompts
# ---------------------------------------------------------------------------


def _env_get(name: str) -> str | None:
    """Return os.environ[name] stripped, or None if unset/blank."""
    val = os.environ.get(name)
    if val is None:
        return None
    val = val.strip()
    return val if val else None


def _non_interactive() -> bool:
    val = os.environ.get("ARIZE_INSTALL_NON_INTERACTIVE", "").strip().lower()
    return val in ("1", "true", "yes")


def _require(name: str, human: str) -> str:
    """Used in strict mode: read an env var or exit with a clear error."""
    val = _env_get(name)
    if val is None:
        err(f"--non-interactive mode but {human} not provided (set {name})")
        sys.exit(1)
    return val


def _env_bool(name: str, default: bool) -> bool:
    val = _env_get(name)
    if val is None:
        return default
    return val.lower() in ("1", "true", "yes", "y")


# ---------------------------------------------------------------------------
# Shared path constants
# ---------------------------------------------------------------------------

INSTALL_DIR = Path.home() / ".arize" / "harness"
VENV_DIR = INSTALL_DIR / "venv"
CONFIG_FILE = INSTALL_DIR / "config.yaml"
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

    if not sys.stdout.isatty():
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

    Honors ARIZE_INSTALL_* env vars: when ARIZE_INSTALL_BACKEND is set, the
    interactive choice and copy-from menu are skipped. Per-field env vars
    (ARIZE_INSTALL_PHOENIX_ENDPOINT, PHOENIX_API_KEY, ARIZE_API_KEY,
    ARIZE_INSTALL_SPACE_ID, ARIZE_INSTALL_OTLP_ENDPOINT) bypass their prompts.
    ARIZE_INSTALL_NON_INTERACTIVE=1 makes missing required fields a hard error.

    Returns (target, credentials).  credentials keys:
      phoenix: {"endpoint", "api_key"}
      arize:   {"endpoint", "api_key", "space_id"}
    """
    backend_env = _env_get("ARIZE_INSTALL_BACKEND")
    skip_copy_from = False
    if backend_env is not None:
        target = backend_env.lower()
        if target not in ("arize", "phoenix"):
            err(f"ARIZE_INSTALL_BACKEND must be 'arize' or 'phoenix', got: {backend_env!r}")
            sys.exit(1)
        skip_copy_from = True
    elif _non_interactive():
        err("--non-interactive mode but ARIZE_INSTALL_BACKEND not set")
        sys.exit(1)
    else:
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

    # --- copy-from logic (skipped when backend is env-provided) ---
    if not skip_copy_from:
        copied = _try_copy_from(target, existing_harnesses)
        if copied is not None:
            return (target, copied)

    # --- credential gathering: env first, then prompt ---
    if target == "phoenix":
        phoenix_endpoint = _env_get("ARIZE_INSTALL_PHOENIX_ENDPOINT")
        api_key_env = _env_get("PHOENIX_API_KEY")

        if phoenix_endpoint is None:
            if _non_interactive():
                phoenix_endpoint = "http://localhost:6006"
            else:
                print("")
                phoenix_endpoint = input("Phoenix endpoint [http://localhost:6006]: ").strip()
                if not phoenix_endpoint:
                    phoenix_endpoint = "http://localhost:6006"

        if api_key_env is not None:
            api_key = api_key_env
        elif _non_interactive():
            api_key = ""  # blank == no auth, valid for Phoenix
        else:
            api_key = getpass("Phoenix API Key (blank for no auth): ").strip()

        return ("phoenix", {"endpoint": phoenix_endpoint, "api_key": api_key})

    # arize
    api_key_env = _env_get("ARIZE_API_KEY")
    space_id_env = _env_get("ARIZE_INSTALL_SPACE_ID")
    otlp_env = _env_get("ARIZE_INSTALL_OTLP_ENDPOINT")

    if api_key_env is not None:
        api_key = api_key_env
    elif _non_interactive():
        err("--non-interactive mode but ARIZE_API_KEY not set")
        sys.exit(1)
    else:
        print("")
        api_key = getpass("Arize API Key: ").strip()

    if space_id_env is not None:
        space_id = space_id_env
    elif _non_interactive():
        err("--non-interactive mode but ARIZE_INSTALL_SPACE_ID not set")
        sys.exit(1)
    else:
        space_id = input("Arize Space ID: ").strip()

    if not api_key or not space_id:
        err("API key and Space ID are required for Arize AX")
        sys.exit(1)

    if otlp_env is not None:
        otlp_endpoint = otlp_env
    elif _non_interactive():
        otlp_endpoint = "otlp.arize.com:443"
    else:
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
    """Prompt for project name. Returns default if blank.

    Honors ARIZE_INSTALL_PROJECT_NAME. In non-interactive mode without that
    var, returns ``default`` rather than erroring, since the function already
    has a default-fallback contract.
    """
    val = _env_get("ARIZE_INSTALL_PROJECT_NAME")
    if val is not None:
        return val
    if _non_interactive():
        return default
    print("")
    name = input(f"Project name [{default}]: ").strip()
    return name if name else default


def prompt_content_logging() -> dict:
    """Prompt for content logging settings. Returns the dict to write under `logging:`.

    All three default to True to match the kit's existing capture-everything
    behavior. Users opt out per category.

    Individual fields can be overridden via ARIZE_INSTALL_LOG_PROMPTS,
    ARIZE_INSTALL_LOG_TOOL_DETAILS, ARIZE_INSTALL_LOG_TOOL_CONTENT. When all
    three are set (or ARIZE_INSTALL_NON_INTERACTIVE=1), the banner and prompts
    are skipped entirely.
    """
    prompts_env = _env_get("ARIZE_INSTALL_LOG_PROMPTS")
    tool_details_env = _env_get("ARIZE_INSTALL_LOG_TOOL_DETAILS")
    tool_content_env = _env_get("ARIZE_INSTALL_LOG_TOOL_CONTENT")

    if prompts_env is not None and tool_details_env is not None and tool_content_env is not None:
        return {
            "prompts": prompts_env.lower() in ("1", "true", "yes", "y"),
            "tool_details": tool_details_env.lower() in ("1", "true", "yes", "y"),
            "tool_content": tool_content_env.lower() in ("1", "true", "yes", "y"),
        }
    if _non_interactive():
        return {
            "prompts": _env_bool("ARIZE_INSTALL_LOG_PROMPTS", True),
            "tool_details": _env_bool("ARIZE_INSTALL_LOG_TOOL_DETAILS", True),
            "tool_content": _env_bool("ARIZE_INSTALL_LOG_TOOL_CONTENT", True),
        }

    print("")
    if sys.stdout.isatty() and os.name != "nt":
        print("\033[1;33mSecurity:\033[0m Traces can contain sensitive data — credentials, PII, file contents.")
    else:
        print("Security: Traces can contain sensitive data — credentials, PII, file contents.")
    print("All content is logged by default. Opt out per category to match your security needs.")
    print("")

    def _ask(env_val: str | None, prompt_text: str) -> bool:
        if env_val is not None:
            return env_val.lower() in ("1", "true", "yes", "y")
        ans = input(prompt_text).strip().lower()
        return ans not in ("n", "no")

    return {
        "prompts": _ask(prompts_env, "  Log user prompts? [Y/n]: "),
        "tool_details": _ask(
            tool_details_env, "  Log what tools were asked to do (commands, file paths, URLs)? [Y/n]: "
        ),
        "tool_content": _ask(tool_content_env, "  Log what tools returned (file contents, command output)? [Y/n]: "),
    }


def write_logging_config(logging_block: dict, config_path: str | None = None) -> None:
    """Merge a logging block into the top-level `logging:` key in config.yaml."""
    config = load_config(config_path)
    if not config:
        config = {}
    set_value(config, "logging", logging_block)
    if dry_run():
        info("would write logging block to config.yaml")
        return
    save_config(config, config_path)


def prompt_user_id() -> str:
    """Optional user ID prompt. Returns "" if skipped.

    Honors ARIZE_INSTALL_USER_ID. In non-interactive mode without that var,
    returns "" since user_id is optional.
    """
    val = _env_get("ARIZE_INSTALL_USER_ID")
    if val is not None:
        return val
    if _non_interactive():
        return ""
    print("")
    if sys.stdout.isatty() and os.name != "nt":
        print("\033[0;34mOptional:\033[0m Set a user ID to identify your spans (useful for teams).")
    else:
        print("Optional: Set a user ID to identify your spans (useful for teams).")
    user_id = input("User ID (leave blank to skip): ").strip()
    return user_id


def prompt_verbose() -> bool:
    """Optional verbose-mode prompt. Returns False if skipped.

    When True, hook handlers write trace summaries to stderr in addition to
    OTLP export (the existing ARIZE_VERBOSE=true behavior, just made persistent
    via config.yaml). Honors ARIZE_INSTALL_VERBOSE; defaults to False in
    non-interactive mode.
    """
    val = _env_get("ARIZE_INSTALL_VERBOSE")
    if val is not None:
        return val.lower() in ("1", "true", "yes", "y")
    if _non_interactive():
        return False
    print("")
    if sys.stdout.isatty() and os.name != "nt":
        print(
            "\033[0;34mOptional:\033[0m Verbose mode prints trace summaries to your terminal in addition to sending them to the backend."
        )
    else:
        print(
            "Optional: Verbose mode prints trace summaries to your terminal in addition to sending them to the backend."
        )
    ans = input("Enable verbose mode? [y/N]: ").strip().lower()
    return ans in ("y", "yes")


def write_verbose_config(value: bool, config_path: str | None = None) -> None:
    """Set the top-level ``verbose`` key in config.yaml.

    Mirrors the logging-block writer: read existing config, set the key,
    write back. Honors dry_run().
    """
    config = load_config(config_path)
    if not config:
        config = {}
    set_value(config, "verbose", value)
    if dry_run():
        info(f"would set verbose={value} in config.yaml")
        return
    save_config(config, config_path)


def write_config(
    target: str,
    credentials: dict,
    harness_name: str,
    project_name: str,
    user_id: str = "",
    collector: dict | None = None,
    config_path: Optional[str] = None,
) -> None:
    """Write or merge config.yaml with a fully-flattened harnesses.<name> entry.

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
    """Read config.yaml, add/update harnesses.<name>, write back with 0o600.

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
    """Read config.yaml, remove harnesses.<name> if present, write back.

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
    """Return the list of keys under harnesses.* in config.yaml.

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
