---
name: manage-kiro-tracing
description: Set up and configure Arize tracing for Kiro CLI sessions. Use when users want to set up Kiro tracing, configure Arize AX or Phoenix for Kiro, enable/disable tracing, choose or set a default traced agent, or troubleshoot Kiro tracing issues. Triggers on "set up kiro tracing", "configure Arize for Kiro", "configure Phoenix for Kiro", "enable kiro tracing", "setup-kiro-tracing", "kiro agent tracing", or any request about connecting Kiro CLI to Arize or Phoenix for observability.
---

# Setup Kiro Tracing

Configure OpenInference tracing for **Kiro CLI** sessions to Arize AX (cloud) or Phoenix (self-hosted). Spans are sent directly to the backend from hooks — no background process or backend-specific dependencies are needed in the user's environment. Each traced session emits LLM turns, tool calls, cost in credits, model information, and turn duration.

## How to Use This Skill

**This skill follows a decision tree workflow.** Start by asking the user where they are in the setup process:

1. **Is the harness already installed?**
   - Check `~/.kiro/agents/` for an agent file containing `arize-hook-kiro` in its `hooks` block
   - Check `~/.arize/harness/config.json` for the `harnesses.kiro` block
   - If both are present → Jump to [Validate](#validate) or [Troubleshoot](#troubleshoot)

2. **Do they already have credentials?**
   - Yes → Jump to [Configure Settings](#configure-settings)
   - No → Continue to step 3

3. **Which backend do they want to use?**
   - Phoenix (self-hosted) → Go to [Set Up Phoenix](#set-up-phoenix)
   - Arize AX (cloud) → Go to [Set Up Arize AX](#set-up-arize-ax)

4. **Are they troubleshooting?**
   - Yes → Jump to [Troubleshoot](#troubleshoot)

**Important:** Only follow the relevant path for the user's needs. Don't go through all sections.

## Set Up Phoenix

Phoenix is self-hosted. No Python dependencies are needed for tracing — spans are sent directly via `send_span()` using stdlib `urllib`.

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

- **SaaS** → Uses the default endpoint (`otlp.arize.com:443`). Continue below.
- **On-prem** → The user will need to provide their custom OTLP endpoint (e.g., `otlp.mycompany.arize.com:443`). Ask for it and note it for the [Configure Settings](#configure-settings) step.

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

**Important:** Users must run this setup before tracing will work. The `send_span()` function requires `~/.arize/harness/config.json` to exist for backend credential resolution.

Configuration has two parts:

1. **Backend config** (`~/.arize/harness/config.json`) — backend credentials and per-harness settings, read by `send_span()`.
2. **Kiro agent config** (`~/.kiro/agents/<agent>.json`) — the agent the user runs with, containing the tracing `hooks` block.

### Ask the user for:

1. **Backend choice**: Phoenix or Arize AX
2. **Credentials** (only if no existing config):
   - Phoenix: endpoint URL (default: `http://localhost:6006`), optional API key
   - Arize AX: API key and Space ID
3. **OTLP Endpoint** (Arize AX only, optional): For hosted Arize instances using a custom endpoint. Defaults to `otlp.arize.com:443`.
4. **Project name** (optional): defaults to `"kiro"`, stored under `harnesses.kiro.project_name`
5. **User ID** (optional): Set `ARIZE_USER_ID` env var to identify spans by user (useful for teams)
6. **Agent name** (Kiro-specific): defaults to `arize-traced`. The hooks are written into `~/.kiro/agents/<name>.json`. Use an existing agent if the user wants to add tracing to their current workflow without switching agents.
7. **Set as Kiro's default?** (Kiro-specific): If yes, the installer runs `kiro-cli agent set-default <name>` so `kiro-cli chat` (no `--agent` flag) uses the traced agent.

### Write the backend config

The config file at `~/.arize/harness/config.json` is the single source of truth for backend credentials and per-harness settings. Create the directory structure if needed: `mkdir -p ~/.arize/harness/{bin,run,logs,state/kiro}`

**Important: read-merge-write.** If `~/.arize/harness/config.json` already exists, read it first, then merge in the new or updated fields (e.g., add/update the `harnesses.kiro` entry) while preserving existing backend credentials. Only prompt for backend credentials if no existing config is found.

**Phoenix:**
```json
{
  "harnesses": {
    "kiro": {
      "project_name": "kiro",
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
    "kiro": {
      "project_name": "kiro",
      "target": "arize",
      "endpoint": "otlp.arize.com:443",
      "api_key": "<key>",
      "space_id": "<id>"
    }
  }
}
```

If the user has a custom OTLP endpoint, set it in `harnesses.kiro.endpoint`.

### Activate Kiro hooks

Kiro registers hooks per agent. Each agent is a JSON file under `~/.kiro/agents/<name>.json`. The installer either creates the file fresh (default agent: `arize-traced`) or merges hooks into an existing agent the user picks.

A freshly-created `arize-traced` agent looks like:

```json
{
  "name": "arize-traced",
  "description": "Kiro agent with Arize tracing hooks installed.",
  "prompt": null,
  "mcpServers": {},
  "tools": ["*"],
  "toolAliases": {},
  "allowedTools": [],
  "resources": [],
  "hooks": {
    "agentSpawn":       [{ "command": "~/.arize/harness/venv/bin/arize-hook-kiro" }],
    "userPromptSubmit": [{ "command": "~/.arize/harness/venv/bin/arize-hook-kiro" }],
    "preToolUse":       [{ "command": "~/.arize/harness/venv/bin/arize-hook-kiro" }],
    "postToolUse":      [{ "command": "~/.arize/harness/venv/bin/arize-hook-kiro" }],
    "stop":             [{ "command": "~/.arize/harness/venv/bin/arize-hook-kiro" }]
  },
  "toolsSettings": {},
  "includeMcpJson": true,
  "model": null
}
```

All five events route to a single `arize-hook-kiro` CLI entry point that dispatches based on the event name in the payload.

If the user already has an agent JSON they want to trace, merge the five entries above into its existing `hooks` block — do not overwrite the rest of the agent definition.

To install or reinstall via the installer:

```bash
./install.sh kiro
```

To uninstall (removes only the Arize hook entries from each agent file; deletes the agent file only if the installer created it):

```bash
./install.sh uninstall kiro
```

### Set as default agent (optional)

If the user wants `kiro-cli chat` to use the traced agent automatically:

```bash
kiro-cli agent set-default arize-traced
```

Otherwise, they explicitly pass `--agent`:

```bash
kiro-cli chat --agent arize-traced
```

### Validate

1. **Config exists**: Run `cat ~/.arize/harness/config.json` to verify the file contains the `harnesses.kiro` block.
2. **Agent file exists**: Run `cat ~/.kiro/agents/<agent>.json` to verify the `hooks` block has all five events pointing at `arize-hook-kiro`.
3. **Phoenix** (if applicable): Run `curl -sf <endpoint>/v1/traces >/dev/null` to check connectivity.
4. **Kiro accepts the agent config** (optional, requires `kiro-cli` on PATH): `kiro-cli agent validate --path ~/.kiro/agents/<agent>.json`.

### Confirm

Tell the user:
- Backend config saved to `~/.arize/harness/config.json`
- Tracing hooks registered in `~/.kiro/agents/<agent>.json`
- Run a session with `kiro-cli chat` (if you set it as default) or `kiro-cli chat --agent <agent>`
- Spans are sent directly to the backend from hooks — no background process needed
- Traces will appear in their Phoenix UI or Arize AX dashboard under the project name
- Mention `ARIZE_DRY_RUN=true` to test without sending data (set as env var before launching Kiro)
- Mention `ARIZE_VERBOSE=true` for debug output
- Errors are always written to `~/.arize/harness/logs/kiro.log`; set `ARIZE_VERBOSE=true` in the shell before launching Kiro to also capture routine hook activity

## Hook Events

Kiro fires 5 hook events. The first three accumulate state; only `postToolUse` and `stop` emit spans.

| Event | Emits span? | Span name | Kind | Description |
|-------|-------------|-----------|------|-------------|
| `agentSpawn` | No | — | — | Initializes per-session state (trace correlation, tool stack) |
| `userPromptSubmit` | No | — | — | Starts a turn: generates `trace_id` and `span_id`, saves raw prompt |
| `preToolUse` | No | — | — | Pushes tool input + start time to a FIFO stack (Kiro doesn't expose a tool-call id) |
| `postToolUse` | Yes | `Tool: {name}` | TOOL | Pops matching tool slot, builds TOOL span with serialized input/output |
| `stop` | Yes | LLM turn span | LLM | Builds the parent LLM span for the turn, enriched from the session sidecar at `~/.kiro/sessions/cli/<session_id>.json` (model, cost, duration, context usage) |

### Span attributes (LLM span)

| Attribute | Description |
|-----------|-------------|
| `session.id` | Kiro session UUID |
| `openinference.span.kind` | `LLM` |
| `input.value` | User prompt |
| `output.value` | Assistant response |
| `llm.model_name` | Model ID from the session sidecar (e.g. `auto`) |
| `llm.token_count.prompt` / `.completion` / `.total` | Token counts when reported (omitted when 0) |
| `kiro.cost.credits` | Cost in credits from metering data |
| `kiro.metering_usage` | Full metering usage JSON |
| `kiro.turn_duration_ms` | Turn duration in milliseconds |
| `kiro.agent_name` | Name of the Kiro agent (e.g. `arize-traced`) |
| `kiro.context_usage_percentage` | Context window usage percentage |

### Span attributes (TOOL span)

| Attribute | Description |
|-----------|-------------|
| `tool.name` | Tool name (alias form) |
| `tool.description` | Purpose of the tool call (from `__tool_use_purpose` in tool input) |
| `input.value` | Serialized tool input JSON |
| `output.value` | Serialized tool response JSON |

TOOL spans are parented to the LLM turn they belong to via the FIFO tool-state stack.

## Known limitations

- **Token counts are typically 0.** Kiro currently meters in credits, not tokens. Token count attributes are omitted when the value is 0. See `kiro.cost.credits` for usage tracking instead.
- **FIFO tool matching.** Kiro does not expose a tool-call ID, so pre/post tool events are matched using a FIFO stack. This assumes serial tool execution within a session — concurrent tool calls would mismatch.
- **Sidecar read is fail-soft.** The session sidecar at `~/.kiro/sessions/cli/<session_id>.json` may not exist or may lag behind hook events due to a flush race. When this happens, the LLM span is emitted with basic attributes only (no model name, cost, or duration).

## Troubleshoot

Common issues and fixes:

| Problem | Fix |
|---------|-----|
| Traces not appearing | Verify config exists: `cat ~/.arize/harness/config.json`. Check hook log: `tail -20 ~/.arize/harness/logs/kiro.log` |
| Hooks not firing | Verify the agent JSON has all five hooks under `hooks` and that each `command` resolves to the `arize-hook-kiro` venv binary. Run `kiro-cli agent validate --path ~/.kiro/agents/<agent>.json` if `kiro-cli` is on PATH |
| Wrong agent in use | Either pass `--agent <name>` to `kiro-cli chat`, or set the agent as default: `kiro-cli agent set-default <name>` |
| Config missing | Run `./install.sh kiro` or create `~/.arize/harness/config.json` manually with a `harnesses.kiro` section |
| Phoenix unreachable | Verify Phoenix is running: `curl -sf <endpoint>/v1/traces` |
| LLM spans missing model name / cost | The session sidecar at `~/.kiro/sessions/cli/<session_id>.json` was unavailable when `stop` fired. Confirm the sidecar exists for the session — enrichment is fail-soft so the span is emitted without those attributes |
| Tool spans mismatched or orphaned | Concurrent tool execution can break the FIFO match. The handler emits an "orphan" TOOL span when the stack is empty — search the hook log for `no pending tool slot` |
| Want to test without sending | Set `ARIZE_DRY_RUN=true` env var before launching Kiro |
| Want verbose logging | Set `ARIZE_VERBOSE=true` env var before launching Kiro |
| Wrong project name | Set `harnesses.kiro.project_name` in `~/.arize/harness/config.json` (default: `"kiro"`) |
| Spans missing user attribution | Set `ARIZE_USER_ID` env var before launching Kiro |
