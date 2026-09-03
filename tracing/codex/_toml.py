"""TOML read/write helpers used by the Codex installer.

Extracted from ``install.py`` so that ``install_legacy.py`` (and any other
module) can depend on these utilities without creating an import cycle back
into ``install.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

_tomllib = None
try:
    import tomllib as _tomllib  # type: ignore[no-redef]
except ImportError:
    try:
        import tomli as _tomllib  # type: ignore[no-redef]
    except ImportError:
        pass

_ARIZE_OTLP_ENDPOINT_RE = re.compile(r"^https?://127\.0\.0\.1:\d+/v1/logs$")


def _is_arize_owned_otlp_exporter(table: object) -> bool:
    """Return True only if *table* is provably an Arize-written OTLP exporter."""
    if not isinstance(table, dict):
        return False
    if set(table.keys()) - {"endpoint", "protocol"}:
        return False
    endpoint = table.get("endpoint")
    if not (isinstance(endpoint, str) and _ARIZE_OTLP_ENDPOINT_RE.match(endpoint)):
        return False
    return table.get("protocol") == "json"


_ARIZE_OTLP_HEADER = "[otel.exporter.otlp-http]"


def _toml_owned_exporter_span(text: str, endpoint: str) -> tuple[int, int] | None:
    """Locate the literal, canonical Arize ``[otel.exporter.otlp-http]`` table.

    Arize only ever writes this table one way: a bare header line, followed
    by exactly two body lines — ``endpoint = "<endpoint>"`` and
    ``protocol = "json"`` with no other content before the next table header or
    EOF. Any other shape is not something we can safely locate and edit, so this
    returns None for all of those instead of guessing.
    """
    lines = text.splitlines(keepends=True)
    expected = {f'endpoint = "{endpoint}"', 'protocol = "json"'}

    matches: list[tuple[int, int]] = []
    for start, line in enumerate(lines):
        if line.strip() != _ARIZE_OTLP_HEADER:
            continue
        end = start + 1
        body_end = start
        body: list[str] = []
        while end < len(lines) and not lines[end].lstrip().startswith("["):
            stripped = lines[end].strip()
            if stripped and not stripped.startswith("#"):
                body.append(stripped)
                body_end = end + 1
            end += 1
        if len(body) == 2 and set(body) == expected:
            matches.append((start, body_end))

    if len(matches) != 1:
        return None
    return matches[0]


def _toml_load_strict(path: Path) -> dict:
    """Load TOML without falling back to a lossy parser.

    Write paths use this guard before serializing a config back to disk.  A
    lenient parse can omit or misinterpret user-owned data, so malformed TOML
    must be left untouched instead of being rewritten from a partial dict.
    """
    if not path.is_file():
        return {}
    if _tomllib is None:
        raise ValueError(f"Cannot validate TOML without a TOML parser: {path}")
    try:
        return _tomllib.loads(path.read_text())
    except Exception as exc:
        raise ValueError(f"Malformed TOML in {path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def _toml_write(data: dict, path: Path) -> None:
    """Write a dict as TOML. Hand-rolled — no tomli-w dependency."""
    lines: list[str] = []
    _toml_write_section(data, [], lines)
    path.write_text("\n".join(lines) + "\n")


_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_key(key: str) -> str:
    """Quote a TOML key if it contains characters not allowed in bare keys."""
    if _BARE_KEY_RE.match(key):
        return key
    escaped = key.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_unkey(key: str) -> str:
    """Inverse of _toml_key — strip quotes and unescape a TOML key."""
    if len(key) >= 2 and key.startswith('"') and key.endswith('"'):
        inner = key[1:-1]
        inner = inner.replace('\\"', '"')
        inner = inner.replace("\\\\", "\\")
        return inner
    return key


def _toml_split_key_path(path: str) -> list[str]:
    """Split a dotted TOML key path respecting quoted segments.

    Examples:
        'a.b.c' -> ['a', 'b', 'c']
        'mcp_servers."@scope/server"' -> ['mcp_servers', '@scope/server']
        'mcp_servers."a.b.c"' -> ['mcp_servers', 'a.b.c']
    """
    segments: list[str] = []
    buf: list[str] = []
    in_quotes = False
    escape = False
    for ch in path:
        if escape:
            buf.append(ch)
            escape = False
            continue
        if in_quotes:
            if ch == "\\":
                buf.append(ch)
                escape = True
            elif ch == '"':
                buf.append(ch)
                in_quotes = False
            else:
                buf.append(ch)
        else:
            if ch == '"':
                buf.append(ch)
                in_quotes = True
            elif ch == ".":
                segments.append(_toml_unkey("".join(buf).strip()))
                buf = []
            else:
                buf.append(ch)
    # Flush remaining buffer
    segments.append(_toml_unkey("".join(buf).strip()))
    return segments


def _toml_write_section(data: dict, prefix: list[str], lines: list[str]) -> None:
    """Recursively write TOML sections."""
    # Pass 1: simple scalars and arrays of scalars.
    for key, val in data.items():
        if isinstance(val, dict) or _is_table_array(val):
            continue
        _toml_write_value(key, val, lines)

    # Pass 2: arrays-of-tables → emit [[prefix.key]] for each element.
    for key, val in data.items():
        if not _is_table_array(val):
            continue
        section_path = prefix + [key]
        header = f"[[{'.'.join(_toml_key(k) for k in section_path)}]]"
        for table in val:
            if lines and lines[-1] != "":
                lines.append("")
            lines.append(header)
            _toml_write_table_body(table, lines)

    # Pass 3: nested dict sections.
    for key, val in data.items():
        if not isinstance(val, dict):
            continue
        section_path = prefix + [key]
        # Emit [section] header only when there are direct scalars to anchor
        # (or the table is empty). If all children are dicts/table-arrays we
        # skip the header and let those nested writers emit their own headers.
        has_scalars = any(not isinstance(v, dict) and not _is_table_array(v) for v in val.values())
        if has_scalars or not val:
            if lines and lines[-1] != "":
                lines.append("")
            lines.append(f"[{'.'.join(_toml_key(k) for k in section_path)}]")
        _toml_write_section(val, section_path, lines)


def _is_table_array(val: object) -> bool:
    """Return True if val is a list whose elements are all dicts."""
    return isinstance(val, list) and len(val) > 0 and all(isinstance(v, dict) for v in val)


def _toml_write_table_body(table: dict, lines: list[str]) -> None:
    """Write a dict as the body of a ``[[section]]`` entry.

    Nested dicts render as inline tables; arrays of dicts render as arrays of
    inline tables. Scalars and arrays of scalars use the standard writer.
    """
    for key, val in table.items():
        if isinstance(val, dict):
            lines.append(f"{_toml_key(key)} = {_inline_table(val)}")
        elif _is_table_array(val):
            elems = ", ".join(_inline_table(d) for d in val)
            lines.append(f"{_toml_key(key)} = [{elems}]")
        else:
            _toml_write_value(key, val, lines)


def _inline_table(table: dict) -> str:
    """Render a dict as a TOML inline table: ``{ k = v, k2 = v2 }``."""
    parts: list[str] = []
    for k, v in table.items():
        kk = _toml_key(k)
        if isinstance(v, dict):
            parts.append(f"{kk} = {_inline_table(v)}")
        elif isinstance(v, bool):
            parts.append(f"{kk} = {'true' if v else 'false'}")
        elif isinstance(v, int):
            parts.append(f"{kk} = {v}")
        elif isinstance(v, float):
            parts.append(f"{kk} = {v!r}")
        elif isinstance(v, list):
            if _is_table_array(v):
                items = ", ".join(_inline_table(d) for d in v)
            else:
                items = ", ".join(_toml_string_literal(item) for item in v)
            parts.append(f"{kk} = [{items}]")
        else:
            parts.append(f"{kk} = {_toml_string_literal(v)}")
    return "{ " + ", ".join(parts) + " }"


def _toml_write_value(key: str, val: object, lines: list[str]) -> None:
    """Write a single TOML key-value pair (scalars and arrays of scalars only)."""
    k = _toml_key(key)
    if isinstance(val, list):
        items = ", ".join(_toml_string_literal(v) for v in val)
        lines.append(f"{k} = [{items}]")
    elif isinstance(val, bool):
        lines.append(f"{k} = {'true' if val else 'false'}")
    elif isinstance(val, int):
        lines.append(f"{k} = {val}")
    elif isinstance(val, float):
        lines.append(f"{k} = {val!r}")
    else:
        lines.append(f"{k} = {_toml_string_literal(val)}")


def _toml_string_literal(val: object) -> str:
    """Render a string as a TOML literal '...' — no escape handling needed,
    which matches `_toml_line_parse` semantics and is safe for Windows paths
    with backslashes. Falls back to an escaped basic string if the value
    contains a single quote or newline (which literal strings cannot carry).
    """
    s = str(val)
    if "'" in s or "\n" in s or "\r" in s:
        escaped = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
        return f'"{escaped}"'
    return f"'{s}'"
