"""Shared ``[project.scripts]`` reader for tests that check entry points."""

from __future__ import annotations

import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # tomllib is stdlib from 3.11; tomli is the same parser as a backport
    import tomli as tomllib

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"


def parse_project_scripts(text: str) -> dict[str, str]:
    """Return the ``[project.scripts]`` table from pyproject text."""
    return tomllib.loads(text)["project"]["scripts"]
