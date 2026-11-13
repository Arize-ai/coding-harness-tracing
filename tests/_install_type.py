"""Helpers that let test subprocesses run setup from this checkout."""

from __future__ import annotations

import os
from pathlib import Path

from tests._pyproject import REPO_ROOT

PERSISTENT_SHIM = Path(__file__).resolve().parent / "_persistent_install"

# PYTHONPATH for a subprocess that runs setup from this checkout.
PERSISTENT_PYTHONPATH = os.pathsep.join([str(PERSISTENT_SHIM), str(REPO_ROOT)])

# For tests that write their own sitecustomize.py.
PERSISTENT_SNIPPET = (PERSISTENT_SHIM / "sitecustomize.py").read_text()
