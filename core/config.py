#!/usr/bin/env python3
"""
Config helper for Arize Coding Harness Tracing.

Reads and writes ~/.arize/harness/config.json.
Used by shell scripts (via CLI subcommands) and Python modules (via import).

CLI usage:
    python3 core/config.py get <dotted.key>
    python3 core/config.py set <dotted.key> <value>
    python3 core/config.py delete <dotted.key>
    python3 core/config.py write          # reads JSON from stdin
    python3 core/config.py dump
    python3 core/config.py exists
    python3 core/config.py migrate        # convert legacy config.yaml -> config.json
"""

import json
import os
import sys

from core.constants import CONFIG_FILE, CONFIG_FILE_YAML

# --- Python API ---


def load_config(config_path=None):
    """Load and return the config dict from the JSON file.

    Returns an empty dict if the file does not exist.
    Raises ValueError on malformed JSON or non-mapping content.
    """
    path = config_path or CONFIG_FILE
    if not os.path.isfile(path):
        return {}
    with open(path, "r") as f:
        text = f.read()
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Malformed JSON in {path}: {e}")
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"Config file is not a JSON mapping: {path}")
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
    """Write config dict to the JSON file with chmod 600."""
    path = config_path or CONFIG_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(config, f, indent=2)


def _coerce_scalar(raw):
    """Coerce a scalar YAML value (as written by yaml.safe_dump) to a Python type.

    Quoted values are always strings; unquoted values are typed like YAML would
    type them (bools, null, int, float, otherwise the raw string).
    """
    raw = raw.strip()

    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        inner = raw[1:-1]
        return inner.replace('\\"', '"').replace("\\\\", "\\")
    if len(raw) >= 2 and raw[0] == "'" and raw[-1] == "'":
        inner = raw[1:-1]
        return inner.replace("''", "'")

    lowered = raw.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if raw == "" or raw == "~" or lowered == "null":
        return None
    if _is_int(raw):
        return int(raw)
    if _is_float(raw):
        return float(raw)
    return raw


def _is_int(raw):
    """True if raw matches ^-?\\d+$ (an optionally-signed integer)."""
    digits = raw[1:] if raw[:1] == "-" else raw
    return digits.isdigit() and digits != ""


def _is_float(raw):
    """True if raw matches ^-?\\d+\\.\\d+$ (a simple signed decimal)."""
    body = raw[1:] if raw[:1] == "-" else raw
    left, dot, right = body.partition(".")
    return bool(dot) and left.isdigit() and right.isdigit()


def _strip_key_quotes(key):
    """Strip one matching pair of surrounding quotes from a mapping key."""
    key = key.strip()
    if len(key) >= 2 and key[0] == key[-1] and key[0] in ("'", '"'):
        return key[1:-1]
    return key


def parse_yaml_config(text):
    """Parse block-style YAML restricted to nested mappings of scalars.

    Only the bounded schema written by ``yaml.safe_dump(..., default_flow_style=
    False, sort_keys=False)`` is supported: block style, 2-space indentation, no
    lists, no flow collections, no anchors, no multi-line/folded scalars. Each
    value is either a nested mapping or a scalar leaf.
    """
    root = {}
    # Stack of (indent, mapping) pairs; the mapping is where keys at that indent
    # (i.e. deeper than the parent) get assigned.
    stack = [(-1, root)]

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped[0] == "#":
            continue

        indent = len(line) - len(line.lstrip(" "))

        # Pop back to the parent that owns this indentation level.
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()

        parent = stack[-1][1]

        key_part, sep, value_part = line.strip().partition(":")
        key = _strip_key_quotes(key_part)

        if sep and value_part.strip() == "":
            # Key opens a child mapping.
            child = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _coerce_scalar(value_part)

    return root


def migrate_yaml_config(yaml_path=None, json_path=None):
    """Convert a legacy config.yaml to config.json, backing up the original.

    Idempotent and fail-soft. Returns True only when a migration was performed:

    - No-op (returns False) if the JSON config already exists (never clobber it)
      or if no legacy YAML file exists.
    - If parsing fails, the original YAML is left untouched, no JSON is written,
      a warning is emitted to stderr, and False is returned.
    - On success the parsed config is written via ``save_config`` (chmod 0o600)
      and the original is renamed to ``<yaml_path>.bak``.
    """
    yaml_path = yaml_path or CONFIG_FILE_YAML
    json_path = json_path or CONFIG_FILE

    if os.path.isfile(json_path):
        return False
    if not os.path.isfile(yaml_path):
        return False

    try:
        with open(yaml_path, "r") as f:
            text = f.read()
        data = parse_yaml_config(text)
    except Exception as e:
        sys.stderr.write(f"warning: skipped migrating {yaml_path}: {e}; original left untouched\n")
        return False

    save_config(data, str(json_path))
    os.rename(str(yaml_path), str(yaml_path) + ".bak")
    return True


# --- CLI helpers ---


def _parse_value(raw):
    """Auto-detect type for CLI set values.

    Integers become ints, true/false become bools, everything else stays a string.
    """
    if raw.lower() == "true":
        return True
    if raw.lower() == "false":
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    return raw


def _format_output(value):
    """Format a value for stdout.

    Scalars are printed raw; dicts and lists are JSON-encoded.
    """
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


# --- CLI entrypoint ---


def main():
    if len(sys.argv) < 2:
        sys.stderr.write("usage: config.py <get|set|delete|write|dump|exists|migrate> [args...]\n")
        sys.exit(1)

    command = sys.argv[1]

    if command == "exists":
        sys.exit(0 if os.path.isfile(CONFIG_FILE) else 1)

    if command == "migrate":
        if migrate_yaml_config():
            print("Migrated config.yaml -> config.json")
        sys.exit(0)

    if command == "get":
        if len(sys.argv) < 3:
            sys.stderr.write("usage: config.py get <dotted.key>\n")
            sys.exit(1)
        try:
            config = load_config()
        except ValueError as e:
            sys.stderr.write(f"error: {e}\n")
            sys.exit(1)
        value = get_value(config, sys.argv[2])
        output = _format_output(value)
        if output:
            print(output)
        sys.exit(0)

    if command == "set":
        if len(sys.argv) < 4:
            sys.stderr.write("usage: config.py set <dotted.key> <value>\n")
            sys.exit(1)
        try:
            config = load_config()
        except ValueError as e:
            sys.stderr.write(f"error: {e}\n")
            sys.exit(1)
        value = _parse_value(sys.argv[3])
        set_value(config, sys.argv[2], value)
        save_config(config)
        sys.exit(0)

    if command == "delete":
        if len(sys.argv) < 3:
            sys.stderr.write("usage: config.py delete <dotted.key>\n")
            sys.exit(1)
        try:
            config = load_config()
        except ValueError as e:
            sys.stderr.write(f"error: {e}\n")
            sys.exit(1)
        delete_value(config, sys.argv[2])
        save_config(config)
        sys.exit(0)

    if command == "write":
        text = sys.stdin.read()
        if not text.strip():
            save_config({})
            sys.exit(0)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            sys.stderr.write(f"error: invalid JSON on stdin: {e}\n")
            sys.exit(1)
        if data is None:
            data = {}
        if not isinstance(data, dict):
            sys.stderr.write("error: stdin JSON must be a mapping\n")
            sys.exit(1)
        save_config(data)
        sys.exit(0)

    if command == "dump":
        try:
            config = load_config()
        except ValueError as e:
            sys.stderr.write(f"error: {e}\n")
            sys.exit(1)
        print(json.dumps(config, indent=2))
        sys.exit(0)

    sys.stderr.write(f"error: unknown command '{command}'\n")
    sys.exit(1)


if __name__ == "__main__":
    main()
