from __future__ import annotations

import json
import subprocess
from pathlib import Path

PLUGIN = Path(__file__).parents[3] / "tracing" / "opencode" / "plugin" / "arize-tracing.ts"


def _run_node(script: str) -> dict:
    result = subprocess.run(
        ["node", "--experimental-transform-types", "--input-type=module", "-e", script],
        check=True,
        capture_output=True,
        text=True,
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
        const {{ ArizeTracing }} = await import({json.dumps(plugin.as_uri())});
        const messages = [{{
          info: {{ id: 'msg_foreign', sessionID: 'ses_foreign', role: 'user', time: {{ created: 1 }} }},
          parts: [{{ id: 'prt_foreign', sessionID: 'ses_foreign', messageID: 'msg_foreign', type: 'text', text: 'private' }}],
        }}];
        const hooks = await ArizeTracing({{
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
        const {{ ArizeTracing }} = await import({json.dumps(plugin.as_uri())});
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
        const hooks = await ArizeTracing({{
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
        import {{ ArizeTracing }} from {json.dumps(PLUGIN.as_uri())};
        let messagesCalls = 0;
        const hooks = await ArizeTracing({{
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
