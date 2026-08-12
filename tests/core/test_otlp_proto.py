#!/usr/bin/env python3
"""Tests for core.otlp_proto — stdlib OTLP protobuf wire-format encoding."""

import struct

import pytest

from core.otlp_proto import otlp_json_to_protobuf

# ── Minimal protobuf wire-format decoder (test helper) ────────────────────


def _pb_read_varint(data, i):
    result = shift = 0
    while True:
        byte = data[i]
        result |= (byte & 0x7F) << shift
        i += 1
        if not byte & 0x80:
            return result, i
        shift += 7


def _pb_decode(data):
    """Decode a protobuf message into {field: [values]} (varint ints, raw bytes otherwise)."""
    fields = {}
    i = 0
    while i < len(data):
        tag, i = _pb_read_varint(data, i)
        field, wire_type = tag >> 3, tag & 7
        if wire_type == 0:
            value, i = _pb_read_varint(data, i)
        elif wire_type == 1:
            value = data[i : i + 8]
            i += 8
        elif wire_type == 2:
            length, i = _pb_read_varint(data, i)
            value = data[i : i + length]
            i += length
        else:
            raise ValueError(f"unsupported wire type {wire_type}")
        fields.setdefault(field, []).append(value)
    return fields


def _pb_fixed64_int(raw):
    return struct.unpack("<Q", raw)[0]


def _pb_double_val(raw):
    return struct.unpack("<d", raw)[0]


def _pb_attrs(raw_key_values):
    """Decode repeated KeyValue messages into {key bytes: decoded AnyValue fields}."""
    attrs = {}
    for raw in raw_key_values:
        kv = _pb_decode(raw)
        attrs[kv[1][0]] = _pb_decode(kv[2][0])
    return attrs


# ── OTLP protobuf encoding tests ──────────────────────────────────────────


class TestOtlpProtobufEncoding:
    def test_encodes_span_fields(self):
        payload = {
            "resourceSpans": [
                {
                    "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "svc"}}]},
                    "scopeSpans": [
                        {
                            "scope": {"name": "scope"},
                            "spans": [
                                {
                                    "traceId": "0123456789abcdef0123456789abcdef",
                                    "spanId": "abcdef1234567890",
                                    "parentSpanId": "1122334455667788",
                                    "name": "tool-call",
                                    "kind": 1,
                                    "startTimeUnixNano": "1000000000",
                                    "endTimeUnixNano": "1500000000",
                                    "attributes": [
                                        {"key": "openinference.span.kind", "value": {"stringValue": "TOOL"}},
                                        {"key": "count", "value": {"intValue": "3"}},
                                        {"key": "ok", "value": {"boolValue": True}},
                                        {"key": "score", "value": {"doubleValue": 0.5}},
                                    ],
                                    "events": [
                                        {
                                            "name": "exception",
                                            "timeUnixNano": "1250000000",
                                            "attributes": [{"key": "message", "value": {"stringValue": "boom"}}],
                                        }
                                    ],
                                    "status": {"code": 2, "message": "failed"},
                                }
                            ],
                        }
                    ],
                }
            ]
        }

        encoded = otlp_json_to_protobuf(payload)

        # ExportTraceServiceRequest.resource_spans[0]
        rs = _pb_decode(_pb_decode(encoded)[1][0])
        resource_attrs = _pb_attrs(_pb_decode(rs[1][0])[1])
        assert resource_attrs[b"service.name"][1][0] == b"svc"

        ss = _pb_decode(rs[2][0])
        assert _pb_decode(ss[1][0])[1][0] == b"scope"

        span = _pb_decode(ss[2][0])
        assert span[1][0] == bytes.fromhex("0123456789abcdef0123456789abcdef")
        assert span[2][0] == bytes.fromhex("abcdef1234567890")
        assert span[4][0] == bytes.fromhex("1122334455667788")
        assert span[5][0] == b"tool-call"
        assert span[6][0] == 1
        assert _pb_fixed64_int(span[7][0]) == 1_000_000_000
        assert _pb_fixed64_int(span[8][0]) == 1_500_000_000

        attrs = _pb_attrs(span[9])
        assert attrs[b"openinference.span.kind"][1][0] == b"TOOL"
        assert attrs[b"count"][3][0] == 3
        assert attrs[b"ok"][2][0] == 1
        assert _pb_double_val(attrs[b"score"][4][0]) == 0.5

        event = _pb_decode(span[11][0])
        assert _pb_fixed64_int(event[1][0]) == 1_250_000_000
        assert event[2][0] == b"exception"
        assert _pb_attrs(event[3])[b"message"][1][0] == b"boom"

        status = _pb_decode(span[15][0])
        assert status[2][0] == b"failed"
        assert status[3][0] == 2

    def test_rejects_missing_span_timestamp(self):
        payload = {
            "resourceSpans": [
                {
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "traceId": "t" * 32,
                                    "spanId": "s" * 16,
                                    "name": "missing-time",
                                    "endTimeUnixNano": "2000000000",
                                }
                            ]
                        }
                    ]
                }
            ]
        }

        with pytest.raises(ValueError, match="Invalid Unix nanosecond timestamp"):
            otlp_json_to_protobuf(payload)
