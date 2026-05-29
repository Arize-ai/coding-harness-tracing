#!/usr/bin/env python3
"""
Config helper for Arize Coding Harness Tracing.

Reads and writes ~/.arize/harness/config.yaml. Imported by other Python
modules. The user-facing CLI for editing this file is `acht config`
(see cmd/acht/config.go) — there is no Python CLI entry point.

Top-level keys:
  harnesses: dict of per-harness entries (target/endpoint/api_key/project_name/...)
  logging:   dict of content-logging toggles (prompts, tool_details, tool_content)
  user_id:   optional string identifying the user across harnesses
  verbose:   bool — when true, hook handlers print trace summaries to stderr.
             ARIZE_VERBOSE env var takes precedence over this key.
"""

import os
import sys

try:
    import yaml
except ImportError:
    # When running from venv, yaml should be available.
    # Provide a clear error if not.
    sys.stderr.write("error: PyYAML not installed. Install it in the collector venv.\n")
    sys.exit(1)

from core.constants import CONFIG_FILE

# --- Python API ---


def load_config(config_path=None):
    """Load and return the config dict from the YAML file.

    Returns an empty dict if the file does not exist.
    Raises ValueError on malformed YAML or non-mapping content.
    """
    path = config_path or CONFIG_FILE
    if not os.path.isfile(path):
        return {}
    with open(path, "r") as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ValueError(f"Malformed YAML in {path}: {e}")
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"Config file is not a YAML mapping: {path}")
    return data


def get_value(config, dotted_key):
    """Traverse a nested dict using a dotted key path.

    Returns None if any part of the path is missing.
    """
    keys = dotted_key.split(".")
    current = config
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
        if current is None:
            return None
    return current


def set_value(config, dotted_key, value):
    """Set a value at a dotted key path, creating intermediate dicts as needed.

    Modifies config in place and returns it.
    """
    keys = dotted_key.split(".")
    current = config
    for key in keys[:-1]:
        if key not in current or not isinstance(current.get(key), dict):
            current[key] = {}
        current = current[key]
    current[keys[-1]] = value
    return config


def delete_value(config, dotted_key):
    """Delete a key at a dotted key path. No-op if key doesn't exist.

    Modifies config in place and returns it.
    """
    keys = dotted_key.split(".")
    current = config
    for key in keys[:-1]:
        if not isinstance(current, dict) or key not in current:
            return config
        current = current[key]
    if isinstance(current, dict):
        current.pop(keys[-1], None)
    return config


def save_config(config, config_path=None):
    """Write config dict to the YAML file with chmod 600."""
    path = config_path or CONFIG_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)
