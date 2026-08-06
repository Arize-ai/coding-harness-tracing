"""In-memory OTLP contract tests for typed Claude event rendering."""

from pathlib import Path

from core.event_model import AgentEvent, EventGraph, EventStatus, ModelCallEvent, ToolEvent, TurnEvent, Usage
from tracing.claude_code.hooks.span_renderer import render_event_graph
from tracing.claude_code.hooks.transcript import parse_claude_transcript

FIXTURE_DIR = Path(__file__).parent / "fixtures"
TRACE_ID = "a" * 32
SPAN_IDS = [f"{index:016x}" for index in range(1, 20)]


def _attrs(span):
    return {attribute["key"]: next(iter(attribute["value"].values())) for attribute in span["attributes"]}


def _spans(payload):
    return payload["resourceSpans"][0]["scopeSpans"][0]["spans"]


def _factory():
    values = iter(SPAN_IDS)
    return lambda: next(values)


def _main_graph():
    root = TurnEvent(
        event_id="turn:1",
        session_id="session-main-1",
        turn_id="1",
        sequence=0,
        started_at_ms=1_767_268_800_000,
        ended_at_ms=1_767_268_803_500,
        status=EventStatus.COMPLETED,
        input="synthetic prompt",
        output="SYNTHETIC_TOOL_OK",
    )
    return parse_claude_transcript(FIXTURE_DIR / "main_tool_cycle.jsonl", root)


def test_renders_turn_model_calls_and_tools_with_exact_parents():
    payload = render_event_graph(
        _main_graph(),
        trace_id=TRACE_ID,
        service_name="claude-code",
        scope_name="claude-code-hooks",
        span_id_factory=_factory(),
    )
    spans = _spans(payload)

    assert [span["name"] for span in spans] == [
        "Turn 1",
        "LLM call 1: qwen3-coder-next",
        "Read",
        "LLM call 2: qwen3-coder-next",
        "Bash",
        "LLM call 3: qwen3-coder-next",
    ]
    assert [_attrs(span)["openinference.span.kind"] for span in spans] == [
        "CHAIN",
        "LLM",
        "TOOL",
        "LLM",
        "TOOL",
        "LLM",
    ]
    assert all(span["traceId"] == TRACE_ID for span in spans)
    assert "parentSpanId" not in spans[0]
    assert spans[1]["parentSpanId"] == spans[0]["spanId"]
    assert spans[2]["parentSpanId"] == spans[1]["spanId"]
    assert spans[3]["parentSpanId"] == spans[0]["spanId"]
    assert spans[4]["parentSpanId"] == spans[3]["spanId"]
    assert spans[5]["parentSpanId"] == spans[0]["spanId"]


def test_renders_per_call_usage_without_putting_aggregate_usage_on_root():
    spans = _spans(render_event_graph(_main_graph(), trace_id=TRACE_ID, span_id_factory=_factory()))
    root_attrs = _attrs(spans[0])
    first_model_attrs = _attrs(spans[1])

    assert "llm.token_count.total" not in root_attrs
    assert first_model_attrs["llm.token_count.prompt"] == 115
    assert first_model_attrs["llm.token_count.completion"] == 20
    assert first_model_attrs["llm.token_count.total"] == 135
    assert first_model_attrs["llm.token_count.prompt_details.cache_read"] == 10
    assert first_model_attrs["llm.token_count.prompt_details.cache_write"] == 5


def test_renders_failed_tool_with_error_status():
    root = TurnEvent(
        event_id="turn:1",
        session_id="session-main-1",
        turn_id="1",
        sequence=0,
        started_at_ms=1000,
        ended_at_ms=3000,
        status=EventStatus.FAILED,
    )
    model = ModelCallEvent(
        event_id="model:1",
        parent_event_id=root.event_id,
        session_id=root.session_id,
        turn_id=root.turn_id,
        sequence=1,
        started_at_ms=1100,
        ended_at_ms=1200,
        status=EventStatus.COMPLETED,
        model="m",
        usage=Usage(),
    )
    tool = ToolEvent(
        event_id="tool:1",
        parent_event_id=model.event_id,
        session_id=root.session_id,
        turn_id=root.turn_id,
        sequence=2,
        started_at_ms=1200,
        ended_at_ms=1300,
        status=EventStatus.FAILED,
        error="command failed",
        tool_call_id="call-1",
        tool_name="Bash",
    )

    spans = _spans(render_event_graph(EventGraph([root, model, tool]), trace_id=TRACE_ID, span_id_factory=_factory()))

    assert spans[2]["status"] == {"code": 2, "message": "command failed"}
    assert _attrs(spans[2])["tool.call.id"] == "call-1"


def test_renders_agent_as_agent_parent_for_its_model_and_tool():
    root = AgentEvent(
        event_id="agent:1",
        session_id="session-agent-1",
        turn_id="1",
        sequence=0,
        started_at_ms=1000,
        ended_at_ms=2000,
        status=EventStatus.COMPLETED,
        agent_id="agent-1",
        source_id="synthetic-explorer",
    )
    graph = parse_claude_transcript(FIXTURE_DIR / "subagent_agent.jsonl", root)
    spans = _spans(render_event_graph(graph, trace_id=TRACE_ID, span_id_factory=_factory()))

    assert spans[0]["name"] == "Subagent: synthetic-explorer"
    assert _attrs(spans[0])["openinference.span.kind"] == "AGENT"
    assert _attrs(spans[0])["subagent.id"] == "agent-1"
    assert spans[1]["parentSpanId"] == spans[0]["spanId"]
    assert spans[2]["parentSpanId"] == spans[1]["spanId"]


def test_duplicate_event_ids_receive_distinct_span_ids():
    first = TurnEvent(
        event_id="duplicate",
        session_id="session",
        turn_id="1",
        sequence=0,
        started_at_ms=100,
        ended_at_ms=200,
        status=EventStatus.COMPLETED,
    )
    second = TurnEvent(
        event_id="duplicate",
        session_id="session",
        turn_id="1",
        sequence=1,
        started_at_ms=100,
        ended_at_ms=200,
        status=EventStatus.COMPLETED,
    )

    spans = _spans(render_event_graph(EventGraph([first, second]), trace_id=TRACE_ID, span_id_factory=_factory()))

    assert spans[0]["spanId"] != spans[1]["spanId"]


def test_renderer_normalizes_negative_timestamps():
    event = TurnEvent(
        event_id="negative",
        session_id="session",
        turn_id="1",
        sequence=0,
        started_at_ms=-2,
        ended_at_ms=-1,
        status=EventStatus.COMPLETED,
    )

    span = _spans(render_event_graph(EventGraph([event]), trace_id=TRACE_ID, span_id_factory=_factory()))[0]

    assert int(span["startTimeUnixNano"]) >= 0
    assert int(span["endTimeUnixNano"]) >= int(span["startTimeUnixNano"])


def test_renderer_breaks_parent_cycles_into_a_valid_tree():
    first = TurnEvent(
        event_id="first",
        parent_event_id="second",
        session_id="session",
        turn_id="1",
        sequence=0,
        started_at_ms=100,
        ended_at_ms=200,
        status=EventStatus.COMPLETED,
    )
    second = TurnEvent(
        event_id="second",
        parent_event_id="first",
        session_id="session",
        turn_id="1",
        sequence=1,
        started_at_ms=100,
        ended_at_ms=200,
        status=EventStatus.COMPLETED,
    )

    spans = _spans(render_event_graph(EventGraph([first, second]), trace_id=TRACE_ID, span_id_factory=_factory()))

    assert "parentSpanId" not in spans[0]
    assert spans[1]["parentSpanId"] == spans[0]["spanId"]


def test_capture_flags_redact_root_model_and_tool_content(monkeypatch):
    monkeypatch.setenv("ARIZE_LOG_PROMPTS", "false")
    monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "false")
    monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "false")

    spans = _spans(render_event_graph(_main_graph(), trace_id=TRACE_ID, span_id_factory=_factory()))
    all_attributes = [_attrs(span) for span in spans]
    serialized_values = "\n".join(str(value) for attrs in all_attributes for value in attrs.values())

    assert "synthetic prompt" not in serialized_values
    assert "/synthetic/project" not in serialized_values
    assert "SYNTHETIC_TOOL_OK" not in serialized_values
    assert "<redacted (" in serialized_values


def test_capture_flags_redact_agent_and_tool_errors(monkeypatch):
    monkeypatch.setenv("ARIZE_LOG_PROMPTS", "false")
    monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "false")
    monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "false")

    agent = AgentEvent(
        event_id="agent:secret",
        session_id="session-agent-1",
        turn_id="1",
        sequence=0,
        started_at_ms=1000,
        ended_at_ms=2000,
        status=EventStatus.COMPLETED,
        input="SECRET_AGENT_PROMPT",
        output="SECRET_AGENT_OUTPUT",
        agent_id="agent-secret",
        source_id="synthetic-explorer",
    )
    graph = parse_claude_transcript(FIXTURE_DIR / "subagent_agent.jsonl", agent)
    tool = next(event for event in graph.events if isinstance(event, ToolEvent))
    tool.status = EventStatus.FAILED
    tool.error = "SECRET_TOOL_ERROR"

    spans = _spans(render_event_graph(graph, trace_id=TRACE_ID, span_id_factory=_factory()))
    serialized_values = str(spans)

    assert "SECRET_AGENT_PROMPT" not in serialized_values
    assert "SECRET_AGENT_OUTPUT" not in serialized_values
    assert "SECRET_TOOL_ERROR" not in serialized_values
    assert "<redacted (" in serialized_values


def test_duplicate_override_span_ids_are_replaced():
    graph = _main_graph()
    collision = "b" * 16
    overrides = {graph.events[0].event_id: collision, graph.events[1].event_id: collision}

    spans = _spans(
        render_event_graph(
            graph,
            trace_id=TRACE_ID,
            span_id_overrides=overrides,
            span_id_factory=_factory(),
        )
    )

    assert len({span["spanId"] for span in spans}) == len(spans)
