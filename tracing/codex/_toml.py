"""TOML read/write helpers used by the Codex installer.

Extracted from ``install.py`` so that ``install_legacy.py`` (and any other
module) can depend on these utilities without creating an import cycle back
into ``install.py``.

The parser is intentionally lenient — falling back to a line-based parse if
the file is malformed — so install/uninstall keep working when another tool
has written ``~/.codex/config.toml`` in a slightly off-spec way.
"""

from __future__ import annotations

import re
from pathlib import Path

# Try tomllib (3.11+), then tomli, then fall back to the line parser only.
_tomllib = None
try:
    import tomllib as _tomllib  # type: ignore[no-redef]
except ImportError:
    try:
        import tomli as _tomllib  # type: ignore[no-redef]
    except ImportError:
        pass


# ---------------------------------------------------------------------------
# Load / parse
# ---------------------------------------------------------------------------


def _toml_load(path: Path) -> dict:
    """Load a TOML file into a dict. Falls back to line-based parsing.

    If the file is malformed (e.g. another tool wrote unquoted keys with
    `@` or `/`), fall back to the lenient line parser rather than crashing
    so install/uninstall can still proceed.
    """
    if not path.is_file():
        return {}
    text = path.read_text()
    if _tomllib is not None:
        try:
            return _tomllib.loads(text)
        except Exception:
            pass
    return _toml_line_parse(text)


def _toml_extract_section(line: str) -> str | None:
    """Extract the inner path from a ``[section]`` header, quote-aware.

    Returns ``None`` when *line* is not a valid section header.
    """
    if not line.startswith("[") or line.startswith("[["):
        return None
    in_quotes = False
    escape = False
    for i, ch in enumerate(line):
        if i == 0:
            continue  # skip opening '['
        if escape:
            escape = False
            continue
        if in_quotes:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_quotes = False
        else:
            if ch == '"':
                in_quotes = True
            elif ch == "]":
                if line[i + 1 :].strip() == "":
                    return line[1:i]
                return None
    return None


def _toml_split_kv(line: str) -> tuple[str, str] | None:
    """Split ``key = value`` respecting quoted keys (e.g. ``"a=b" = 'x'``).

    Returns ``(raw_key, raw_value)`` or ``None`` if the line isn't a kv pair.
    """
    in_quotes = False
    escape = False
    for i, ch in enumerate(line):
        if escape:
            escape = False
            continue
        if in_quotes:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_quotes = False
        else:
            if ch == '"':
                in_quotes = True
            elif ch == "=":
                key = line[:i].strip()
                val = line[i + 1 :].strip()
                if key:
                    return (key, val)
                return None
    return None


def _toml_array_is_complete(value: str) -> bool:
    """Return True once a TOML array's closing bracket is present.

    Brackets inside quoted strings or comments do not affect the nesting
    depth. This is intentionally small, but sufficient to join multiline
    arrays before the lenient fallback parser extracts their string values.
    """
    depth = 0
    in_double_quotes = False
    in_single_quotes = False
    in_comment = False
    escape = False

    for ch in value:
        if ch == "\n":
            in_comment = False
            continue
        if in_comment:
            continue
        if escape:
            escape = False
            continue
        if in_double_quotes:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_double_quotes = False
            continue
        if in_single_quotes:
            if ch == "'":
                in_single_quotes = False
            continue
        if ch == "#":
            in_comment = True
        elif ch == '"':
            in_double_quotes = True
        elif ch == "'":
            in_single_quotes = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1

    return depth <= 0


def _toml_line_parse(text: str) -> dict:
    """Minimal TOML parser — handles flat keys and sections for our use case."""
    result: dict = {}
    current_section: dict = result
    raw_lines = text.splitlines()
    index = 0
    while index < len(raw_lines):
        raw_line = raw_lines[index]
        index += 1
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Section header (quote-aware — handles ] inside quoted keys)
        section_inner = _toml_extract_section(line)
        if section_inner is not None:
            keys = _toml_split_key_path(section_inner)
            current_section = result
            for k in keys:
                if k not in current_section:
                    current_section[k] = {}
                current_section = current_section[k]
            continue
        # Key = value (quote-aware — handles = inside quoted keys)
        kv = _toml_split_kv(line)
        if kv:
            key = _toml_unkey(kv[0])
            val_raw = kv[1]
            # Handle array values like ["cmd"] or ['cmd']
            if val_raw.startswith("["):
                array_lines = [val_raw]
                while not _toml_array_is_complete("\n".join(array_lines)) and index < len(raw_lines):
                    array_lines.append(raw_lines[index].strip())
                    index += 1
                val_raw = "\n".join(array_lines)
                items = []
                for item in re.findall(r'"([^"]*)"|\'([^\']*)\'', val_raw):
                    items.append(item[0] or item[1])
                current_section[key] = items
            elif (val_raw.startswith('"') and val_raw.endswith('"')) or (
                val_raw.startswith("'") and val_raw.endswith("'")
            ):
                current_section[key] = val_raw[1:-1]
            elif val_raw.lower() in ("true", "false"):
                current_section[key] = val_raw.lower() == "true"
            else:
                try:
                    current_section[key] = int(val_raw)
                except ValueError:
                    current_section[key] = val_raw
    return result


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
    if _BARE_KEY_RE.fullmatch(key):
        return key

    escapes = {
        '"': '\\"',
        "\\": "\\\\",
        "\b": "\\b",
        "\t": "\\t",
        "\n": "\\n",
        "\f": "\\f",
        "\r": "\\r",
    }
    escaped: list[str] = []
    for ch in key:
        if ch in escapes:
            escaped.append(escapes[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            escaped.append(f"\\u{ord(ch):04X}")
        else:
            escaped.append(ch)
    return f'"{"".join(escaped)}"'


def _toml_unkey(key: str) -> str:
    """Inverse of _toml_key — strip quotes and unescape a TOML key."""
    if len(key) < 2:
        return key
    if key.startswith("'") and key.endswith("'"):
        return key[1:-1]
    if not (key.startswith('"') and key.endswith('"')):
        return key

    inner = key[1:-1]
    result: list[str] = []
    simple_escapes = {
        '"': '"',
        "\\": "\\",
        "b": "\b",
        "t": "\t",
        "n": "\n",
        "f": "\f",
        "r": "\r",
    }
    index = 0
    while index < len(inner):
        ch = inner[index]
        if ch != "\\" or index + 1 >= len(inner):
            result.append(ch)
            index += 1
            continue

        escape = inner[index + 1]
        if escape in simple_escapes:
            result.append(simple_escapes[escape])
            index += 2
            continue
        if escape in ("u", "U"):
            width = 4 if escape == "u" else 8
            digits = inner[index + 2 : index + 2 + width]
            if len(digits) == width and all(c in "0123456789abcdefABCDEF" for c in digits):
                try:
                    codepoint = int(digits, 16)
                    if 0xD800 <= codepoint <= 0xDFFF:
                        raise ValueError
                    result.append(chr(codepoint))
                    index += width + 2
                    continue
                except ValueError:
                    pass

        # Preserve malformed/unknown escapes for the lenient fallback parser.
        result.extend(("\\", escape))
        index += 2

    return "".join(result)


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
