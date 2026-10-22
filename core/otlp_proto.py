"""Stdlib-only OTLP protobuf wire-format encoding.

Phoenix's OTLP HTTP endpoint (/v1/traces) only accepts binary protobuf, and
hooks must run on the stdlib alone (no protobuf/OTel SDK dependency), so the
OTLP JSON dicts built by build_span() are encoded to the protobuf wire format
by hand. Field numbers follow the stable OTLP v1 trace schema
(opentelemetry/proto/trace/v1/trace.proto):
https://github.com/open-telemetry/opentelemetry-proto/blob/main/opentelemetry/proto/trace/v1/trace.proto

The module is layered bottom-up:

1. Wire-format primitives — one encoder per protobuf wire type.
2. OTLP value encoders — OTLP JSON leaf values (AnyValue, KeyValue,
   hex-encoded ids, Unix-nanosecond timestamps).
3. Trace message encoders — Span and Span.Link messages.
4. Public API — otlp_json_to_protobuf(), the only intended entry point.

Malformed *optional* values encode to nothing (fail-soft: a bad attribute
should not drop the span), while malformed *required* values (ids,
timestamps) raise ValueError so send_span() cannot report success for a span
the backend would orphan or reject.
"""

import base64
import struct

# ---------------------------------------------------------------------------
# Wire-format primitives — one encoder per protobuf wire type
# ---------------------------------------------------------------------------


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
    """Encode a varint field (wire type 0), masking the value to 64 bits."""
    return _pb_varint(field << 3) + _pb_varint(n & 0xFFFFFFFFFFFFFFFF)


def _pb_fixed64_field(field: int, n: int) -> bytes:
    """Encode a little-endian fixed64 field (wire type 1)."""
    return _pb_varint(field << 3 | 1) + struct.pack("<Q", n & 0xFFFFFFFFFFFFFFFF)


def _pb_fixed32_field(field: int, n: int) -> bytes:
    """Encode a little-endian fixed32 field (wire type 5)."""
    return _pb_varint(field << 3 | 5) + struct.pack("<I", n & 0xFFFFFFFF)


def _pb_double_field(field: int, value: float) -> bytes:
    """Encode an IEEE-754 double field (wire type 1)."""
    return _pb_varint(field << 3 | 1) + struct.pack("<d", float(value))


def _pb_len_field(field: int, payload: bytes) -> bytes:
    """Encode a length-delimited field (wire type 2): tag, length, payload."""
    return _pb_varint(field << 3 | 2) + _pb_varint(len(payload)) + payload


def _pb_string_field(field: int, value: str) -> bytes:
    """Encode a UTF-8 string as a length-delimited field."""
    return _pb_len_field(field, str(value).encode("utf-8"))


# ---------------------------------------------------------------------------
# OTLP value encoders — OTLP JSON leaf values (fail-soft on optional fields,
# ValueError on required ids/timestamps)
# ---------------------------------------------------------------------------


def _pb_uint_field(field: int, value) -> bytes:
    """Encode an optional non-negative int field; zero/missing/malformed encode nothing."""
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        n = 0
    return _pb_varint_field(field, n) if n else b""


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
        try:
            return _pb_double_field(4, value["doubleValue"])
        except (TypeError, ValueError):
            return b""
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


def _pb_hex_field(field: int, hex_str, label: str, required: bool = False) -> bytes:
    """Encode a hex-encoded id (traceId/spanId) as a bytes field.

    Raises ValueError on a non-string or non-hex id, and on a missing/empty id
    when required — a silently omitted id would orphan the span (all-zero
    trace) or promote it to a trace root while send_span reports success.
    """
    try:
        raw = bytes.fromhex(hex_str or "")
    except (TypeError, ValueError):
        raise ValueError(f"Invalid hex {label}: {hex_str!r}")
    if not raw:
        if required:
            raise ValueError(f"Missing {label}")
        return b""
    return _pb_len_field(field, raw)


def _pb_time_field(field: int, value, label: str) -> bytes:
    """Encode a Unix-nanosecond timestamp as a fixed64 field.

    Raises ValueError on a missing or non-integer timestamp — OTLP requires
    both span timestamps, and a silently defaulted one would corrupt durations.
    """
    try:
        return _pb_fixed64_field(field, int(value if value is not None else ""))
    except (TypeError, ValueError):
        raise ValueError(f"Invalid Unix nanosecond timestamp for {label}: {value!r}")


# ---------------------------------------------------------------------------
# Trace message encoders — Span and Span.Link
# ---------------------------------------------------------------------------


def _pb_link(link: dict) -> bytes:
    """Encode an OTLP JSON span link as a Span.Link message."""
    out = _pb_hex_field(1, link.get("traceId", ""), "link traceId", required=True)
    out += _pb_hex_field(2, link.get("spanId", ""), "link spanId", required=True)
    if link.get("traceState"):
        out += _pb_string_field(3, link["traceState"])
    out += _pb_key_values(4, link.get("attributes", []))
    out += _pb_uint_field(5, link.get("droppedAttributesCount"))
    if link.get("flags"):
        out += _pb_fixed32_field(6, int(link["flags"]))
    return out


def _pb_span(span: dict) -> bytes:
    """Encode an OTLP JSON span as a Span message.

    Events and Status are encoded inline (they have no other callers); links
    delegate to _pb_link. Raises ValueError via _pb_hex_field/_pb_time_field
    on missing or malformed ids and timestamps.
    """
    out = _pb_hex_field(1, span.get("traceId", ""), "traceId", required=True)
    out += _pb_hex_field(2, span.get("spanId", ""), "spanId", required=True)
    if span.get("traceState"):
        out += _pb_string_field(3, span["traceState"])
    out += _pb_hex_field(4, span.get("parentSpanId", ""), "parentSpanId")
    out += _pb_string_field(5, span.get("name", "unknown"))
    try:
        kind = int(span.get("kind", 0))
    except (TypeError, ValueError):
        kind = 0
    if kind:
        out += _pb_varint_field(6, kind)
    out += _pb_time_field(7, span.get("startTimeUnixNano"), "startTimeUnixNano")
    out += _pb_time_field(8, span.get("endTimeUnixNano"), "endTimeUnixNano")
    out += _pb_key_values(9, span.get("attributes", []))
    out += _pb_uint_field(10, span.get("droppedAttributesCount"))
    for event in span.get("events", []) or []:
        ev = _pb_time_field(1, event.get("timeUnixNano"), "event timeUnixNano")
        ev += _pb_string_field(2, event.get("name", "event"))
        ev += _pb_key_values(3, event.get("attributes", []))
        ev += _pb_uint_field(4, event.get("droppedAttributesCount"))
        out += _pb_len_field(11, ev)
    out += _pb_uint_field(12, span.get("droppedEventsCount"))
    for link in span.get("links", []) or []:
        out += _pb_len_field(13, _pb_link(link))
    out += _pb_uint_field(14, span.get("droppedLinksCount"))
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
    if span.get("flags"):
        out += _pb_fixed32_field(16, int(span["flags"]))
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def otlp_json_to_protobuf(span_dict: dict) -> bytes:
    """Encode an OTLP JSON payload as an ExportTraceServiceRequest protobuf.

    Walks the resourceSpans → scopeSpans → spans hierarchy, encoding
    Resource and InstrumentationScope messages inline. Raises ValueError on
    spans with missing/malformed ids or timestamps (see _pb_hex_field and
    _pb_time_field); everything else encodes fail-soft.
    """
    out = b""
    for rs in span_dict.get("resourceSpans", []):
        resource = rs.get("resource", {})
        resource_msg = _pb_key_values(1, resource.get("attributes", []))
        resource_msg += _pb_uint_field(2, resource.get("droppedAttributesCount"))
        rs_body = _pb_len_field(1, resource_msg)
        for ss in rs.get("scopeSpans", []):
            scope = ss.get("scope") or {}
            scope_msg = b""
            if scope.get("name"):
                scope_msg += _pb_string_field(1, scope["name"])
            if scope.get("version"):
                scope_msg += _pb_string_field(2, scope["version"])
            scope_msg += _pb_key_values(3, scope.get("attributes", []))
            scope_msg += _pb_uint_field(4, scope.get("droppedAttributesCount"))
            ss_body = _pb_len_field(1, scope_msg) if scope_msg else b""
            for span in ss.get("spans", []):
                ss_body += _pb_len_field(2, _pb_span(span))
            if ss.get("schemaUrl"):
                ss_body += _pb_string_field(3, ss["schemaUrl"])
            rs_body += _pb_len_field(2, ss_body)
        if rs.get("schemaUrl"):
            rs_body += _pb_string_field(3, rs["schemaUrl"])
        out += _pb_len_field(1, rs_body)
    return out
