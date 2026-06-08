---
name: manage-cursor-tracing
description: Set up and configure Arize tracing for Cursor IDE sessions. Use when users want to set up tracing, configure Arize AX or Phoenix for Cursor, enable/disable tracing, or troubleshoot tracing issues. Triggers on "set up cursor tracing", "configure Arize for Cursor", "configure Phoenix for Cursor", "enable cursor tracing", "setup-cursor-tracing", or any request about connecting Cursor to Arize or Phoenix for observability.
---

# Setup Cursor Tracing

Configure OpenInference tracing for Cursor IDE sessions to Arize AX (cloud) or Phoenix (self-hosted). Spans are sent directly to the backend from hooks -- no background process or backend-specific dependencies are needed in the user's environment.

## How to Use This Skill

**This skill follows a decision tree workflow.** Start by asking the user where they are in the setup process:

1. **First, determine the install type.** Was Arize tracing installed as a **Cursor plugin** (via `/add-plugin` in Cursor 2.5+, which registers hooks automatically), or **manually** by running `install.sh`?
   - If unsure, ask the user directly: "Did you install Arize tracing as a Cursor plugin (via /add-plugin), or by running install.sh?"
   - Heuristic 1: Check Cursor's installed-plugins location for a `cursor-tracing` plugin directory.
   - Heuristic 2: Check whether `.cursor/hooks.json` already contains `arize-hook-cursor` entries -- if so, this is a manual `install.sh` install; if absent, it's a plugin install (or a fresh setup).
   - This branch only matters for the [Activate Cursor hooks](#activate-cursor-hooks) step below. The credentials/config step is identical for both install types.

2. **Do they already have credentials?**
   - Yes -> Jump to [Configure Settings](#configure-settings)
   - No -> Continue to step 3

3. **Which backend do they want to use?**
   - Phoenix (self-hosted) -> Go to [Set Up Phoenix](#set-up-phoenix)
   - Arize AX (cloud) -> Go to [Set Up Arize AX](#set-up-arize-ax)

4. **Are they troubleshooting?**
   - Yes -> Jump to [Troubleshoot](#troubleshoot)

**Important:** Only follow the relevant path for the user's needs. Don't go through all sections.

**Plugin users, please read:** The marketplace install via `/add-plugin` registers Cursor hooks automatically but cannot run an interactive credentials wizard. Run this skill once after `/add-plugin` so it can write `~/.arize/harness/config.yaml` with your backend credentials -- without that file, the hooks will fail open (no-op) and no spans will be sent.

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

**No Python dependencies are needed.** Both Phoenix and Arize AX use HTTP/JSON — no additional Python dependencies are needed.

Then proceed to [Configure Settings](#configure-settings). If the user is on an on-prem instance, remind them to provide their custom endpoint.

## Configure Settings

**Important:** Users must run this setup before tracing will work. The `send_span()` function requires `~/.arize/harness/config.yaml` to exist for backend credential resolution. This step is identical for plugin installs and manual `install.sh` installs.

> **Env var override:** Backend credentials can also be supplied via env vars (`ARIZE_API_KEY` + `ARIZE_SPACE_ID` for Arize AX, or `PHOENIX_ENDPOINT` for Phoenix), and they take precedence over `config.yaml`. However, `config.yaml` is the recommended primary path: Cursor does not reliably inject shell env vars into hook processes -- notably a GUI-launched Cursor on macOS will not see vars from your shell profile -- so env-var-only setups silently fail to resolve credentials.

### Ask the user for:

1. **Backend choice**: Phoenix or Arize AX
2. **Credentials** (only if no existing config):
   - Phoenix: endpoint URL (default: `http://localhost:6006`), optional API key
   - Arize AX: API key and Space ID
3. **OTLP Endpoint** (Arize AX only, optional): For hosted Arize instances using a custom endpoint. Defaults to `otlp.arize.com:443`.
4. **Project name** (optional): defaults to `"cursor"`, stored under `harnesses.cursor.project_name`
5. **User ID** (optional): Set `ARIZE_USER_ID` env var to identify spans by user (useful for teams)

### Write the config

The config file at `~/.arize/harness/config.yaml` is the single source of truth for backend credentials and per-harness settings. Create the directory structure if needed: `mkdir -p ~/.arize/harness/{bin,run,logs,state/cursor}`

**Important: read-merge-write.** If `~/.arize/harness/config.yaml` already exists, read it first, then merge in the new or updated fields (e.g., add/update the `harnesses.cursor` entry) while preserving existing backend credentials. Only prompt for backend credentials if no existing config is found.

**Phoenix:**
```yaml
harnesses:
  cursor:
    project_name: cursor
    target: phoenix
    endpoint: <endpoint>
    api_key: ""
```

**Arize AX:**
```yaml
harnesses:
  cursor:
    project_name: cursor
    target: arize
    endpoint: otlp.arize.com:443
    api_key: <key>
    space_id: <id>
```

If the user has a custom OTLP endpoint, set it in `harnesses.cursor.endpoint`.

### Activate Cursor hooks

**This step depends on install type** (see [How to Use This Skill](#how-to-use-this-skill) step 1). Pick the branch that matches the user's install:

#### Plugin install (Cursor `/add-plugin`)

**Skip this step entirely.** The plugin's bundled `hooks/hooks.json` already registers every Cursor hook event automatically when the plugin is installed. There is nothing to write to `.cursor/hooks.json`.

After saving credentials in the previous step, tell the user to start a new Cursor session -- traces will begin flowing on the next interaction.

> **Warning:** Do NOT manually add `.cursor/hooks.json` entries on top of a plugin install. Cursor will then route each event to the handler twice (once via the plugin and once via your project file), producing duplicate spans for every hook. If the user already has manual `.cursor/hooks.json` entries pointing at `arize-hook-cursor` from a previous `install.sh` setup, remove those entries before relying on the plugin.

#### Manual install (`install.sh`)

Cursor uses a `.cursor/hooks.json` file in the project root to route hook events to the handler. All events route to a single `arize-hook-cursor` CLI entry point, which dispatches based on `hook_event_name` in the JSON payload.

Create `.cursor/hooks.json` in the user's project (or merge into it if it already exists):

```json
{
  "version": 1,
  "hooks": {
    "sessionStart": [{ "command": "~/.arize/harness/venv/bin/arize-hook-cursor" }],
    "sessionEnd": [{ "command": "~/.arize/harness/venv/bin/arize-hook-cursor" }],
    "beforeSubmitPrompt": [{ "command": "~/.arize/harness/venv/bin/arize-hook-cursor" }],
    "afterAgentResponse": [{ "command": "~/.arize/harness/venv/bin/arize-hook-cursor" }],
    "afterAgentThought": [{ "command": "~/.arize/harness/venv/bin/arize-hook-cursor" }],
    "beforeShellExecution": [{ "command": "~/.arize/harness/venv/bin/arize-hook-cursor" }],
    "afterShellExecution": [{ "command": "~/.arize/harness/venv/bin/arize-hook-cursor" }],
    "beforeMCPExecution": [{ "command": "~/.arize/harness/venv/bin/arize-hook-cursor" }],
    "afterMCPExecution": [{ "command": "~/.arize/harness/venv/bin/arize-hook-cursor" }],
    "beforeReadFile": [{ "command": "~/.arize/harness/venv/bin/arize-hook-cursor" }],
    "afterFileEdit": [{ "command": "~/.arize/harness/venv/bin/arize-hook-cursor" }],
    "stop": [{ "command": "~/.arize/harness/venv/bin/arize-hook-cursor" }],
    "beforeTabFileRead": [{ "command": "~/.arize/harness/venv/bin/arize-hook-cursor" }],
    "afterTabFileEdit": [{ "command": "~/.arize/harness/venv/bin/arize-hook-cursor" }],
    "postToolUse": [{ "command": "~/.arize/harness/venv/bin/arize-hook-cursor" }]
  }
}
```

If the user already has a `.cursor/hooks.json` with other hooks, merge the Arize entries into the existing arrays for each event.

### Validate

1. **Config exists**: Run `cat ~/.arize/harness/config.yaml` to verify the config file exists and has correct backend credentials.
2. **Phoenix** (if applicable): Run `curl -sf <endpoint>/v1/traces >/dev/null` to check connectivity.
3. **Hooks active**:
   - **Manual install**: Verify `.cursor/hooks.json` exists in the project root and contains the Arize hook entries.
   - **Plugin install**: No project-level `.cursor/hooks.json` is required -- the plugin registers hooks itself. Confirm the `cursor-tracing` plugin is listed as installed in Cursor.

### Confirm

Tell the user:
- Config saved to `~/.arize/harness/config.yaml`
- Cursor hooks are active (via the plugin for plugin installs, or via `.cursor/hooks.json` for manual installs)
- Spans are sent directly to the backend from hooks — no background process needed
- After saving, open a new Cursor session and traces will appear in their Phoenix UI or Arize AX dashboard under the project name
- Mention `ARIZE_DRY_RUN=true` to test without sending data (set as env var before launching Cursor)
- Mention `ARIZE_VERBOSE=true` for debug output
- Errors are always written to `~/.arize/harness/logs/cursor.log`; set `ARIZE_VERBOSE=true` in the shell before launching Cursor to also capture routine hook activity

## Hook Events

### IDE Hooks

Cursor IDE fires 15 hook events. Here's what each one traces:

| Event | Span Name | Kind | Description |
|-------|-----------|------|-------------|
| `sessionStart` | Session Start | CHAIN | Root span for the conversation; captures session metadata |
| `beforeSubmitPrompt` | User Prompt | CHAIN | Root span for the turn; captures prompt text, model, attachments |
| `afterAgentResponse` | Agent Response | LLM | LLM response text and model name; span is deferred and sent at end-of-turn (on `stop`) so it can carry per-turn token usage |
| `afterAgentThought` | Agent Thinking | CHAIN | Agent thinking/reasoning text |
| `beforeShellExecution` | (state push) | -- | Saves command and start time to disk state |
| `afterShellExecution` | Shell | TOOL | Merged span with command input and output |
| `beforeMCPExecution` | (state push) | -- | Saves tool name, input, and start time |
| `afterMCPExecution` | MCP: {tool} | TOOL | Merged span with tool input and result |
| `beforeReadFile` | Read File | TOOL | File path being read |
| `afterFileEdit` | File Edit | TOOL | File path and edit details |
| `beforeTabFileRead` | Tab Read File | TOOL | Tab file read (file path) |
| `afterTabFileEdit` | Tab File Edit | TOOL | Tab file edit (path and edits) |
| `postToolUse` | Tool: {name} | TOOL | Generic tool span; postToolUse is suppressed for tools with a dedicated handler (Shell, Read, File Edit, Tab ops, MCP) to avoid duplicate spans |
| `stop` | Agent Stop | CHAIN | Per-turn stop event with status / loop_count / duration metadata; per-turn token counts are attached to the deferred `Agent Response` (LLM) span when it is sent at end-of-turn |
| `sessionEnd` | Session End | CHAIN | End-of-session span with duration and final status |

Shell and MCP events use a disk-backed state stack to merge before/after context into single spans with both input and output.

### CLI Hooks

Cursor CLI currently emits a smaller hook surface than the IDE. The supported
CLI hooks in this package are:

- `sessionStart`
- `sessionEnd`
- `beforeShellExecution`
- `afterShellExecution`
- `afterFileEdit`
- `postToolUse`
- `stop`

Cursor CLI hooks do not currently emit afterAgentResponse or afterAgentThought.

Full Cursor CLI assistant and thinking coverage requires parsing --output-format stream-json, which is out of scope for this change.

### What We Capture

- **`sessionStart`** produces a `Session Start` CHAIN span that acts as the root for the conversation.
- **`sessionEnd`** produces a `Session End` CHAIN span with `cursor.session.duration_ms`, `cursor.session.final_status`, `cursor.session.reason`, and end-of-session token counts when available.
- **`stop`** produces an `Agent Stop` CHAIN span carrying per-turn status / loop_count / duration metadata. On the Cursor IDE, per-turn token usage is attached to the `Agent Response` (LLM) span instead: that span is deferred from `afterAgentResponse` and sent at end-of-turn when `stop` fires, populated from the `stop` payload with `llm.token_count.prompt`, `llm.token_count.completion`, `llm.token_count.cache_read`, `llm.token_count.cache_write`, `llm.token_count.total`, and `llm.model_name`. Cursor CLI does not emit `afterAgentResponse`, so there is no LLM span to attach to; for the CLI path, token counts remain on the `Agent Stop` / `Session End` CHAIN span as before.
- **`postToolUse`** produces a generic `Tool: <name>` span ONLY for tools without a dedicated handler. Shell, file read/edit, tab file ops, and MCP execution are handled by their dedicated `before*`/`after*` events; the generic postToolUse is suppressed for these to avoid duplicate spans.

Every span includes `cursor.conversation.id` as a span attribute. Since `sessionStart` and per-turn activity use different `trace_id` values, `cursor.conversation.id` is the recommended cross-trace join key in Arize. To gather all activity for a Cursor session regardless of trace, filter spans by `attributes.cursor.conversation.id = "<id>"`.

### Hooks JSON Example (IDE + CLI)

> **Plugin users: do NOT hand-write `.cursor/hooks.json` from this example.** The example below is a reference for the **manual `install.sh` path only**. Under a Cursor `/add-plugin` install, the plugin's bundled `hooks/hooks.json` already registers every event automatically; adding these entries on top of the plugin would route each event to the handler twice and produce duplicate spans for every hook. See [Activate Cursor hooks > Plugin install](#plugin-install-cursor-add-plugin).

When configuring `.cursor/hooks.json` on a manual `install.sh` install, include both IDE and CLI events:

```json
{
  "version": 1,
  "hooks": {
    "sessionStart": [{ "command": "~/.arize/harness/venv/bin/arize-hook-cursor" }],
    "sessionEnd": [{ "command": "~/.arize/harness/venv/bin/arize-hook-cursor" }],
    "beforeSubmitPrompt": [{ "command": "~/.arize/harness/venv/bin/arize-hook-cursor" }],
    "afterAgentResponse": [{ "command": "~/.arize/harness/venv/bin/arize-hook-cursor" }],
    "afterAgentThought": [{ "command": "~/.arize/harness/venv/bin/arize-hook-cursor" }],
    "beforeShellExecution": [{ "command": "~/.arize/harness/venv/bin/arize-hook-cursor" }],
    "afterShellExecution": [{ "command": "~/.arize/harness/venv/bin/arize-hook-cursor" }],
    "beforeMCPExecution": [{ "command": "~/.arize/harness/venv/bin/arize-hook-cursor" }],
    "afterMCPExecution": [{ "command": "~/.arize/harness/venv/bin/arize-hook-cursor" }],
    "beforeReadFile": [{ "command": "~/.arize/harness/venv/bin/arize-hook-cursor" }],
    "afterFileEdit": [{ "command": "~/.arize/harness/venv/bin/arize-hook-cursor" }],
    "stop": [{ "command": "~/.arize/harness/venv/bin/arize-hook-cursor" }],
    "beforeTabFileRead": [{ "command": "~/.arize/harness/venv/bin/arize-hook-cursor" }],
    "afterTabFileEdit": [{ "command": "~/.arize/harness/venv/bin/arize-hook-cursor" }],
    "postToolUse": [{ "command": "~/.arize/harness/venv/bin/arize-hook-cursor" }]
  }
}
```

## Troubleshoot

Common issues and fixes:

| Problem | Fix |
|---------|-----|
| Traces not appearing | Verify config exists: `cat ~/.arize/harness/config.yaml`. Check hook log: `tail -20 ~/.arize/harness/logs/cursor.log` |
| Config missing | Run the installer or create `~/.arize/harness/config.yaml` manually (include `harnesses.cursor` section) |
| Phoenix unreachable | Verify Phoenix is running: `curl -sf <endpoint>/v1/traces` |
| Hooks not firing (manual install) | Verify `.cursor/hooks.json` exists in the project root and paths are correct (use absolute paths) |
| Hooks not firing (plugin install) | Verify the `cursor-tracing` plugin is enabled in Cursor; start a fresh Cursor session after installation; check `~/.arize/harness/logs/cursor.log` for errors |
| Duplicate spans / events traced twice | A plugin install plus manual `.cursor/hooks.json` entries pointing at `arize-hook-cursor` will fire each hook twice. Remove the manual entries and keep only one install path. |
| Shell/MCP spans missing input | State push failed -- check that `~/.arize/harness/state/cursor/` is writable |
| Want to test without sending | Set `ARIZE_DRY_RUN=true` env var before launching Cursor |
| Want verbose logging | Set `ARIZE_VERBOSE=true` env var before launching Cursor |
| Wrong project name | Set `harnesses.cursor.project_name` in `~/.arize/harness/config.yaml` (default: `"cursor"`) |
| Spans missing user attribution | Set `ARIZE_USER_ID` env var before launching Cursor |
