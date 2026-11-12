"""``arize-harness setup | uninstall | status | update``: install.sh's commands, run from the installed package.

Imported by ``core/cli.py`` only once one of these subcommands is chosen, never
on the ``hook`` path, so it may import ``core.setup`` (and with it
``python-dotenv``).
"""

from __future__ import annotations

import argparse
import inspect
import os
import sys
from pathlib import Path
from typing import List, Optional

import core
from core import install_table
from core.config import load_config, migrate_yaml_config
from core.constants import HARNESSES
from core.runtime import is_binary
from core.setup import INSTALL_DIR, VENV_DIR, err, info, is_harness_installed, list_installed_harnesses, print_color
from core.setup.runner import (
    DIST_NAME,
    INSTALL_HINT,
    SetupRefused,
    arize_harness_path,
    require_setup_writes,
    setup_write_refusal,
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


def update_hint() -> str:
    """One line telling the user how to update this install, based on how it was installed."""
    if is_binary():
        return f"This is a binary install ({arize_harness_path()}); download a new arize-harness binary to replace it."
    if running_from_install_venv():
        if on_windows():
            return "This is an install.bat install; update it with `install.bat update`."
        return f"This is an install.sh install; update it by re-running install.sh (`{INSTALL_DIR / 'install.sh'} update`)."
    reason = setup_write_refusal()
    if reason is None:
        prefix = Path(sys.prefix)
        if (prefix / "uv-receipt.toml").is_file():
            return f"This is a uv tool install; update it with `uv tool upgrade {DIST_NAME}`."
        if (prefix / "pipx_metadata.json").is_file():
            return f"This is a pipx install; update it with `pipx upgrade {DIST_NAME}`."
    return f"{reason or 'this is not a persistent install'}; {INSTALL_HINT}."


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


def _refresh_files() -> None:
    """Rewrite files recorded by a different package version, if this install may write."""
    from core.setup.assets import refresh_and_report

    refresh_and_report()


def cmd_setup(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    # Checked before any prompt: hooks registered from a temporary install would break.
    try:
        require_setup_writes()
    except SetupRefused:
        return 1
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
    # also refresh files from other harnesses
    _refresh_files()
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
        _refresh_files()
        return 0
    if not args.all:
        err("uninstall needs a harness name, or --all to remove everything. Nothing was changed.")
        return 2

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
    # Keep the running package, which may live under ~/.arize/harness.
    wipe_shared_runtime(keep_running_package=True)
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


def cmd_update(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Print how to update this install and exit 1 without changing anything.

    Setup needn't be re-run: hooks run the command link, which follows the upgrade.
    """
    err(f"{update_hint()} Nothing was changed.")
    return 1


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
    setup.add_argument("--with-skills", action="store_true", help="copy harness skills into .agents/skills/")
    setup.set_defaults(func=cmd_setup, parser=setup)

    uninstall = sub.add_parser(
        "uninstall", help="remove tracing for one harness, or everything", description=f"Harnesses: {harness_names}."
    )
    uninstall.add_argument("harness", nargs="?", help="harness to remove")
    uninstall.add_argument(
        "--all", action="store_true", help="remove every harness, then ~/.arize/harness config, state and logs"
    )
    uninstall.set_defaults(func=cmd_uninstall, parser=uninstall)

    status = sub.add_parser("status", help="report configured harnesses and whether their hooks are wired up")
    status.add_argument(
        "--json",
        action="store_true",
        help="machine-readable JSON; exit 0 all wired up, 1 nothing configured, 2 hooks missing",
    )
    status.set_defaults(func=cmd_status, parser=status)

    update = sub.add_parser("update", help="print the command that updates this install")
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
