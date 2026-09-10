---
name: manage-codex-tracing
description: Set up and configure Arize tracing for OpenAI Codex CLI sessions. Use for Codex tracing setup, Arize AX or Phoenix configuration, enable/disable operations, and troubleshooting.
---

# Set Up Codex Tracing

Configure OpenInference tracing for OpenAI Codex CLI sessions to Arize AX or Phoenix.

## Current architecture

Codex tracing is **notify-only**:

1. Codex invokes `arize-hook-codex-notify` for `agent-turn-complete`.
2. The handler locates the matching durable rollout under `~/.codex/sessions/` using a literal session ID and validates `session_meta` when present.
3. It reconstructs one turn as `CHAIN → LLM → TOOL`, with each tool owned by the LLM response cycle that initiated it.
4. It sends OTLP JSON directly through `core/common.py`.
5. If no usable rollout is available, it emits one bounded fallback LLM span from the notify payload.

```text
Codex CLI
  └─ agent-turn-complete (notify)
       └─ arize-hook-codex-notify
            ├─ read matching rollout JSONL
            ├─ reconstruct CHAIN / LLM / TOOL spans
            └─ POST /v1/traces to Phoenix or Arize
```

There are no `SessionStart`, `PreToolUse`, `Stop`, or other lifecycle hook entries in the current integration. No `/hooks` approval step is required.

## Workflow

1. Determine whether the user wants Phoenix or Arize AX.
2. Reuse existing credentials from `~/.arize/harness/config.json` when present.
3. Install/update the Codex harness.
4. Verify the top-level `notify` entry and backend connectivity.
5. Run a synthetic dry-run or a normal Codex turn.

## Backend configuration

The canonical backend configuration is `harnesses.codex` in `~/.arize/harness/config.json`. Preserve unrelated existing entries when updating it.

### Phoenix

```json
{
  "harnesses": {
    "codex": {
      "project_name": "codex",
      "target": "phoenix",
      "endpoint": "http://localhost:6006",
      "api_key": ""
    }
  }
}
```

A local Phoenix instance can be started with:

```bash
docker run --rm -p 6006:6006 arizephoenix/phoenix:latest
```

### Arize AX

```json
{
  "harnesses": {
    "codex": {
      "project_name": "codex",
      "target": "arize",
      "endpoint": "otlp.arize.com:443",
      "api_key": "<api-key>",
      "space_id": "<space-id>"
    }
  }
}
```

For an on-prem Arize deployment, put its OTLP endpoint in `harnesses.codex.endpoint`. There is no `ARIZE_OTLP_ENDPOINT` runtime override in the current resolver.

Environment credentials can override configured credentials:

- `PHOENIX_ENDPOINT` selects Phoenix.
- `PHOENIX_API_KEY` supplies optional Phoenix auth.
- `ARIZE_API_KEY` together with `ARIZE_SPACE_ID` selects Arize.
- `ARIZE_PROJECT_NAME` overrides the project name.

## Codex configuration

The installer writes one top-level entry to `~/.codex/config.toml`:

```toml
notify = ["~/.arize/harness/venv/bin/arize-hook-codex-notify"]
```

The `notify` array is one program argv, not a list of callbacks. If another notify program is already configured, the installer reports a conflict rather than overwriting it. Remove that integration first or configure a separate dispatcher program that invokes both integrations. Do not append the Arize executable as another array element and do not add lifecycle hook tables.

The optional `~/.codex/arize-env.sh` file may contain:

```bash
export ARIZE_TRACE_ENABLED=true
# export ARIZE_USER_ID="<user-id>"
# export ARIZE_LOG_PROMPTS=false
# export ARIZE_LOG_TOOL_DETAILS=false
# export ARIZE_LOG_TOOL_CONTENT=false
```

Keep this file mode `0600`.

## Validation

Check that the notify executable exists and configuration parses:

```bash
test -x ~/.arize/harness/venv/bin/arize-hook-codex-notify
~/.arize/harness/venv/bin/python - <<'PY'
from pathlib import Path
from tracing.codex._toml import _toml_load_strict
print(_toml_load_strict(Path.home()/'.codex/config.toml').get('notify'))
PY
```

Synthetic dry run:

```bash
ARIZE_DRY_RUN=true ~/.arize/harness/venv/bin/arize-hook-codex-notify \
  '{"type":"agent-turn-complete","thread-id":"synthetic-session","turn-id":"synthetic-turn","input-messages":[{"role":"user","content":"hello"}],"last-assistant-message":"hi"}'
```

When no matching synthetic rollout exists, this intentionally validates the notify fallback.

## Privacy behavior

- `ARIZE_LOG_PROMPTS` controls user and assistant text.
- `ARIZE_LOG_TOOL_DETAILS` controls tool arguments.
- `ARIZE_LOG_TOOL_CONTENT` controls tool outputs and tool-output-derived next-response input.
- Exported textual content attributes are bounded to 64 KiB.
- Image-generation binary/base64 results are omitted.
- Rollout lookup rejects path-like IDs and out-of-root symlinks.

## Troubleshooting

| Problem | Action |
|---|---|
| No traces | Check `ARIZE_TRACE_ENABLED`, backend config, and `~/.arize/harness/logs/codex.log`. |
| Notify not firing | Verify the top-level `notify` entry and executable path. No `/hooks` approval is involved. |
| Only one flat LLM span | The matching rollout was absent, incomplete, malformed, or failed session validation; inspect verbose/debug logs. |
| Wrong backend | Check `harnesses.codex.target/endpoint` and environment overrides. |
| Wrong project | Set `harnesses.codex.project_name` or `ARIZE_PROJECT_NAME`. |
| Sensitive content visible | Disable the relevant `ARIZE_LOG_*` gate and restart the Codex process. |
| Need payload diagnostics | Set `ARIZE_VERBOSE=true` and, only in a safe environment, `ARIZE_TRACE_DEBUG=true`. |
| Existing notify integration | The installer stops on conflict. Remove the existing program or use one dispatcher executable as the sole `notify` argv command. |

Logs are written to `~/.arize/harness/logs/codex.log` unless `ARIZE_LOG_FILE` overrides the path.
