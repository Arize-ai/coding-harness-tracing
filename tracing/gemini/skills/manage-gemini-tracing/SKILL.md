---
name: manage-gemini-tracing
description: Set up and configure Arize tracing for Gemini CLI sessions. Use when users want to set up tracing, configure Arize AX or Phoenix for Gemini, enable/disable tracing, or troubleshoot tracing issues. Triggers on "set up gemini tracing", "configure Arize for Gemini", "configure Phoenix for Gemini", "enable gemini tracing", "setup-gemini-tracing", or any request about connecting Gemini CLI to Arize or Phoenix for observability.
---

# Setup Gemini Tracing

Configure OpenInference tracing for **Gemini CLI** sessions to Arize AX (cloud) or Phoenix (self-hosted). Spans are sent directly to the backend from hooks -- no background process or backend-specific dependencies are needed in the user's environment.

## How to Use This Skill

**This skill follows a decision tree workflow.** Start by asking the user where they are in the setup process:

1. **Is the harness already installed?**
   - Check `~/.gemini/settings.json` for 8 hook entries with `name: arize-tracing`
   - Check `~/.arize/harness/config.json` for the `harnesses.gemini` block
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
4. **Project name** (optional): defaults to `"gemini"`, stored under `harnesses.gemini.project_name`
5. **User ID** (optional): Set `ARIZE_USER_ID` env var to identify spans by user (useful for teams)

### Write the config

The config file at `~/.arize/harness/config.json` is the single source of truth for backend credentials and per-harness settings. Create the directory structure if needed: `mkdir -p ~/.arize/harness/{bin,run,logs,state/gemini}`

**Important: read-merge-write.** If `~/.arize/harness/config.json` already exists, read it first, then merge in the new or updated fields (e.g., add/update the `harnesses.gemini` entry) while preserving existing backend credentials. Only prompt for backend credentials if no existing config is found.

**Phoenix:**
```json
{
  "harnesses": {
    "gemini": {
      "project_name": "gemini",
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
    "gemini": {
      "project_name": "gemini",
      "target": "arize",
      "endpoint": "otlp.arize.com:443",
      "api_key": "<key>",
      "space_id": "<id>"
    }
  }
}
```

If the user has a custom OTLP endpoint, set it in `harnesses.gemini.endpoint`.

### Activate Gemini hooks

Gemini CLI uses `~/.gemini/settings.json` for hook registration. Hooks are configured under `hooks.<EventName>` as an array of matcher/hook objects.

Install or reinstall via the installer:

```bash
./install.sh gemini
```

To uninstall:

```bash
./install.sh uninstall gemini
```

The installer registers all 8 hook events (`SessionStart`, `SessionEnd`, `BeforeAgent`, `AfterAgent`, `BeforeModel`, `AfterModel`, `BeforeTool`, `AfterTool`) in `~/.gemini/settings.json` with `name: arize-tracing` on each entry.

### Validate

1. **Config exists**: Run `cat ~/.arize/harness/config.json` to verify the config file exists and has correct backend credentials under `harnesses.gemini`.
2. **Phoenix** (if applicable): Run `curl -sf <endpoint>/v1/traces >/dev/null` to check connectivity.
3. **Hooks active**: Verify `~/.gemini/settings.json` contains 8 hook entries with `name: arize-tracing`.
4. **Quick dry-run test** (optional):
   ```bash
   echo '{"event":"BeforeModel"}' | ARIZE_DRY_RUN=true arize-hook-gemini-before-model
   ```

### Confirm

Tell the user:
- Config saved to `~/.arize/harness/config.json`
- Gemini CLI hooks activated via `~/.gemini/settings.json`
- Spans are sent directly to the backend from hooks -- no background process needed
- After saving, open a new Gemini CLI session and traces will appear in their Phoenix UI or Arize AX dashboard under the project name
- Mention `ARIZE_DRY_RUN=true` to test without sending data (set as env var before launching Gemini CLI)
- Mention `ARIZE_VERBOSE=true` for debug output
- Errors are always written to `~/.arize/harness/logs/gemini.log`; set `ARIZE_VERBOSE=true` in the shell before launching Gemini CLI to also capture routine hook activity
- Toggle tracing on/off via `ARIZE_TRACE_ENABLED` env var (must be exported in the user's shell -- Gemini hooks read host env vars)
- Tail the log file at `~/.arize/harness/logs/gemini.log` for real-time debugging

## Hook Events

Gemini CLI fires 8 hook events. Each event is registered in `~/.gemini/settings.json` and maps to a dedicated CLI entry point.

| Event | Span Name | Kind | Description |
|-------|-----------|------|-------------|
| `SessionStart` | Session Start | CHAIN | Session initialization |
| `SessionEnd` | Session End | CHAIN | Session termination |
| `BeforeAgent` | Agent Turn | CHAIN | User prompt to agent |
| `AfterAgent` | Agent Turn | CHAIN | Agent completion |
| `BeforeModel` | LLM Call | LLM | Model invocation start with prompt |
| `AfterModel` | LLM Call | LLM | Model response with tokens |
| `BeforeTool` | Tool: {name} | TOOL | Tool invocation start |
| `AfterTool` | Tool: {name} | TOOL | Tool result |

## Troubleshoot

Common issues and fixes for Gemini CLI:

| Problem | Fix |
|---------|-----|
| Traces not appearing | Verify config exists: `cat ~/.arize/harness/config.json`. Check hook log: `tail -20 ~/.arize/harness/logs/gemini.log` |
| Hooks not firing | Verify `~/.gemini/settings.json` contains the 8 hook entries with `name: arize-tracing` for all events |
| Config missing | Run `./install.sh gemini` or create `~/.arize/harness/config.json` manually (include `harnesses.gemini` section) |
| Phoenix unreachable | Verify Phoenix is running: `curl -sf <endpoint>/v1/traces` |
| Want to test without sending | Set `ARIZE_DRY_RUN=true` env var before launching Gemini CLI |
| Want verbose logging | Set `ARIZE_VERBOSE=true` env var before launching Gemini CLI |
| Wrong project name | Set `harnesses.gemini.project_name` in `~/.arize/harness/config.json` (default: `"gemini"`) |
| Spans missing user attribution | Set `ARIZE_USER_ID` env var before launching Gemini CLI |
| Tracing not toggling | Ensure `ARIZE_TRACE_ENABLED` is exported in your shell, not just set |
