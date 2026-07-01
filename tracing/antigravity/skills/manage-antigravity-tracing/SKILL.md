---
name: manage-antigravity-tracing
description: Set up and configure Arize tracing for Google Antigravity CLI/IDE sessions. Use when users want to set up tracing, configure Arize AX or Phoenix for Antigravity, enable/disable tracing, or troubleshoot tracing issues. Triggers on "set up antigravity tracing", "configure Arize for Antigravity", "configure Phoenix for Antigravity", "enable antigravity tracing", "setup-antigravity-tracing", or any request about connecting the Antigravity CLI/IDE to Arize or Phoenix for observability.
---

# Setup Antigravity Tracing

Configure OpenInference tracing for the **Antigravity CLI/IDE** (Google's Gemini-lineage coding agent) to Arize AX (cloud) or Phoenix (self-hosted). Spans are sent directly to the backend from hooks -- no background process or backend-specific dependencies are needed in the user's environment.

This harness is **transcript-driven**: Antigravity hooks are a control plane that carries only pointers (`transcriptPath`, `conversationId`, `workspacePaths`). The real model and tool content lives in the transcript file the agent writes. The Stop hook parses `transcript_full.jsonl` and reconstructs spans -- one trace per user turn.

## How to Use This Skill

**This skill follows a decision tree workflow.** Start by asking the user where they are in the setup process:

1. **Is the harness already installed?**
   - Check `~/.gemini/config/hooks.json` for a top-level `"arize-tracing"` key containing `PreInvocation` and `Stop` entries
   - Check `~/.arize/harness/config.json` for the `harnesses.antigravity` block
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

**Important:** Users must run this setup before tracing will work. The `send_span()` function requires `~/.arize/harness/config.json` to exist for backend credential resolution.

### Ask the user for:

1. **Backend choice**: Phoenix or Arize AX
2. **Credentials** (only if no existing config):
   - Phoenix: endpoint URL (default: `http://localhost:6006`), optional API key
   - Arize AX: API key and Space ID
3. **OTLP Endpoint** (Arize AX only, optional): For hosted Arize instances using a custom endpoint. Defaults to `otlp.arize.com:443`.
4. **Project name** (optional): defaults to `"antigravity"`, stored under `harnesses.antigravity.project_name`
5. **User ID** (optional): Set `ARIZE_USER_ID` env var to identify spans by user (useful for teams)

### Write the config

The config file at `~/.arize/harness/config.json` is the single source of truth for backend credentials and per-harness settings. Create the directory structure if needed: `mkdir -p ~/.arize/harness/{bin,run,logs,state/antigravity}`

**Important: read-merge-write.** If `~/.arize/harness/config.json` already exists, read it first, then merge in the new or updated fields (e.g., add/update the `harnesses.antigravity` entry) while preserving existing backend credentials. Only prompt for backend credentials if no existing config is found.

**Phoenix:**
```json
{
  "harnesses": {
    "antigravity": {
      "project_name": "antigravity",
      "target": "phoenix",
      "endpoint": "<endpoint>",
      "api_key": ""
    }
  }
}
```

**Arize AX:**
```json
{
  "harnesses": {
    "antigravity": {
      "project_name": "antigravity",
      "target": "arize",
      "endpoint": "otlp.arize.com:443",
      "api_key": "<key>",
      "space_id": "<id>"
    }
  }
}
```

If the user has a custom OTLP endpoint, set it in `harnesses.antigravity.endpoint`.

### Activate Antigravity hooks

Antigravity uses `~/.gemini/config/hooks.json` for hook registration (note: this is **not** `settings.json` like Gemini CLI). The schema is inverted vs. Gemini -- the top level maps **hook name -> { event -> handlers }**, and each event's value is a flat list of handler objects (no `matcher` wrapper). `timeout` is in **seconds**.

Install or reinstall via the installer:

```bash
./install.sh antigravity
```

To uninstall:

```bash
./install.sh uninstall antigravity
```

The installer registers a top-level `"arize-tracing"` block in `~/.gemini/config/hooks.json` with handlers for the `PreInvocation` and `Stop` events, each pointing at the absolute venv binary path (e.g., `~/.arize/harness/venv/bin/arize-hook-antigravity-stop`).

### Validate

1. **Config exists**: Run `cat ~/.arize/harness/config.json` to verify the config file exists and has correct backend credentials under `harnesses.antigravity`.
2. **Phoenix** (if applicable): Run `curl -sf <endpoint>/v1/traces >/dev/null` to check connectivity.
3. **Hooks active**: Verify `~/.gemini/config/hooks.json` contains a top-level `"arize-tracing"` entry with `PreInvocation` and `Stop` handler lists.
4. **Quick dry-run test** (optional):
   ```bash
   echo '{"conversationId":"test","workspacePaths":["/tmp"],"transcriptPath":"/tmp/transcript.jsonl"}' \
     | ARIZE_DRY_RUN=true arize-hook-antigravity-stop
   ```

### Confirm

Tell the user:
- Config saved to `~/.arize/harness/config.json`
- Antigravity hooks activated via `~/.gemini/config/hooks.json`
- Spans are sent directly to the backend from hooks -- no background process needed
- After saving, open a new Antigravity session and traces will appear in their Phoenix UI or Arize AX dashboard under the project name
- Mention `ARIZE_DRY_RUN=true` to test without sending data (set as env var before launching Antigravity)
- Mention `ARIZE_VERBOSE=true` for debug output
- Errors are always written to `~/.arize/harness/logs/antigravity.log`; set `ARIZE_VERBOSE=true` in the shell before launching Antigravity to also capture routine hook activity
- Toggle tracing on/off via `ARIZE_TRACE_ENABLED` env var (must be exported in the user's shell -- Antigravity hooks read host env vars)
- Tail the log file at `~/.arize/harness/logs/antigravity.log` for real-time debugging

## Hook Events

Antigravity fires two hook events that drive tracing. The harness is **transcript-driven**: hook payloads carry only pointers (`conversationId`, `workspacePaths`, `transcriptPath`, `artifactDirectoryPath`), and the actual model and tool content is reconstructed from the transcript file (`transcript_full.jsonl`). The Stop handler parses the transcript and emits one trace per user turn.

| Event | Trigger | Behavior |
|-------|---------|----------|
| `PreInvocation` | Before each model call | Backstop -- flushes any *earlier* turn whose `Stop` was missed (crash/kill). Idempotent. |
| `Stop` | Agent loop ends | Parses the transcript, reconstructs spans for the just-finished turn, and sends them. |

Per user turn, the Stop handler emits:
- one **Turn** span (`CHAIN` kind, fresh `trace_id` -- one trace per turn),
- one **LLM** span per `PLANNER_RESPONSE` model response,
- one **TOOL** span per tool call.

Turn boundaries come from the transcript's `USER_INPUT` records (the ground-truth user-message boundary), not from the hook events themselves.

**Stop output contract:** the Stop hook prints exactly `{}` to stdout. It never emits `{"decision": "continue"}` (which would force the agent loop to re-enter and produce an infinite loop).

### Token counts are intentionally omitted

Antigravity withholds per-turn token usage from every local surface (transcript, SQLite store, CLI logs, hook payloads). This was verified empirically. The harness deliberately leaves `llm.token_count.*` attributes unset -- absent reads correctly in Arize, while zeros would look like a bug. Tokens are not recovered by intercepting network traffic.

## Troubleshoot

Common issues and fixes for Antigravity:

| Problem | Fix |
|---------|-----|
| Traces not appearing | Verify config exists: `cat ~/.arize/harness/config.json`. Check hook log: `tail -20 ~/.arize/harness/logs/antigravity.log` |
| Hooks not firing | Verify `~/.gemini/config/hooks.json` contains a top-level `"arize-tracing"` block with `PreInvocation` and `Stop` entries pointing at absolute venv binary paths |
| Agent loops or won't exit | Check that the Stop handler is printing exactly `{}` on stdout -- a stray `{"decision": "continue"}` will force re-entry. Inspect `~/.arize/harness/logs/antigravity.log`. |
| Config missing | Run `./install.sh antigravity` or create `~/.arize/harness/config.json` manually (include `harnesses.antigravity` section) |
| Phoenix unreachable | Verify Phoenix is running: `curl -sf <endpoint>/v1/traces` |
| Want to test without sending | Set `ARIZE_DRY_RUN=true` env var before launching Antigravity |
| Want verbose logging | Set `ARIZE_VERBOSE=true` env var before launching Antigravity |
| Wrong project name | Set `harnesses.antigravity.project_name` in `~/.arize/harness/config.json` (default: `"antigravity"`) |
| Spans missing user attribution | Set `ARIZE_USER_ID` env var before launching Antigravity |
| Tracing not toggling | Ensure `ARIZE_TRACE_ENABLED` is exported in your shell, not just set |
| Token counts missing | Expected -- Antigravity does not expose per-turn token usage on any local surface, so the harness intentionally leaves token attributes unset |
| Transcript not found | The Stop handler reads `transcriptPath` from hook stdin. Confirm the path resolves and the file is non-empty: `tail -5 "$(jq -r .transcriptPath < /tmp/stop-payload.json)"` |
