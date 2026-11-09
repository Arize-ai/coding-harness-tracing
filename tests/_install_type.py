"""Run setup from this checkout as if from a persistent install (see ``tests/_persistent_install``)."""

from __future__ import annotations

import os
from pathlib import Path

from tests._pyproject import REPO_ROOT

PERSISTENT_SHIM = Path(__file__).resolve().parent / "_persistent_install"

# PYTHONPATH for a subprocess that runs setup from this checkout.
PERSISTENT_PYTHONPATH = os.pathsep.join([str(PERSISTENT_SHIM), str(REPO_ROOT)])

# The shim's code, for a test that writes its own sitecustomize.py.
PERSISTENT_SNIPPET = (PERSISTENT_SHIM / "sitecustomize.py").read_text()
