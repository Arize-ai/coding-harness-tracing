#!/usr/bin/env python3
"""Arize Copilot Tracing Plugin - Interactive Setup.

Configures tracing for GitHub Copilot in both VS Code and CLI modes.
Writes config.json to ~/.arize/harness/config.json and installs hooks
into .github/hooks/ (project-local).

The ``arize-setup-copilot`` entry point calls ``main()`` here, which runs the
legacy interactive wizard.  The new ``tracing/copilot/install.py`` module
provides the decomposed ``install()`` / ``uninstall()`` API used by the
shell router.  ``install()`` and ``uninstall()`` below delegate to it.
"""

from __future__ import annotations

import sys

from tracing.copilot import install as _install_mod


def install() -> None:
    """Delegate to tracing/copilot/install.py install()."""
    _install_mod.install()


def uninstall() -> None:
    """Delegate to tracing/copilot/install.py uninstall()."""
    _install_mod.uninstall()


def main() -> None:
    """Entry point for arize-setup-copilot."""
    try:
        _run()
    except (KeyboardInterrupt, EOFError):
        print("\nSetup cancelled.")
        sys.exit(1)


def _run() -> None:
    """Delegate to the install module in tracing/copilot/.

    This replaces the old interactive flow so that ``arize-setup-copilot``
    and the installer router share a single code path.
    """
    _install_mod.install()


if __name__ == "__main__":
    main()
