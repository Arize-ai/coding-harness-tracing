#!/usr/bin/env python3
"""Arize Antigravity Tracing - Interactive Setup.

Entry point for ``arize-setup-antigravity``. The heavy lifting lives in
``tracing/antigravity/install.py``; this module is a thin shim for the
``arize-setup-antigravity`` console script.
"""

from __future__ import annotations

import sys

from tracing.antigravity import install as _install_mod


def main() -> None:
    """Entry point for arize-setup-antigravity."""
    try:
        _run()
    except (KeyboardInterrupt, EOFError):
        print("\nSetup cancelled.")
        sys.exit(1)


def _run() -> None:
    """Delegate to the install module in tracing/antigravity/."""
    _install_mod.install()


if __name__ == "__main__":
    main()
