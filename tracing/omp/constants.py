"""Constants for the omp (Oh My Pi) tracing harness installer."""

from __future__ import annotations

from pathlib import Path

HARNESS_NAME = "omp"
DISPLAY_NAME = "Oh My Pi (omp)"

# omp config root + extension registration (per https://omp.sh/docs/hooks).
OMP_CONFIG_DIR = Path.home() / ".omp"
SETTINGS_DIR = OMP_CONFIG_DIR / "agent"
SETTINGS_FILE = SETTINGS_DIR / "settings.json"  # has an "extensions": [paths] array
EXTENSIONS_DIR = OMP_CONFIG_DIR / "extensions"
PLUGIN_FILE = EXTENSIONS_DIR / "arize-tracing.ts"  # where the installer drops the shim

# Repo-shipped source asset copied on install:
PLUGIN_SOURCE = Path(__file__).parent / "plugin" / "arize-tracing.ts"

# Soft install detection:
HARNESS_HOME = ".omp"
HARNESS_BIN = "omp"
