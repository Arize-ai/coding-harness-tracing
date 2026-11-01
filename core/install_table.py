"""Harness installers used by ``arize-harness setup`` and ``uninstall``: one entry per ``tracing/*/install.py``.

Keys are the ``core.constants.HARNESSES`` keys, which are also the config.json
keys. Modules are named as strings, as in ``core/hook_table.py``, so looking a
harness up imports nothing; a Nuitka build reads the same names to bundle them.
``tests/core/test_cli_commands.py`` fails when an ``install.py`` is missing here.

Imported only by ``core/setup/commands.py``. Standard library only, like
``core/hook_table.py``: looking a harness up imports nothing but the modules
it names.
"""

import importlib
from types import ModuleType
from typing import Dict, NamedTuple, Optional, Tuple


class Installer(NamedTuple):
    # Module defining install() and uninstall().
    module: str
    # Module defining HARNESS_HOME and HARNESS_BIN for is_harness_installed(),
    # or None for a harness with no detection values.
    detection: Optional[str]


INSTALLERS: Dict[str, Installer] = {
    "claude-code": Installer("tracing.claude_code.install", "tracing.claude_code.constants"),
    "codex": Installer("tracing.codex.install", "tracing.codex.constants"),
    "cursor": Installer("tracing.cursor.install", "tracing.cursor.constants"),
    "copilot": Installer("tracing.copilot.install", None),
    "gemini": Installer("tracing.gemini.install", None),
    "antigravity": Installer("tracing.antigravity.install", None),
    "opencode": Installer("tracing.opencode.install", "tracing.opencode.constants"),
    "omp": Installer("tracing.omp.install", "tracing.omp.constants"),
    "kiro": Installer("tracing.kiro.install", "tracing.kiro.constants"),
    "devin": Installer("tracing.devin.install", "tracing.devin.constants"),
}

# install.sh's name for Claude Code.
ALIASES: Dict[str, str] = {
    "claude": "claude-code",
}


def resolve(name: str) -> Optional[str]:
    """The ``INSTALLERS`` key for ``name`` (aliases resolved), or None if it names no harness."""
    key = ALIASES.get(name, name)
    return key if key in INSTALLERS else None


def load(key: str) -> ModuleType:
    """Import the installer module for ``key``."""
    return importlib.import_module(INSTALLERS[key].module)


def detection_values(key: str) -> Optional[Tuple[Optional[str], Optional[str]]]:
    """``(HARNESS_HOME, HARNESS_BIN)`` for ``key``, or None when it has no detection values."""
    module_name = INSTALLERS[key].detection
    if module_name is None:
        return None
    module = importlib.import_module(module_name)
    return getattr(module, "HARNESS_HOME", None), getattr(module, "HARNESS_BIN", None)
