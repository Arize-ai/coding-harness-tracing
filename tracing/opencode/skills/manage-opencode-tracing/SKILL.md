---
name: manage-opencode-tracing
description: Set up and configure Arize tracing for opencode terminal coding sessions. Use when users want to set up tracing, configure Arize AX or Phoenix for opencode, enable/disable tracing, or troubleshoot tracing issues. Triggers on "set up opencode tracing", "configure Arize for opencode", "configure Phoenix for opencode", "enable opencode tracing", "setup-opencode-tracing", or any request about connecting opencode to Arize or Phoenix for observability.
---

# Setup opencode Tracing

Configure OpenInference tracing for **opencode** terminal coding sessions to Arize AX (cloud) or Phoenix (self-hosted). Unlike the other harnesses in this repo, opencode loads its extensions [in-process inside its Bun runtime](https://opencode.ai/docs/plugins/) — there is no per-event subprocess. The integration ships as a small TypeScript plugin shim that pulls snapshots via the [opencode SDK](https://opencode.ai/docs/sdk/) and spawns a Python reconciler (`arize-hook-opencode`) which emits spans. Spans are sent directly to the backend from the reconciler — no separate buffer/collector service is required.

## How to Use This Skill

**This skill follows a decision tree workflow.** Start by asking the user where they are in the setup process:

1. **Is the harness already installed?**
   - Check `~/.config/opencode/plugin/arize-tracing.ts` for the Arize plugin shim
   - Check `~/.arize/harness/config.yaml` for the `harnesses.opencode` block
   - If both are present -> Jump to [Validate](#validate) or [Troubleshoot](#troubleshoot)

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

**Important:** Users must run this setup before tracing will work. The `send_span()` function requires `~/.arize/harness/config.yaml` to exist for backend credential resolution.

### Ask the user for:

1. **Backend choice**: Phoenix or Arize AX
2. **Credentials** (only if no existing config):
   - Phoenix: endpoint URL (default: `http://localhost:6006`), optional API key
   - Arize AX: API key and Space ID
3. **OTLP Endpoint** (Arize AX only, optional): For hosted Arize instances using a custom endpoint. Defaults to `otlp.arize.com:443`.
4. **Project name** (optional): defaults to `"opencode"`, stored under `harnesses.opencode.project_name`
5. **User ID** (optional): Set `ARIZE_USER_ID` env var to identify spans by user (useful for teams)

### Write the config

The config file at `~/.arize/harness/config.yaml` is the single source of truth for backend credentials and per-harness settings. Create the directory structure if needed: `mkdir -p ~/.arize/harness/{bin,run,logs,state/opencode}`

**Important: read-merge-write.** If `~/.arize/harness/config.yaml` already exists, read it first, then merge in the new or updated fields (e.g., add/update the `harnesses.opencode` entry) while preserving existing backend credentials. Only prompt for backend credentials if no existing config is found.

**Phoenix:**
```yaml
harnesses:
  opencode:
    project_name: opencode
    target: phoenix
    endpoint: <endpoint>
    api_key: ""   # set when the Phoenix instance requires auth (Phoenix Cloud)
```

If Phoenix requires authentication (e.g. Phoenix Cloud), set the API key here under
`api_key`, or export `PHOENIX_API_KEY` in the environment — it is sent as a
`Authorization: Bearer <key>` header. The env var takes precedence over the YAML value.
Leave `api_key: ""` for an unauthenticated local Phoenix.

**Arize AX:**
```yaml
harnesses:
  opencode:
    project_name: opencode
    target: arize
    endpoint: otlp.arize.com:443
    api_key: <key>
    space_id: <id>
```

If the user has a custom OTLP endpoint, set it in `harnesses.opencode.endpoint`.

### Activate the opencode plugin

opencode auto-discovers plugins under `~/.config/opencode/plugin/` ([config docs](https://opencode.ai/docs/config/)) — there is no `opencode.json` edit and no host settings file to register. The installer drops the Arize plugin shim at `~/.config/opencode/plugin/arize-tracing.ts`; opencode picks it up on next launch.

Install or reinstall via the installer:

```bash
./install.sh opencode
```

To uninstall:

```bash
./install.sh uninstall opencode
```

Uninstall deletes the plugin file at `~/.config/opencode/plugin/arize-tracing.ts` (only if it carries the Arize header marker — the installer never touches the user's own plugins) and removes the `harnesses.opencode` block from `~/.arize/harness/config.yaml`.

### Validate

1. **Config exists**: Run `cat ~/.arize/harness/config.yaml` to verify the config file exists and has correct backend credentials under `harnesses.opencode`.
2. **Phoenix** (if applicable): Run `curl -sf <endpoint>/v1/traces >/dev/null` to check connectivity.
3. **Plugin installed**: Verify `~/.config/opencode/plugin/arize-tracing.ts` exists and starts with the Arize header marker.
4. **Reconciler entry point**: Verify the reconciler binary exists at `~/.arize/harness/venv/bin/arize-hook-opencode` (or `~/.arize/harness/venv/Scripts/arize-hook-opencode.exe` on Windows). The shim spawns this binary by absolute path — it does not rely on PATH resolution. `install.sh` installs it as a venv entry point.

### Confirm

Tell the user:
- Config saved to `~/.arize/harness/config.yaml`
- opencode plugin shim installed at `~/.config/opencode/plugin/arize-tracing.ts`
- opencode auto-discovers the plugin on next launch — no `opencode.json` edit required
- Spans are sent directly to the backend from the reconciler — no background process needed
- After saving, open a new opencode session and traces will appear in their Phoenix UI or Arize AX dashboard under the project name
- Mention `ARIZE_DRY_RUN=true` to test without sending data (set as env var before launching opencode)
- Mention `ARIZE_VERBOSE=true` for debug output
- Errors and reconciler stderr are always written to `~/.arize/harness/logs/opencode.log` (the adapter redirects Python stderr there via `ARIZE_LOG_FILE`); set `ARIZE_VERBOSE=true` in the shell before launching opencode to also capture routine reconciler activity (snapshot ingest, span emits, dedup hits)
- Toggle tracing on/off via `ARIZE_TRACE_ENABLED` env var (must be exported in the user's shell before launching opencode — the shim and reconciler inherit host env vars)
- Tail the log file at `~/.arize/harness/logs/opencode.log` for real-time debugging
- Mention `ARIZE_TRACE_DEBUG=true` to dump raw snapshot payloads under `~/.arize/harness/state/debug/` (files are named `opencode_reconcile_<ts>.yaml` / `opencode_close_<ts>.yaml`) for inspection

## Architecture (How spans are produced)

opencode is fundamentally different from every other harness in this repo: extensions are [plugins](https://opencode.ai/docs/plugins/) loaded **in-process** inside opencode's Bun runtime. The Arize integration is split into two pieces:

1. **TypeScript plugin shim** at `~/.config/opencode/plugin/arize-tracing.ts`. A dumb bridge. On `message.updated` (assistant completed) and `session.idle` it pulls the authoritative session snapshot via `client.session.messages({ path: { id } })` (see the [opencode SDK docs](https://opencode.ai/docs/sdk/)), then spawns `arize-hook-opencode` detached and pipes the snapshot to stdin. The shim contains no tracing logic.
2. **Python snapshot reconciler** (`arize-hook-opencode`). Reads the snapshot, walks `{info, parts}[]`, and emits any NEW `Turn`/`LLM`/`TOOL` spans deduped by message id and tool `callID`. opencode's `AssistantMessage` already carries final, cumulative `tokens` and `cost`, so no per-delta coalescing is needed.

## Span tree

Each trace covers one **turn** (one user prompt → the assistant's response → `session.idle`). The tree is three levels deep:

| Span | Kind | Description |
|------|------|-------------|
| `Turn` | CHAIN | Root span. `input.value` is the user prompt; `output.value` is the assistant's final text. Timestamps come from `message.time.created` / `time.completed`. |
| `LLM: <model>` | LLM | Child of `Turn`. The assistant message. Carries `llm.model_name`, `llm.provider`, prompt/completion/reasoning token counts, cache read/write tokens, and `llm.cost`. |
| `<tool>` | TOOL | Child of `Turn`. One per completed `ToolPart`. Records `tool.name`, redacted input/output, and `tool.command`/`tool.file_path`/`tool.query`/`tool.url` where applicable. Timestamps come from `toolPart.state.time.start` / `.end`. |

## Troubleshoot

Common issues and fixes for opencode:

| Problem | Fix |
|---------|-----|
| Traces not appearing | Verify config exists: `cat ~/.arize/harness/config.yaml`. Check reconciler log: `tail -20 ~/.arize/harness/logs/opencode.log`. Confirm the plugin is in place: `ls ~/.config/opencode/plugin/arize-tracing.ts`. |
| Plugin not loading | opencode loads plugins from `~/.config/opencode/plugin/` on startup. If the file exists but isn't loading, restart opencode and check the opencode CLI output for plugin errors. |
| Reconciler entry point missing | The shim spawns the reconciler by absolute path; verify the binary exists at `~/.arize/harness/venv/bin/arize-hook-opencode` (or `~/.arize/harness/venv/Scripts/arize-hook-opencode.exe` on Windows). Rerun `./install.sh opencode` to reinstall the venv entry point. |
| Spans appear partial / missing tool spans | Snapshots are pulled on `message.updated` (assistant complete) and `session.idle`. Pending or running tool parts won't emit a span until they reach `completed` or `error` state. Wait for the turn to finish. |
| Duplicate spans | The reconciler dedupes by message id and tool `callID`. If you still see duplicates, set `ARIZE_VERBOSE=true` and check `~/.arize/harness/logs/opencode.log` for dedup hits to confirm state tracking is working. |
| Sub-agent (`task` tool) trace not linked to parent | Known v1 limitation: opencode's built-in `task` tool spawns sub-agents with their own `sessionID`, which produce their own independent traces. They are not linked back to the parent session's trace. |
| Phoenix unreachable | Verify Phoenix is running: `curl -sf <endpoint>/v1/traces` |
| Want to test without sending | Set `ARIZE_DRY_RUN=true` env var before launching opencode |
| Want verbose logging | Set `ARIZE_VERBOSE=true` env var before launching opencode |
| Want raw snapshot payloads for inspection | Set `ARIZE_TRACE_DEBUG=true` env var; payloads land under `~/.arize/harness/state/debug/` as `opencode_reconcile_<ts>.yaml` / `opencode_close_<ts>.yaml` |
| Wrong project name | Set `harnesses.opencode.project_name` in `~/.arize/harness/config.yaml` (default: `"opencode"`) |
| Spans missing user attribution | Set `ARIZE_USER_ID` env var before launching opencode |
| Tracing not toggling | Ensure `ARIZE_TRACE_ENABLED` is exported in your shell, not just set — the opencode process and any plugin-spawned reconciler inherit host env vars |
