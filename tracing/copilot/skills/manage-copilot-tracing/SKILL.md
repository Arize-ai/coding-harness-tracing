---
name: manage-copilot-tracing
description: Set up and configure Arize tracing for GitHub Copilot sessions. Use when users want to set up tracing, configure Arize AX or Phoenix for Copilot, enable/disable tracing, or troubleshoot tracing issues. Triggers on "set up copilot tracing", "configure Arize for Copilot", "configure Phoenix for Copilot", "enable copilot tracing", "setup-copilot-tracing", or any request about connecting GitHub Copilot to Arize or Phoenix for observability.
---

# Setup Copilot Tracing

Configure OpenInference tracing for **GitHub Copilot** in VS Code Copilot. Spans are sent directly to the backend from hooks -- no background process or backend-specific dependencies are needed in the user's environment.

## How to Use This Skill

**This skill follows a decision tree workflow.** Start by asking the user where they are in the setup process:

1. **Do they already have credentials?**
   - Yes -> Jump to [Configure Settings](#configure-settings)
   - No -> Continue to step 2

2. **Which backend do they want to use?**
   - Phoenix (self-hosted) -> Go to [Set Up Phoenix](#set-up-phoenix)
   - Arize AX (cloud) -> Go to [Set Up Arize AX](#set-up-arize-ax)

3. **Are they troubleshooting?**
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
4. **Project name** (optional): defaults to `"copilot"`, stored under `harnesses.copilot.project_name`
5. **User ID** (optional): Set `ARIZE_USER_ID` env var to identify spans by user (useful for teams)

### Write the config

The config file at `~/.arize/harness/config.json` is the single source of truth for backend credentials and per-harness settings. Create the directory structure if needed: `mkdir -p ~/.arize/harness/{bin,run,logs,state/copilot}`

**Important: read-merge-write.** If `~/.arize/harness/config.json` already exists, read it first, then merge in the new or updated fields (e.g., add/update the `harnesses.copilot` entry) while preserving existing backend credentials. Only prompt for backend credentials if no existing config is found.

**Phoenix:**
```json
{
  "harnesses": {
    "copilot": {
      "project_name": "copilot",
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
    "copilot": {
      "project_name": "copilot",
      "target": "arize",
      "endpoint": "otlp.arize.com:443",
      "api_key": "<key>",
      "space_id": "<id>"
    }
  }
}
```

If the user has a custom OTLP endpoint, set it in `harnesses.copilot.endpoint`.

### Activate Copilot hooks

Copilot hooks are registered in a single `.github/hooks/hooks.json` file. Create it (or merge Arize entries into it if it already exists):

```json
{
  "hooks": {
    "SessionStart":      [{"type": "command", "command": "~/.arize/harness/venv/bin/arize-hook-copilot-session-start"}],
    "UserPromptSubmit":  [{"type": "command", "command": "~/.arize/harness/venv/bin/arize-hook-copilot-user-prompt"}],
    "PreToolUse":        [{"type": "command", "command": "~/.arize/harness/venv/bin/arize-hook-copilot-pre-tool"}],
    "PostToolUse":       [{"type": "command", "command": "~/.arize/harness/venv/bin/arize-hook-copilot-post-tool"}],
    "Stop":              [{"type": "command", "command": "~/.arize/harness/venv/bin/arize-hook-copilot-stop"}],
    "SubagentStop":      [{"type": "command", "command": "~/.arize/harness/venv/bin/arize-hook-copilot-subagent-stop"}]
  }
}
```

All `command` values should be absolute paths to the venv binary (e.g. `~/.arize/harness/venv/bin/arize-hook-copilot-<event>`).

### Validate

1. **Config exists**: Run `cat ~/.arize/harness/config.json` to verify the config file exists and has correct backend credentials.
2. **Phoenix** (if applicable): Run `curl -sf <endpoint>/v1/traces >/dev/null` to check connectivity.
3. **Hooks active**: Verify `.github/hooks/hooks.json` exists in the project root and each `command` path is the absolute venv binary path.
4. **Quick dry-run test** (optional):
   ```bash
   echo '{"hookEventName":"PreToolUse","tool_name":"test"}' | ARIZE_DRY_RUN=true arize-hook-copilot-pre-tool
   ```

### Confirm

Tell the user:
- Config saved to `~/.arize/harness/config.json`
- Copilot hooks activated via `.github/hooks/hooks.json`
- Spans are sent directly to the backend from hooks -- no background process needed
- After saving, open a new Copilot session and traces will appear in their Phoenix UI or Arize AX dashboard under the project name
- Mention `ARIZE_DRY_RUN=true` to test without sending data (set as env var before launching Copilot)
- Mention `ARIZE_VERBOSE=true` for debug output
- Errors are always written to `~/.arize/harness/logs/copilot.log`; set `ARIZE_VERBOSE=true` in the shell before launching VS Code / Copilot CLI to also capture routine hook activity

## Hook Events

Copilot fires 6 hook events. Each event maps to a span:

| Event | Span Name | Kind | Description |
|-------|-----------|------|-------------|
| `SessionStart` | `Session Start` | CHAIN | Session initialization |
| `UserPromptSubmit` | `User Prompt` | CHAIN | User prompt text |
| `PreToolUse` | `Tool: {name}` | TOOL | Tool start; **must print permission response to stdout** |
| `PostToolUse` | `Tool: {name}` | TOOL | Tool result |
| `Stop` | `Agent Stop` | LLM | Per-turn completion; transcript at `~/.copilot/session-state/<session_id>/events.jsonl` is parsed for model name, prompt, and tool-call count |
| `SubagentStop` | `Subagent: {id}` | CHAIN | Subagent completion |

### PreToolUse permission response

The pre-tool handler must print a permission response to stdout:

```json
{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}
```

All other handlers print `{"continue": true}`.

## Troubleshoot

| Problem | Fix |
|---------|-----|
| Traces not appearing | Verify config exists: `cat ~/.arize/harness/config.json`. Check hook log: `tail -20 ~/.arize/harness/logs/copilot.log` |
| Hooks not firing | Verify `.github/hooks/hooks.json` exists in the project root and each `command` path is the absolute venv binary path |
| `PreToolUse` blocking tools | Check the handler prints the correct permission JSON. Test: `echo '{"hookEventName":"PreToolUse","tool_name":"test"}' \| arize-hook-copilot-pre-tool` |
| Config missing | Run the installer or create `~/.arize/harness/config.json` manually (include `harnesses.copilot` section) |
| Phoenix unreachable | Verify Phoenix is running: `curl -sf <endpoint>/v1/traces` |
| Want to test without sending | Set `ARIZE_DRY_RUN=true` env var before launching Copilot |
| Want verbose logging | Set `ARIZE_VERBOSE=true` env var before launching Copilot |
| Wrong project name | Set `harnesses.copilot.project_name` in `~/.arize/harness/config.json` (default: `"copilot"`) |
| Spans missing user attribution | Set `ARIZE_USER_ID` env var before launching Copilot |
