---
name: manage-omp-tracing
description: Set up and configure Arize tracing for Oh My Pi (omp) terminal coding sessions. Use when users want to set up tracing, configure Arize AX or Phoenix for Oh My Pi, enable/disable omp tracing, or troubleshoot tracing issues. Triggers on "set up omp tracing", "configure Arize for Oh My Pi", "configure Phoenix for omp", "enable omp tracing", "setup-omp-tracing", or any request about connecting omp / Oh My Pi to Arize or Phoenix for observability.
---

# Setup omp Tracing

Configure OpenInference tracing for **Oh My Pi (omp)** terminal coding sessions to Arize AX (cloud) or Phoenix (self-hosted). Like opencode, omp loads its extensions [in-process inside its Bun runtime](https://omp.sh/docs/hooks) — there is no per-event subprocess. The integration ships as a small TypeScript hook shim that forwards omp's once-fired lifecycle events to a Python handler (`arize-hook-omp`) which emits spans. Spans are sent directly to the backend from the handler — no separate buffer/collector service is required.

## How to Use This Skill

**This skill follows a decision tree workflow.** Start by asking the user where they are in the setup process:

1. **Is the harness already installed?**
   - Check `~/.omp/extensions/arize-tracing.ts` for the Arize hook shim
   - Check `~/.omp/agent/settings.json` for the shim's path in the `extensions` array
   - Check `~/.arize/harness/config.json` for the `harnesses.omp` block
   - If all are present -> Jump to [Validate](#validate) or [Troubleshoot](#troubleshoot)

2. **Do they already have credentials?**
   - Yes -> Jump to [Configure Settings](#configure-settings)
   - No -> Continue to step 3

3. **Which backend do they want to use?**
   - Phoenix (self-hosted) -> Go to [Set Up Phoenix](#set-up-phoenix)
   - Arize AX (cloud) -> Go to [Set Up Arize AX](#set-up-arize-ax)

4. **Are they troubleshooting?**
   - Yes -> Jump to [Troubleshoot](#troubleshoot)

**Important:** Only follow the relevant path for the user's needs. Don't go through all sections.

## Set Up Phoenix

Phoenix is self-hosted. No Python dependencies are needed for tracing -- spans are sent directly via `send_span()` using stdlib `urllib`.

### Install Phoenix

Ask if they already have Phoenix running. If not, walk through:

```bash
# Option A: pip
pip install arize-phoenix && phoenix serve

# Option B: Docker
docker run -p 6006:6006 arizephoenix/phoenix:latest
```

Phoenix UI will be available at `http://localhost:6006`. Confirm it's running:

```bash
curl -sf http://localhost:6006/v1/traces >/dev/null && echo "Phoenix is running" || echo "Phoenix not reachable"
```

Then proceed to [Configure Settings](#configure-settings) with the Phoenix endpoint.

## Set Up Arize AX

Arize AX is available as a SaaS platform or as an on-prem deployment. Users need an account, a space, and an API key.

**First, ask the user: "Are you using the Arize SaaS platform or an on-prem instance?"**

- **SaaS** -> Uses the default endpoint (`otlp.arize.com:443`). Continue below.
- **On-prem** -> The user will need to provide their custom OTLP endpoint (e.g., `otlp.mycompany.arize.com:443`). Ask for it and note it for the [Configure Settings](#configure-settings) step.

### 1. Create an account

If the user doesn't have an Arize account:
- **SaaS**: Sign up at https://app.arize.com/auth/join
- **On-prem**: Contact their administrator for access to the on-prem instance

### 2. Get Space ID and API key

Walk the user through finding their credentials:
1. Log in to their Arize instance (https://app.arize.com for SaaS, or their on-prem URL)
2. Click **Settings** (gear icon) in the left sidebar
3. The **Space ID** is shown on the Space Settings page
4. Go to the **API Keys** tab
5. Click **Create API Key** or copy an existing one

Both `api_key` and `space_id` are required for the shared config.

**No Python dependencies are needed.** Both Phoenix and Arize AX use HTTP/JSON -- no additional Python dependencies are needed.

Then proceed to [Configure Settings](#configure-settings). If the user is on an on-prem instance, remind them to provide their custom endpoint.

## Configure Settings

**Important:** Users must run this setup before tracing will work. The `send_span()` function requires `~/.arize/harness/config.json` to exist for backend credential resolution.

### Ask the user for:

1. **Backend choice**: Phoenix or Arize AX
2. **Credentials** (only if no existing config):
   - Phoenix: endpoint URL (default: `http://localhost:6006`), optional API key
   - Arize AX: API key and Space ID
3. **OTLP Endpoint** (Arize AX only, optional): For hosted Arize instances using a custom endpoint. Defaults to `otlp.arize.com:443`.
4. **Project name** (optional): defaults to `"omp"`, stored under `harnesses.omp.project_name`
5. **User ID** (optional): Set `ARIZE_USER_ID` env var to identify spans by user (useful for teams)

### Write the config

The config file at `~/.arize/harness/config.json` is the single source of truth for backend credentials and per-harness settings. Create the directory structure if needed: `mkdir -p ~/.arize/harness/{bin,run,logs,state/omp}`

**Important: read-merge-write.** If `~/.arize/harness/config.json` already exists, read it first, then merge in the new or updated fields (e.g., add/update the `harnesses.omp` entry) while preserving existing backend credentials. Only prompt for backend credentials if no existing config is found.

**Phoenix:**
```json
{
  "harnesses": {
    "omp": {
      "project_name": "omp",
      "target": "phoenix",
      "endpoint": "<endpoint>",
      "api_key": ""
    }
  }
}
```

If Phoenix requires authentication (e.g. Phoenix Cloud), set the API key here under
`api_key`, or export `PHOENIX_API_KEY` in the environment — it is sent as a
`Authorization: Bearer <key>` header. The env var takes precedence over the config value.
Leave `api_key: ""` for an unauthenticated local Phoenix.

**Arize AX:**
```json
{
  "harnesses": {
    "omp": {
      "project_name": "omp",
      "target": "arize",
      "endpoint": "otlp.arize.com:443",
      "api_key": "<key>",
      "space_id": "<id>"
    }
  }
}
```

If the user has a custom OTLP endpoint, set it in `harnesses.omp.endpoint`.

### Activate the omp hook

omp does **not** auto-discover an extensions directory — the `extensions` key in `~/.omp/agent/settings.json` is an explicit array of file/dir paths omp loads ([hook docs](https://omp.sh/docs/hooks)). The installer drops the Arize hook shim at `~/.omp/extensions/arize-tracing.ts` **and** registers that absolute path in the `extensions` array, e.g.:

```json
{ "extensions": ["/Users/me/.omp/extensions/arize-tracing.ts"] }
```

omp picks up the registered extension on next launch.

Install or reinstall via the installer:

```bash
./install.sh omp
```

To uninstall:

```bash
./install.sh uninstall omp
```

Uninstall removes the shim's path from the `extensions` array, deletes the hook file at `~/.omp/extensions/arize-tracing.ts` (only if it carries the Arize header marker — the installer never touches the user's own extensions), and removes the `harnesses.omp` block from `~/.arize/harness/config.json`.

### Validate

1. **Config exists**: Run `cat ~/.arize/harness/config.json` to verify the config file exists and has correct backend credentials under `harnesses.omp`.
2. **Phoenix** (if applicable): Run `curl -sf <endpoint>/v1/traces >/dev/null` to check connectivity.
3. **Hook installed**: Verify `~/.omp/extensions/arize-tracing.ts` exists and starts with the Arize header marker.
4. **Hook registered**: Verify the shim's absolute path appears in the `extensions` array of `~/.omp/agent/settings.json` (omp does not auto-discover — registration is required).
5. **Handler entry point**: Verify the handler binary exists at `~/.arize/harness/venv/bin/arize-hook-omp` (or `~/.arize/harness/venv/Scripts/arize-hook-omp.exe` on Windows). The shim spawns this binary by absolute path — it does not rely on PATH resolution. `install.sh` installs it as a venv entry point.

### Confirm

Tell the user:
- Config saved to `~/.arize/harness/config.json`
- omp hook shim installed at `~/.omp/extensions/arize-tracing.ts`
- Shim path registered in the `extensions` array of `~/.omp/agent/settings.json` — omp requires explicit registration (no auto-discovery)
- Spans are sent directly to the backend from the handler — no background process needed
- After saving, open a new omp session and traces will appear in their Phoenix UI or Arize AX dashboard under the project name
- Mention `ARIZE_DRY_RUN=true` to test without sending data (set as env var before launching omp)
- Mention `ARIZE_VERBOSE=true` for debug output
- Errors and handler stderr are always written to `~/.arize/harness/logs/omp.log` (the adapter redirects Python stderr there via `ARIZE_LOG_FILE`); set `ARIZE_VERBOSE=true` in the shell before launching omp to also capture routine handler activity (event dispatch, span emits, state transitions)
- Toggle tracing on/off via `ARIZE_TRACE_ENABLED` env var (must be exported in the user's shell before launching omp — the shim and handler inherit host env vars)
- Tail the log file at `~/.arize/harness/logs/omp.log` for real-time debugging
- Mention `ARIZE_TRACE_DEBUG=true` to dump raw event payloads under `~/.arize/harness/state/debug/` (files are named `omp_before_agent_start_<ts>.json` / `omp_turn_end_<ts>.json` / `omp_agent_end_<ts>.json` / `omp_session_shutdown_<ts>.json`) for inspection

## Architecture (How spans are produced)

omp loads its extensions **in-process** inside its Bun runtime ([hook docs](https://omp.sh/docs/hooks)). Unlike opencode, omp exposes rich, **once-fired** lifecycle events that already carry final, structured data, so the Arize integration is a stateful event-forward (no snapshot reconciliation, no dedup). It is split into two pieces:

1. **TypeScript hook shim** at `~/.omp/extensions/arize-tracing.ts`. A dumb bridge. On a whitelist of lifecycle events — `before_agent_start` (the prompt), `turn_end` (the completed `AssistantMessage` with inline token usage + model, plus that turn's `toolResults`), `agent_end` (run finished), and `session_shutdown` — it spawns `arize-hook-omp` detached and pipes the event payload to stdin. The shim contains no tracing logic and never blocks omp's event loop.
2. **Python event handler** (`arize-hook-omp`). A small state machine keyed by session id. It dispatches on `payload["type"]`, accumulates per-session state, and emits `Turn`/`LLM`/`TOOL` spans on receipt. Pairs each `ToolCall` with its `ToolResultMessage` by id to build TOOL spans with both input args and output.

## Span tree

Each trace covers one **agent run** (one user prompt → the agent's internal turn/tool-use loop → its final answer). A run usually contains several model calls; one trace covers all of them. The tree:

| Span | Kind | Description |
|------|------|-------------|
| `Turn` | CHAIN | Root span. `input.value` is the user prompt (from `before_agent_start`); `output.value` is the final assistant message's text. One per agent run. |
| `LLM: <model>` | LLM | Child of `Turn`. One per `turn_end` (one per model call in the loop). Carries `llm.model_name`, `llm.provider`, prompt/completion/reasoning token counts, cache read/write tokens, and `llm.cost`. omp surfaces token usage inline on the assistant message, so it **is** captured. |
| `<tool>` | TOOL | Child of `Turn`. One per `ToolResultMessage` in a `turn_end`, paired with its originating `ToolCall` by id. Records `tool.name`, redacted input args + output, and tool-specific attributes; errors are recorded with span status. |

## Troubleshoot

Common issues and fixes for omp:

| Problem | Fix |
|---------|-----|
| Traces not appearing | Verify config exists: `cat ~/.arize/harness/config.json`. Check handler log: `tail -20 ~/.arize/harness/logs/omp.log`. Confirm the hook is in place: `ls ~/.omp/extensions/arize-tracing.ts`. |
| Hook not loading | omp does **not** auto-discover an extensions dir. Confirm the shim's absolute path is in the `extensions` array of `~/.omp/agent/settings.json`, then restart omp and check its CLI output for extension errors. |
| Handler entry point missing | The shim spawns the handler by absolute path; verify the binary exists at `~/.arize/harness/venv/bin/arize-hook-omp` (or `~/.arize/harness/venv/Scripts/arize-hook-omp.exe` on Windows). Rerun `./install.sh omp` to reinstall the venv entry point. |
| Missing LLM or tool spans | Spans emit on `turn_end` (one LLM span per model call, one TOOL span per tool result). If a turn hasn't completed yet, its spans won't appear until the event fires. Wait for the agent run to finish (`agent_end`). |
| Trace missing the final answer | `output.value` on the `Turn` span comes from the final assistant message. Confirm the run reached `agent_end`. |
| Phoenix unreachable | Verify Phoenix is running: `curl -sf <endpoint>/v1/traces` |
| Want to test without sending | Set `ARIZE_DRY_RUN=true` env var before launching omp |
| Want verbose logging | Set `ARIZE_VERBOSE=true` env var before launching omp |
| Want raw event payloads for inspection | Set `ARIZE_TRACE_DEBUG=true` env var; payloads land under `~/.arize/harness/state/debug/` as `omp_before_agent_start_<ts>.json` / `omp_turn_end_<ts>.json` / `omp_agent_end_<ts>.json` / `omp_session_shutdown_<ts>.json` |
| Wrong project name | Set `harnesses.omp.project_name` in `~/.arize/harness/config.json` (default: `"omp"`) |
| Spans missing user attribution | Set `ARIZE_USER_ID` env var before launching omp |
| Tracing not toggling | Ensure `ARIZE_TRACE_ENABLED` is exported in your shell, not just set — the omp process and any shim-spawned handler inherit host env vars |
