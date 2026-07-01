"""Constants for the omp (Oh My Pi) tracing harness installer."""

from __future__ import annotations

from pathlib import Path

HARNESS_NAME = "omp"
DISPLAY_NAME = "Oh My Pi (omp)"

# omp config root + extension registration (per https://omp.sh/docs/hooks).
OMP_CONFIG_DIR = Path.home() / ".omp"

# omp settings live in ~/.omp/agent/settings.json. The `extensions` array there
# is an explicit list of file/dir paths omp loads (no auto-discovery), so the
# shim's absolute path must be registered there on install.
SETTINGS_DIR = OMP_CONFIG_DIR / "agent"
SETTINGS_FILE = SETTINGS_DIR / "settings.json"

# The shim itself is file-dropped into ~/.omp/extensions/.
EXTENSIONS_DIR = OMP_CONFIG_DIR / "extensions"
PLUGIN_FILE = EXTENSIONS_DIR / "arize-tracing.ts"

# Repo-shipped source asset copied on install.
PLUGIN_SOURCE = Path(__file__).parent / "plugin" / "arize-tracing.ts"

# Soft install detection (presence check + binary lookup fallback).
HARNESS_HOME = ".omp"
HARNESS_BIN = "omp"
