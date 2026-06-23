"""Constants for the opencode tracing harness installer."""

from __future__ import annotations

from pathlib import Path

HARNESS_NAME = "opencode"
DISPLAY_NAME = "opencode"

# opencode global plugin dir (auto-discovered). Per docs: ~/.config/opencode/plugin/
OPENCODE_CONFIG_DIR = Path.home() / ".config" / "opencode"
PLUGIN_DIR = OPENCODE_CONFIG_DIR / "plugin"
PLUGIN_FILE = PLUGIN_DIR / "arize-tracing.ts"

# Repo-shipped source asset copied on install.
PLUGIN_SOURCE = Path(__file__).parent / "plugin" / "arize-tracing.ts"

# Soft install detection (presence check + binary lookup fallback).
HARNESS_HOME = ".config/opencode"
HARNESS_BIN = "opencode"
