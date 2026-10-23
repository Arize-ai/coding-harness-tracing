"""``arize-harness setup | uninstall | status | update``: install.sh's commands, run from the installed package.

Imported by ``core/cli.py`` only once one of these subcommands is chosen, never
on the ``hook`` path, so it may import ``core.setup`` (and with it
``python-dotenv``).
"""

from __future__ import annotations

import argparse
import inspect
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

import core
from core import install_table
from core.config import load_config, migrate_yaml_config
from core.constants import HARNESSES
from core.runtime import is_binary
from core.setup import INSTALL_DIR, VENV_DIR, err, info, is_harness_installed, list_installed_harnesses, print_color

# Printed by install.sh too; the smoke test greps for "no source tree to update".
_NO_SOURCE = (
    "This looks like an offline install with no source tree to update.",
    "Re-run the installer that created it (for npx evals, update that), or",
    "pass --wheel-dir <dir> with a newer wheel.",
)


def warn(msg: str) -> None:
    print_color(f"[arize] warning: {msg}", "yellow")


def header(msg: str) -> None:
    print("")
    print_color(msg, "blue")
    print("")


# ---------------------------------------------------------------------------
# Install type
# ---------------------------------------------------------------------------


def on_windows() -> bool:
    return os.name == "nt"


def running_from_install_venv() -> bool:
    """True when this interpreter is ``~/.arize/harness/venv``, the venv install.sh and install.bat create."""
    try:
        return Path(sys.prefix).resolve() == VENV_DIR.resolve()
    except OSError:
        return False


def _other_install_update_hint() -> str:
    """One line saying how to update an install that is not ``~/.arize/harness/venv``."""
    prefix_parts = Path(sys.prefix).parts
    if "uv" in prefix_parts and "tools" in prefix_parts:
        return "This is a uv tool install; update it with `uv tool upgrade coding-harness-tracing`."
    checkout = Path(core.__file__).resolve().parent.parent
    if (checkout / ".git").exists() and (checkout / "pyproject.toml").is_file():
        return f"This runs from the source checkout at {checkout}; update it with `git pull` and `uv sync` there."
    # Nothing is published to an index, so the only update is a newer wheel.
    return (
        f"This runs from {sys.executable}, not {VENV_DIR}; re-run the installer that created it, or install a "
        f"newer wheel with `{sys.executable} -m pip install -U --no-index --find-links <dir> coding-harness-tracing`."
    )


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------


def _detection_mark(key: str) -> str:
    values = install_table.detection_values(key)
    if values is None:
        return "not detected automatically"
    home, bin_name = values
    return "detected" if is_harness_installed(home_subdir=home, bin_name=bin_name) else "not detected"


def _prompt_harnesses(parser: argparse.ArgumentParser) -> List[str]:
    """List every harness with its detection mark and read a comma-separated choice.

    Every name is checked before any is set up, so a typo changes nothing.
    """
    aliases = {key: alias for alias, key in install_table.ALIASES.items()}
    print("Harnesses:")
    print("")
    for key in HARNESSES:
        name = f"{key} ({aliases[key]})" if key in aliases else key
        print(f"  {name:<22} {_detection_mark(key)}")
    print("")
    try:
        answer = input("Harnesses to set up (comma-separated): ")
    except (EOFError, KeyboardInterrupt):
        print("")
        parser.error("no harness given")
    names = [name.strip() for name in answer.split(",") if name.strip()]
    if not names:
        parser.error("no harness given")
    keys: List[str] = []
    for name in names:
        resolved = install_table.resolve(name)
        if resolved is None:
            parser.error(f"unknown harness '{name}'")
        if resolved not in keys:
            keys.append(resolved)
    return keys


def _setup_one(key: str, with_skills: bool, backend: Optional[str]) -> None:
    header(f"Installing {key} tracing")
    info("Migrating legacy config.yaml to config.json (if present)...")
    if migrate_yaml_config():
        print("Migrated config.yaml -> config.json")

    if backend:
        entry = (load_config().get("harnesses") or {}).get(key)
        if isinstance(entry, dict) and "target" in entry:
            warn(f"{key} is already configured for {entry['target']}; keeping it and ignoring --backend {backend}")

    installer = install_table.load(key)
    if with_skills and "with_skills" in inspect.signature(installer.install).parameters:
        installer.install(with_skills=True)
    else:
        if with_skills:
            warn(f"{key} has no skills to install; ignoring --with-skills")
        installer.install()
    info("Setup complete!")


def cmd_setup(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.non_interactive:
        os.environ["ARIZE_NONINTERACTIVE"] = "1"
    if args.backend:
        os.environ["ARIZE_BACKEND"] = args.backend

    if args.harness is not None:
        key = install_table.resolve(args.harness)
        if key is None:
            parser.error(f"unknown harness '{args.harness}'")
        keys = [key]
    elif args.non_interactive:
        parser.error("setup --non-interactive needs a harness name")
    else:
        keys = _prompt_harnesses(parser)

    for key in keys:
        _setup_one(key, args.with_skills, args.backend)
    return 0


# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------


def cmd_uninstall(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.harness is not None and args.all:
        parser.error("give a harness name or --all, not both")
    if args.harness is not None:
        key = install_table.resolve(args.harness)
        if key is None:
            parser.error(f"unknown harness '{args.harness}'")
        header(f"Uninstalling {key} tracing")
        install_table.load(key).uninstall()
        return 0
    if not args.all:
        err("uninstall needs a harness name, or --all to remove everything. Nothing was changed.")
        return 2

    if is_binary():
        err("uninstall --all is not supported on a binary install yet. Nothing was changed.")
        return 1
    if on_windows() and running_from_install_venv():
        err("On Windows, run `install.bat uninstall` to remove this install. Nothing was changed.")
        return 1

    from core.setup.wipe import wipe_shared_runtime

    header("Full uninstall")
    # Each harness's uninstall first, so registrations outside the install dir
    # (settings.json hooks, config.toml notify, ...) go before the runtime does.
    for name in list_installed_harnesses():
        key = install_table.resolve(name)
        if key is None:
            warn(f"Unknown harness: {name} (skipping)")
            continue
        info(f"Uninstalling {key} tracing...")
        try:
            install_table.load(key).uninstall()
        except (Exception, SystemExit) as e:
            warn(f"{key} uninstall failed (continuing): {e}")
    wipe_shared_runtime()
    return 0


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    from core.setup.status import main as status_main

    return status_main(["--json"] if args.json else [])


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


def _run(cmd: List[str]) -> bool:
    return subprocess.run(cmd, check=False).returncode == 0


def _pip_install(*args: str) -> bool:
    """``pip install`` with the running interpreter, so the install being updated is the one that changes."""
    return _run([sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", *args])


def cmd_update(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if is_binary():
        err("update is not supported on a binary install yet. Nothing was changed.")
        return 1
    if not running_from_install_venv():
        err(_other_install_update_hint())
        return 1
    if on_windows():
        err("On Windows, run `install.bat update` to update this install. Nothing was changed.")
        return 1

    header("Updating coding-harness-tracing")
    # Re-registering prompts for the project name; with no terminal to answer
    # on, use saved values instead, as install.sh update does.
    if not sys.stdin.isatty():
        os.environ["ARIZE_NONINTERACTIVE"] = "1"

    if args.wheel_dir is not None:
        wheel_dir = Path(args.wheel_dir).expanduser()
        if not wheel_dir.is_dir():
            err(f"--wheel-dir needs a directory; got '{args.wheel_dir}'")
            return 1
        wheel_dir = wheel_dir.resolve()
        if not any(wheel_dir.glob("coding_harness_tracing-*.whl")):
            err(f"No coding_harness_tracing-*.whl in {wheel_dir}")
            return 1
        info(f"Updating from local wheels in {wheel_dir}...")
        # --no-index: a missing wheel must fail, never fall back to PyPI.
        installed = _pip_install("-U", "--no-index", "--find-links", str(wheel_dir), "coding-harness-tracing")
    elif (INSTALL_DIR / ".git").is_dir():
        info("Pulling latest changes...")
        if not _run(["git", "-C", str(INSTALL_DIR), "pull", "--ff-only"]):
            err(f"git pull failed in {INSTALL_DIR}; run `install.sh update` to update this install.")
            return 1
        info("Reinstalling coding-harness-tracing...")
        installed = _pip_install("-U", str(INSTALL_DIR))
    elif (INSTALL_DIR / "pyproject.toml").is_file():
        err(f"{INSTALL_DIR} was installed from a tarball; run `install.sh update` to update it. Nothing was changed.")
        return 1
    else:
        for line in _NO_SOURCE:
            err(line)
        return 1
    if not installed:
        err("Failed to install coding-harness-tracing")
        return 1

    # The reinstall may have replaced this process's own modules, so the rest
    # runs in fresh interpreters, from site-packages only (-I keeps the cwd off
    # sys.path).
    info("Migrating legacy config.yaml to config.json (if present)...")
    _run([sys.executable, "-I", "-m", "core.config", "migrate"])
    names = list_installed_harnesses()
    if not names:
        info("No installed harnesses found to re-register")
    for name in names:
        key = install_table.resolve(name)
        if key is None:
            warn(f"Unknown harness: {name} (skipping)")
            continue
        info(f"Re-registering {key}...")
        # One harness failing must not leave the rest half-updated.
        if not _run([sys.executable, "-I", "-m", "core.cli", "setup", key]):
            warn(f"{key} re-registration failed (continuing)")
    info("Update complete.")
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arize-harness", description="Arize coding harness tracing.")
    parser.add_argument("--version", action="store_true", help="print the installed version and exit")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # Dispatched by core/cli.py before this parser runs; listed for --help only.
    sub.add_parser("hook", help="run a harness hook (arize-harness hook --list)", add_help=False)

    harness_names = ", ".join([*install_table.ALIASES, *install_table.INSTALLERS])
    setup = sub.add_parser("setup", help="set up tracing for a harness", description=f"Harnesses: {harness_names}.")
    setup.add_argument("harness", nargs="?", help="harness to set up; omit to choose from a list")
    setup.add_argument("--backend", choices=("arize", "phoenix"), help="skip the backend menu")
    setup.add_argument(
        "--non-interactive",
        "-y",
        action="store_true",
        help="ask nothing; read settings from the environment or ARIZE_ENV_FILE",
    )
    setup.add_argument("--with-skills", action="store_true", help="symlink harness skills into .agents/skills/")
    setup.set_defaults(func=cmd_setup, parser=setup)

    uninstall = sub.add_parser(
        "uninstall", help="remove tracing for one harness, or everything", description=f"Harnesses: {harness_names}."
    )
    uninstall.add_argument("harness", nargs="?", help="harness to remove")
    uninstall.add_argument("--all", action="store_true", help="remove every harness and the shared runtime")
    uninstall.set_defaults(func=cmd_uninstall, parser=uninstall)

    status = sub.add_parser("status", help="report configured harnesses and whether their hooks are wired up")
    status.add_argument(
        "--json",
        action="store_true",
        help="machine-readable JSON; exit 0 all wired up, 1 nothing configured, 2 hooks missing",
    )
    status.set_defaults(func=cmd_status, parser=status)

    update = sub.add_parser("update", help="update this install and re-register every harness")
    update.add_argument("--wheel-dir", metavar="DIR", help="install from local wheels in DIR, never an index")
    update.set_defaults(func=cmd_update, parser=update)
    return parser


def run(argv: List[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(f"arize-harness {core.version()}")
        return 0
    if getattr(args, "func", None) is None:
        parser.print_help()
        return 0
    return args.func(args, args.parser)
