from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from core.common import StateManager

os.environ["ARIZE_DISABLE_FORK"] = "true"

from tracing.opencode.hooks.handlers import _handle_close, _handle_reconcile  # noqa: E402

PLUGIN = Path(__file__).parents[3] / "tracing" / "opencode" / "plugin" / "arize-tracing.ts"


def _run_node(script: str, env: dict | None = None) -> dict:
    result = subprocess.run(
        ["node", "--experimental-strip-types", "--input-type=module", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    return json.loads(result.stdout)


def _instrumented_plugin(tmp_path: Path) -> Path:
    """Copy the real plugin with only its process boundary replaced by a sink."""
    source = PLUGIN.read_text(encoding="utf-8")
    marker = "function forward(payload: unknown): void {\n"
    assert source.count(marker) == 1
    source = source.replace(
        marker,
        marker + "  globalThis.__arizePayloads.push(payload)\n  return\n",
    )
    target = tmp_path / "arize-tracing-instrumented.ts"
    target.write_text(source, encoding="utf-8")
    return target


def test_foreign_root_messages_are_not_forwarded(tmp_path: Path) -> None:
    plugin = _instrumented_plugin(tmp_path)
    result = _run_node(
        f"""
        globalThis.__arizePayloads = [];
        const {{ default: plugin }} = await import({json.dumps(plugin.as_uri())});
        const messages = [{{
          info: {{ id: 'msg_foreign', sessionID: 'ses_foreign', role: 'user', time: {{ created: 1 }} }},
          parts: [{{ id: 'prt_foreign', sessionID: 'ses_foreign', messageID: 'msg_foreign', type: 'text', text: 'private' }}],
        }}];
        const hooks = await plugin.server({{
          client: {{ session: {{
            get: async () => ({{ data: {{ id: 'ses_root' }} }}),
            messages: async () => ({{ data: messages }}),
          }} }},
        }});
        await hooks.event({{ event: {{ type: 'session.idle', properties: {{ sessionID: 'ses_root' }} }} }});
        console.log(JSON.stringify({{ payloads: globalThis.__arizePayloads }}));
        """
    )
    assert result == {"payloads": []}


def test_foreign_child_messages_are_not_forwarded_as_child_snapshot(tmp_path: Path) -> None:
    plugin = _instrumented_plugin(tmp_path)
    result = _run_node(
        f"""
        globalThis.__arizePayloads = [];
        const {{ default: plugin }} = await import({json.dumps(plugin.as_uri())});
        const rootMessages = [{{
          info: {{ id: 'msg_root', sessionID: 'ses_root', role: 'assistant', time: {{ created: 1, completed: 2 }} }},
          parts: [{{
            id: 'prt_task', sessionID: 'ses_root', messageID: 'msg_root', type: 'tool', tool: 'task', callID: 'call_1',
            state: {{ metadata: {{ sessionId: 'ses_child' }} }},
          }}],
        }}];
        const childMessages = [{{
          info: {{ id: 'msg_foreign', sessionID: 'ses_foreign', role: 'user', time: {{ created: 1 }} }},
          parts: [{{ id: 'prt_foreign', sessionID: 'ses_foreign', messageID: 'msg_foreign', type: 'text', text: 'private' }}],
        }}];
        const hooks = await plugin.server({{
          client: {{ session: {{
            get: async (request) => ({{ data: request.path.id === 'ses_root'
              ? {{ id: 'ses_root' }}
              : {{ id: 'ses_child', parentID: 'ses_root' }} }}),
            messages: async (request) => ({{ data: request.path.id === 'ses_root' ? rootMessages : childMessages }}),
          }} }},
        }});
        await hooks.event({{ event: {{ type: 'session.idle', properties: {{ sessionID: 'ses_root' }} }} }});
        console.log(JSON.stringify({{ payloads: globalThis.__arizePayloads }}));
        """
    )
    assert len(result["payloads"]) == 1
    assert result["payloads"][0]["childSessions"] == []


def test_sdk_error_result_does_not_fetch_or_forward_root_transcript() -> None:
    result = _run_node(
        f"""
        import plugin from {json.dumps(PLUGIN.as_uri())};
        let messagesCalls = 0;
        const hooks = await plugin.server({{
          client: {{ session: {{
            get: async () => ({{ error: {{ message: 'unavailable' }}, data: undefined }}),
            messages: async () => {{ messagesCalls += 1; return {{ data: [] }}; }},
          }} }},
        }});
        await hooks.event({{ event: {{
          type: 'message.updated',
          properties: {{ info: {{ role: 'assistant', time: {{ completed: 1 }}, sessionID: 'ses_root' }} }},
        }} }});
        console.log(JSON.stringify({{ messagesCalls }}));
        """
    )
    assert result == {"messagesCalls": 0}


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX shell script as the hook binary")
def test_forwarder_survives_a_child_that_exits_at_once(tmp_path: Path) -> None:
    hook = tmp_path / ".arize" / "harness" / "venv" / "bin" / "arize-hook-opencode"
    hook.parent.mkdir(parents=True)
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o755)
    result = _run_node(
        f"""
        import {{ setTimeout }} from 'node:timers/promises';
        import plugin from {json.dumps(PLUGIN.as_uri())};
        const info = {{ id: 'msg', role: 'assistant', sessionID: 'ses_root', time: {{ completed: 1 }} }};
        const messages = [{{ info, parts: [{{ sessionID: 'ses_root', type: 'text', text: 'x'.repeat(8_000_000) }}] }}];
        const hooks = await plugin.server({{ client: {{ session: {{
          get: async () => ({{ data: {{ id: 'ses_root' }} }}),
          messages: async () => ({{ data: messages }}),
        }} }} }});
        await hooks.event({{ event: {{ type: 'message.updated', properties: {{ info }} }} }});
        await setTimeout(300);
        console.log(JSON.stringify({{ ok: true }}));
        """,
        env={**os.environ, "HOME": str(tmp_path)},
    )
    assert result == {"ok": True}


def _v2_snapshot(
    tmp_path: Path, records: dict, sessions: dict, events: list, *, reconnect=False, sequence: list | None = None
) -> dict:
    plugin = _instrumented_plugin(tmp_path)
    return _run_node(
        f"""
        import {{ setImmediate }} from 'node:timers/promises';
        import {{ setTimeout }} from 'node:timers/promises';
        globalThis.__arizePayloads = [];
        const exports = await import({json.dumps(plugin.as_uri())});
        const plugin = exports.default;
        const records = {json.dumps(records)};
        const sessions = {json.dumps(sessions)};
        const sequence = {json.dumps(sequence)};
        let signal, drained = false, subscriptions = 0, messagesCalls = 0;
        const cleanup = plugin.setup({{
          session: {{
            get: async ({{sessionID}}) => sessions[sessionID],
            context: async ({{sessionID}}) => {{
              messagesCalls++;
              return sequence ? sequence[Math.min(messagesCalls, sequence.length) - 1] : records[sessionID];
            }},
          }},
          event: {{ async *subscribe(options) {{
            subscriptions++;
            signal = options.signal;
            if ({json.dumps(reconnect)} && subscriptions === 1) throw new Error('disconnected');
            yield* {json.dumps(events)};
            drained = true;
            await new Promise(resolve => signal.addEventListener('abort', resolve, {{once: true}}));
          }} }},
        }});
        const deadline = Date.now() + 7000;
        while (!drained && Date.now() < deadline) await setTimeout(5);
        cleanup();
        await setImmediate();
        console.log(JSON.stringify({{
          exports: Object.keys(exports), id: plugin.id, server: typeof plugin.server,
          payloads: globalThis.__arizePayloads, aborted: signal.aborted, drained, subscriptions, messagesCalls,
        }}));
        """
    )


def _user(message_id="msg_user") -> dict:
    return {"id": message_id, "type": "user", "text": "private prompt", "time": {"created": 1000}}


def _assistant(message_id="msg_assistant", content=None) -> dict:
    return {
        "id": message_id,
        "type": "assistant",
        "model": {"id": "test-model", "providerID": "test-provider"},
        "agent": "explore",
        "tokens": {"input": 10, "output": 5, "reasoning": 2, "cache": {"read": 3, "write": 4}},
        "cost": 0.01,
        "time": {"created": 1100, "completed": 1200},
        "content": content if content is not None else [{"type": "text", "text": "private answer"}],
    }


def _tool(child="ses_child", call="call_child", name="subagent") -> dict:
    return {
        "type": "tool",
        "id": call,
        "name": name,
        "time": {"created": 1110, "ran": 1120, "completed": 1190},
        "state": {
            "status": "completed",
            "input": {"prompt": "private task"},
            "metadata": {"sessionID": child},
            "content": [{"type": "text", "text": "private output"}],
        },
    }


def test_v2_normalizes_messages_tools_and_nested_children(tmp_path: Path) -> None:
    records = {
        "ses_root": [
            {"id": "msg_switch", "type": "model-switched"},
            _user(),
            _assistant(content=[_tool(), _tool()]),  # Duplicate child references are followed only once.
            _user("msg_user_2"),
            _assistant("msg_assistant_2"),
        ],
        "ses_child": [_user("msg_child_user"), _assistant("msg_child", [_tool("ses_grand", "call_grand")])],
        "ses_grand": [_user("msg_grand_user"), _assistant("msg_grand")],
    }
    result = _v2_snapshot(
        tmp_path,
        records,
        {
            "ses_root": {"id": "ses_root"},
            "ses_child": {"id": "ses_child", "parentID": "ses_root", "agent": "explore"},
            "ses_grand": {"id": "ses_grand", "parentID": "ses_child"},
        },
        [{"type": "session.execution.succeeded", "data": {"sessionID": "ses_root"}}],
    )
    assert result["exports"] == ["default"]  # V1 must not discover two plugin implementations.
    assert result["id"] == "arize-tracing"
    assert result["server"] == "function"
    assert result["aborted"] and result["drained"]
    payload = result["payloads"][0]
    assert payload["type"] == "close"
    assert len(payload["messages"]) == 4
    assistant = payload["messages"][1]
    assert assistant["info"]["parentID"] == "msg_user"
    assert payload["messages"][3]["info"]["parentID"] == "msg_user_2"
    assert assistant["info"]["tokens"] == records["ses_root"][2]["tokens"]
    assert assistant["info"]["modelID"] == "test-model"
    assert assistant["info"]["providerID"] == "test-provider"
    assert assistant["info"]["cost"] == 0.01
    tool = assistant["parts"][0]
    assert tool["callID"] == "call_child" and tool["tool"] == "subagent"
    assert tool["messageID"] == "msg_assistant" and tool["sessionID"] == "ses_root"
    assert tool["state"]["time"] == {"start": 1120, "end": 1190}
    assert tool["state"]["output"] == "private output"
    assert [child["sessionID"] for child in payload["childSessions"]] == ["ses_child", "ses_grand"]
    assert payload["childSessions"][0]["parentCallID"] == "call_child"
    assert payload["childSessions"][1]["parentSessionID"] == "ses_child"


def test_v2_compaction_keeps_the_turn_output(tmp_path: Path) -> None:
    """Compaction can drop the turn's user message from context between two snapshots."""
    before = [_user(), _assistant("msg_a1", [{"type": "text", "text": "working"}])]
    after = [{"id": "msg_compaction", "type": "compaction"}, _assistant("msg_a2", [{"type": "text", "text": "done"}])]
    result = _v2_snapshot(
        tmp_path,
        {},
        {"ses_root": {"id": "ses_root"}},
        [
            {"type": "session.step.ended", "data": {"sessionID": "ses_root"}},
            {"type": "session.execution.succeeded", "data": {"sessionID": "ses_root"}},
        ],
        sequence=[before, after],
    )
    state = StateManager(state_dir=tmp_path, state_file=tmp_path / "state.json", lock_path=tmp_path / ".lock")
    state.init_state()
    state.set("session_id", "ses_root")
    spans: list = []
    handlers = "tracing.opencode.hooks.handlers"
    with (
        mock.patch(f"{handlers}.resolve_session", return_value=state),
        mock.patch(f"{handlers}.ensure_session_initialized"),
        mock.patch(f"{handlers}._send_span_async", side_effect=spans.append),
    ):
        for payload in result["payloads"]:
            (_handle_close if payload["type"] == "close" else _handle_reconcile)(payload)
    turn = next(s for s in spans if s["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] == "Turn")
    attrs = {a["key"]: a["value"] for a in turn["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]}
    assert attrs["output.value"]["stringValue"] == "done"


def test_v2_tool_error_is_normalized(tmp_path: Path) -> None:
    failed = {**_tool(), "state": {"status": "error", "input": {}, "error": {"type": "x", "message": "boom"}}}
    result = _v2_snapshot(
        tmp_path,
        {"ses_root": [_user(), _assistant(content=[failed])]},
        {"ses_root": {"id": "ses_root"}},
        [{"type": "session.execution.succeeded", "data": {"sessionID": "ses_root"}}],
    )
    state = result["payloads"][0]["messages"][1]["parts"][0]["state"]
    assert state["status"] == "error" and state["error"] == "boom"
    assert "output" not in state


@pytest.mark.parametrize(
    ("event", "kind"),
    [
        ("session.step.ended", "reconcile"),
        ("session.step.failed", "reconcile"),
        ("session.execution.succeeded", "close"),
        ("session.execution.failed", "close"),
        ("session.execution.interrupted", "close"),
    ],
)
def test_v2_lifecycle_events(tmp_path: Path, event: str, kind: str) -> None:
    result = _v2_snapshot(
        tmp_path,
        {"ses_root": [_user(), _assistant()]},
        {"ses_root": {"id": "ses_root"}},
        [
            {"type": "session.text.delta", "data": {"sessionID": "ses_root"}},
            {"type": event, "data": {}},
            {"type": event, "data": {"sessionID": "ses_root", "reason": "user"}},
        ],
    )
    assert [p["type"] for p in result["payloads"]] == [kind]


def test_v2_shutdown_and_child_events_do_not_close_independent_turns(tmp_path: Path) -> None:
    result = _v2_snapshot(
        tmp_path,
        {},
        {"ses_child": {"id": "ses_child", "parentID": "ses_root"}},
        [
            {"type": "session.execution.interrupted", "data": {"sessionID": "ses_root", "reason": "shutdown"}},
            {"type": "session.execution.succeeded", "data": {"sessionID": "ses_child"}},
        ],
    )
    assert result["payloads"] == []
    assert result["messagesCalls"] == 0


@pytest.mark.parametrize("bad_records", [None, {}, [_assistant(content="invalid")]])
def test_v2_invalid_root_is_not_forwarded(tmp_path: Path, bad_records) -> None:
    result = _v2_snapshot(
        tmp_path,
        {"ses_root": bad_records},
        {"ses_root": {"id": "ses_root"}},
        [{"type": "session.step.ended", "data": {"sessionID": "ses_root"}}],
    )
    assert result["payloads"] == []


def test_v2_child_with_foreign_parent_is_not_forwarded(tmp_path: Path) -> None:
    result = _v2_snapshot(
        tmp_path,
        {
            "ses_root": [_user(), _assistant(content=[_tool()])],
            "ses_child": [_user("msg_child_user"), _assistant("msg_child")],
        },
        {"ses_root": {"id": "ses_root"}, "ses_child": {"id": "ses_child", "parentID": "foreign"}},
        [{"type": "session.step.ended", "data": {"sessionID": "ses_root"}}],
    )
    assert len(result["payloads"]) == 1
    assert result["payloads"][0]["childSessions"] == []


def test_v2_stream_reconnects_and_cleanup_aborts(tmp_path: Path) -> None:
    result = _v2_snapshot(tmp_path, {}, {}, [], reconnect=True)
    assert result["subscriptions"] == 2
    assert result["aborted"] and result["drained"]


def test_v2_cleanup_during_reconnect_delay_stops_quietly() -> None:
    result = _run_node(
        f"""
        import {{ setTimeout }} from 'node:timers/promises';
        import plugin from {json.dumps(PLUGIN.as_uri())};
        let subscriptions = 0;
        const cleanup = plugin.setup({{ event: {{ async *subscribe() {{ subscriptions++; throw new Error('down'); }} }} }});
        await setTimeout(50);
        cleanup();
        await setTimeout(50);
        console.log(JSON.stringify({{ subscriptions }}));
        """
    )
    assert result == {"subscriptions": 1}


def test_v1_server_forwards_completed_messages_and_children(tmp_path: Path) -> None:
    plugin = _instrumented_plugin(tmp_path)
    result = _run_node(
        f"""
        globalThis.__arizePayloads = [];
        const {{default: plugin}} = await import({json.dumps(plugin.as_uri())});
        const root = [{{
          info: {{id: 'msg_root', role: 'assistant', sessionID: 'ses_root', time: {{created: 1, completed: 2}}}},
          parts: [{{type: 'tool', tool: 'task', callID: 'call_child', messageID: 'msg_root', sessionID: 'ses_root',
            state: {{status: 'completed', metadata: {{sessionId: 'ses_child'}}}}}}],
        }}];
        const child = [{{info: {{id: 'msg_child', role: 'assistant', sessionID: 'ses_child'}}, parts: []}}];
        const hooks = await plugin.server({{client: {{session: {{
          get: async ({{path: {{id}}}}) => ({{data: id === 'ses_root' ? {{id}} : {{id, parentID: 'ses_root'}}}}),
          messages: async ({{path: {{id}}}}) => ({{data: id === 'ses_root' ? root : child}}),
        }}}}}});
        await hooks.event({{event: {{type: 'message.updated', properties: {{info: root[0].info}}}}}});
        await hooks.event({{event: {{type: 'session.idle', properties: {{sessionID: 'ses_root'}}}}}});
        console.log(JSON.stringify({{payloads: globalThis.__arizePayloads, root}}));
        """
    )
    assert [p["type"] for p in result["payloads"]] == ["reconcile", "close"]
    for payload in result["payloads"]:
        assert payload["messages"] == result["root"]
        assert payload["childSessions"][0]["sessionID"] == "ses_child"
        assert payload["childSessions"][0]["parentCallID"] == "call_child"


@pytest.mark.parametrize("blocked_session", ["ses_root", "ses_child"])
def test_v2_cleanup_drops_an_inflight_snapshot(tmp_path: Path, blocked_session: str) -> None:
    plugin = _instrumented_plugin(tmp_path)
    records = {
        "ses_root": [_user(), _assistant(content=[_tool()])],
        "ses_child": [_user("msg_child_user"), _assistant("msg_child", [_tool("ses_grand", "call_grand")])],
        "ses_grand": [_user("msg_grand_user"), _assistant("msg_grand")],
    }
    result = _run_node(
        f"""
        import {{setImmediate}} from 'node:timers/promises';
        globalThis.__arizePayloads = [];
        const {{default: plugin}} = await import({json.dumps(plugin.as_uri())});
        const records = {json.dumps(records)};
        const parents = {{ses_child: 'ses_root', ses_grand: 'ses_child'}};
        let release, fetched, calls = 0;
        const fetching = new Promise(resolve => fetched = resolve);
        const blocked = new Promise(resolve => release = resolve);
        const cleanup = plugin.setup({{
          session: {{
            get: async ({{sessionID}}) => {{ calls++; return {{id: sessionID, parentID: parents[sessionID]}}; }},
            context: async ({{sessionID}}) => {{
              calls++;
              if (sessionID === {json.dumps(blocked_session)}) {{ fetched(); await blocked; }}
              return records[sessionID];
            }},
          }},
          event: {{ async *subscribe() {{
            yield {{type: 'session.execution.succeeded', data: {{sessionID: 'ses_root'}}}};
          }} }},
        }});
        await fetching;
        const before = calls;
        cleanup();
        release();
        await setImmediate();
        console.log(JSON.stringify({{payloads: globalThis.__arizePayloads, started: calls - before}}));
        """
    )
    assert result == {"payloads": [], "started": 0}
