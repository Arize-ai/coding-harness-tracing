"""Shared ``[project.scripts]`` parser for tests that check entry points."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"


def parse_project_scripts(text: str) -> dict[str, str]:
    """Parse the ``[project.scripts]`` table from pyproject text without tomllib.

    ``tomllib`` is stdlib only on Python 3.11+, but this repo targets ``>=3.9``,
    so we parse the simple ``name = "value"`` table by hand. Values may use
    either TOML string quote (basic ``"..."`` or literal ``'...'``). Entry-point
    names are TOML bare keys (letters, digits, ``_``, ``-``, ``.``).

    Any other line in the table raises ``ValueError`` rather than being skipped,
    so an entry this parser can't read never goes unseen by the parity tests.
    """
    scripts: dict[str, str] = {}
    in_table = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            in_table = line == "[project.scripts]"
            continue
        if not in_table:
            continue
        m = re.match(r"""^([A-Za-z0-9_.\-]+)\s*=\s*(["'])([^"']+)\2\s*(#.*)?$""", line)
        if not m:
            raise ValueError(f"unparsed [project.scripts] line: {raw!r}")
        scripts[m.group(1)] = m.group(3)
    return scripts
