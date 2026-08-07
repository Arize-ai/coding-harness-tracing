#!/usr/bin/env python3
"""Arize Devin Tracing - Interactive Setup.

Entry point for ``arize-setup-devin``. The heavy lifting lives in
``tracing/devin/install.py``; this module is a thin shim for the
``arize-setup-devin`` console script.
"""

from __future__ import annotations

import sys

from tracing.devin import install as _install_mod


def main() -> None:
    """Entry point for arize-setup-devin."""
    try:
        _run()
    except (KeyboardInterrupt, EOFError):
        print("\nSetup cancelled.")
        sys.exit(1)


def _run() -> None:
    """Delegate to the install module in tracing/devin/."""
    _install_mod.install(with_skills=False)


if __name__ == "__main__":
    main()
