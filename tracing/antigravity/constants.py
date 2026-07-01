"""Constants for the Antigravity tracing harness installer.

Antigravity uses a single global hooks file at ``~/.gemini/config/hooks.json``
(distinct from Gemini's ``~/.gemini/settings.json``). The schema is inverted
relative to Gemini: the top level maps ``hookName -> { event -> [handlers] }``,
so each installed hook block owns its own event-to-handlers map. We write our
handlers under the ``arize-tracing`` top-level key.
"""

from __future__ import annotations

from pathlib import Path

HARNESS_NAME = "antigravity"

# Antigravity's global customization dir. The hooks file lives here, NOT at
# ~/.gemini/settings.json (which is Gemini's territory).
SETTINGS_DIR = Path.home() / ".gemini" / "config"
SETTINGS_FILE = SETTINGS_DIR / "hooks.json"

# The top-level key we own inside hooks.json. Used by both install() (to write)
# and uninstall() (to identify entries to remove).
HOOK_NAME = "arize-tracing"

# Map of Antigravity event name -> CLI entry-point script name. These entry-
# point names are registered in pyproject.toml [project.scripts] in the
# wire-entry-points task. Order is preserved when writing hooks.json.
EVENTS: dict[str, str] = {
    "PreInvocation": "arize-hook-antigravity-pre-invocation",
    "Stop": "arize-hook-antigravity-stop",
}

# Default per-hook timeout in seconds (Antigravity's unit; default is 30).
HOOK_TIMEOUT_SECONDS = 30
