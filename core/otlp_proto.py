"""Stdlib-only OTLP protobuf wire-format encoding.

Phoenix's OTLP HTTP endpoint (/v1/traces) only accepts binary protobuf, and
hooks must run on the stdlib alone (no protobuf/OTel SDK dependency), so the
OTLP JSON dicts built by build_span() are encoded to the protobuf wire format
by hand. Field numbers follow the stable OTLP v1 trace schema
(opentelemetry/proto/trace/v1/trace.proto).
"""

import base64
import struct


def _pb_varint(n: int) -> bytes:
    """Encode an unsigned varint."""
    out = bytearray()
    while True:
        bits = n & 0x7F
        n >>= 7
        if n:
            out.append(bits | 0x80)
        else:
            out.append(bits)
            return bytes(out)


def _pb_varint_field(field: int, n: int) -> bytes:
    return _pb_varint(field << 3) + _pb_varint(n & 0xFFFFFFFFFFFFFFFF)


def _pb_fixed64_field(field: int, n: int) -> bytes:
    return _pb_varint(field << 3 | 1) + struct.pack("<Q", n & 0xFFFFFFFFFFFFFFFF)


def _pb_double_field(field: int, value: float) -> bytes:
    return _pb_varint(field << 3 | 1) + struct.pack("<d", float(value))


def _pb_len_field(field: int, payload: bytes) -> bytes:
    return _pb_varint(field << 3 | 2) + _pb_varint(len(payload)) + payload


def _pb_string_field(field: int, value: str) -> bytes:
    return _pb_len_field(field, str(value).encode("utf-8"))


def _pb_any_value(value: dict) -> bytes:
    """Encode an OTLP JSON AnyValue object; unknown/malformed values encode empty."""
    if not isinstance(value, dict):
        return b""
    if "stringValue" in value:
        return _pb_string_field(1, value["stringValue"])
    if "boolValue" in value:
        return _pb_varint_field(2, 1 if value["boolValue"] else 0)
    if "intValue" in value:
        try:
            return _pb_varint_field(3, int(value["intValue"]))
        except (TypeError, ValueError):
            return b""
    if "doubleValue" in value:
        return _pb_double_field(4, value["doubleValue"])
    if "arrayValue" in value:
        values = value.get("arrayValue", {}).get("values", [])
        return _pb_len_field(5, b"".join(_pb_len_field(1, _pb_any_value(v)) for v in values))
    if "kvlistValue" in value:
        values = value.get("kvlistValue", {}).get("values", [])
        return _pb_len_field(6, _pb_key_values(1, values))
    if "bytesValue" in value:
        try:
            return _pb_len_field(7, base64.b64decode(value["bytesValue"]))
        except Exception:
            return b""
    return b""


def _pb_key_value(attr: dict) -> bytes:
    """Encode an OTLP JSON attribute {key, value} as a KeyValue message."""
    out = _pb_string_field(1, attr.get("key", ""))
    out += _pb_len_field(2, _pb_any_value(attr.get("value", {})))
    return out


def _pb_key_values(field: int, attrs: list) -> bytes:
    """Encode a repeated-KeyValue list under the given field number."""
    return b"".join(_pb_len_field(field, _pb_key_value(attr)) for attr in attrs or [])


def _pb_hex_field(field: int, hex_str: str) -> bytes:
    """Encode a hex-encoded id (traceId/spanId) as a bytes field; empty if invalid."""
    try:
        raw = bytes.fromhex(hex_str or "")
    except ValueError:
        raw = b""
    if not raw:
        return b""
    return _pb_len_field(field, raw)


def _pb_span(span: dict) -> bytes:
    """Encode an OTLP JSON span as a Span message."""
    out = _pb_hex_field(1, span.get("traceId", ""))
    out += _pb_hex_field(2, span.get("spanId", ""))
    out += _pb_hex_field(4, span.get("parentSpanId", ""))
    out += _pb_string_field(5, span.get("name", "unknown"))
    try:
        kind = int(span.get("kind", 0))
    except (TypeError, ValueError):
        kind = 0
    if kind:
        out += _pb_varint_field(6, kind)
    for field, key in ((7, "startTimeUnixNano"), (8, "endTimeUnixNano")):
        try:
            out += _pb_fixed64_field(field, int(span.get(key, "")))
        except (TypeError, ValueError):
            raise ValueError(f"Invalid Unix nanosecond timestamp for {key}: {span.get(key)!r}")
    out += _pb_key_values(9, span.get("attributes", []))
    for event in span.get("events", []) or []:
        ev = b""
        try:
            ev += _pb_fixed64_field(1, int(event.get("timeUnixNano", "")))
        except (TypeError, ValueError):
            pass
        ev += _pb_string_field(2, event.get("name", "event"))
        ev += _pb_key_values(3, event.get("attributes", []))
        out += _pb_len_field(11, ev)
    status = span.get("status") or {}
    if status:
        st = b""
        if status.get("message"):
            st += _pb_string_field(2, status["message"])
        try:
            code = int(status.get("code", 0) or 0)
        except (TypeError, ValueError):
            code = 0
        if code:
            st += _pb_varint_field(3, code)
        out += _pb_len_field(15, st)
    return out


def otlp_json_to_protobuf(span_dict: dict) -> bytes:
    """Encode an OTLP JSON payload as an ExportTraceServiceRequest protobuf."""
    out = b""
    for rs in span_dict.get("resourceSpans", []):
        rs_body = _pb_len_field(1, _pb_key_values(1, rs.get("resource", {}).get("attributes", [])))
        for ss in rs.get("scopeSpans", []):
            scope = ss.get("scope") or {}
            scope_msg = b""
            if scope.get("name"):
                scope_msg += _pb_string_field(1, scope["name"])
            if scope.get("version"):
                scope_msg += _pb_string_field(2, scope["version"])
            ss_body = _pb_len_field(1, scope_msg) if scope_msg else b""
            for span in ss.get("spans", []):
                ss_body += _pb_len_field(2, _pb_span(span))
            rs_body += _pb_len_field(2, ss_body)
        out += _pb_len_field(1, rs_body)
    return out
