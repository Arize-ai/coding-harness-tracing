---
name: manage-cursor-tracing
description: Set up and manage Arize tracing for Cursor IDE / CLI sessions using the ax-trace CLI. Use when users want to set up Cursor tracing, configure Arize AX or Phoenix for Cursor, edit config, run diagnostics, enable/disable tracing, or troubleshoot Cursor tracing. Triggers on "set up cursor tracing", "configure Arize for Cursor", "ax-trace", "enable cursor tracing", "setup-cursor-tracing", or any request about connecting Cursor to Arize or Phoenix for observability.
---

# Manage Cursor Tracing

Configure OpenInference tracing for Cursor (IDE and CLI) to Arize AX (cloud) or Phoenix (self-hosted). Spans are sent directly to the backend from hooks — no background process or backend-specific Python deps run in the user's environment.

The primary tool is the **`ax-trace`** CLI. It installs a managed Python runtime (via [uv](https://github.com/astral-sh/uv)), registers the Cursor hooks, and manages config. Reach for the repo only to inspect hook/handler internals: <https://github.com/Arize-ai/coding-harness-tracing> (Cursor code under `tracing/cursor/`).

## How to use this skill

1. **Installing / adding tracing?** → [Install](#install)
2. **Have credentials, changing a setting?** → [Configure via the CLI](#configure-via-the-cli)
3. **Not working / debugging?** → [Diagnose with doctor](#diagnose-with-doctor) then [Troubleshoot](#troubleshoot)
4. **No backend account yet?** → [Backends](#backends) first

## Install

```bash
go install github.com/Arize-ai/coding-harness-tracing/cmd/ax-trace@latest
ax-trace add cursor
```

`ax-trace add cursor` bootstraps the runtime, registers hooks in `~/.cursor/hooks.json`, and runs the wizard. Fields collected:

| Field | Notes |
|-------|-------|
| Backend | `arize` or `phoenix` |
| API key | `ARIZE_API_KEY` / `PHOENIX_API_KEY` — env var or masked prompt, never a flag |
| Space ID | Arize only |
| OTLP / Phoenix endpoint | Arize defaults to `otlp.arize.com:443`; Phoenix to `http://localhost:6006` |
| Project name | Defaults to `cursor` |
| User ID | Optional; added to every span as `user.id` |
| Content logging | Three prompts (prompts / tool details / tool content), default **on** |
| Verbose | Terminal trace summaries, default **off** |

A single `arize-hook-cursor` entry point handles all Cursor events (IDE + CLI), dispatching on the event name in the payload. Restart Cursor after install.

**Non-interactive:**

```bash
export ARIZE_API_KEY=...
ax-trace add cursor --backend arize --space-id SPACE_ID --project-name cursor --non-interactive
```

## Backends

### Arize AX (cloud)

SaaS uses `otlp.arize.com:443`; on-prem needs a custom OTLP endpoint. Get credentials: log in (https://app.arize.com), **Settings** → **Space ID** on Space Settings; **API Keys** tab to create/copy a key. Both `api_key` and `space_id` required.

### Phoenix (self-hosted)

```bash
pip install arize-phoenix && phoenix serve   # or: docker run -p 6006:6006 arizephoenix/phoenix:latest
```

UI at `http://localhost:6006`. Verify: `curl -sf http://localhost:6006/v1/traces >/dev/null && echo ok`.

## Configure via the CLI

Backend credentials live in `~/.arize/harness/config.yaml`. The hook wiring lives in `~/.cursor/hooks.json` (written by `ax-trace add cursor`). Edit backend settings with `ax-trace config`:

```bash
ax-trace config show                              # api_key masked
ax-trace config set harnesses.cursor.project_name cursor
ax-trace config set verbose true
ax-trace config edit
```

Schema:

```yaml
harnesses:
  cursor:
    project_name: cursor
    target: arize                   # arize | phoenix
    endpoint: otlp.arize.com:443    # OTLP (arize) or Phoenix URL
    api_key: <key>
    space_id: <id>                  # arize only
logging:
  prompts: true
  tool_details: true
  tool_content: true
user_id: ""
verbose: false                      # ARIZE_VERBOSE env wins over this
```

## Diagnose with doctor

```bash
ax-trace doctor
```

If the user already has a `.cursor/hooks.json` with other hooks, merge the Arize entries into the existing arrays for each event.

### Validate

1. **Config exists**: Run `cat ~/.arize/harness/config.yaml` to verify the config file exists and has correct backend credentials.
2. **Phoenix** (if applicable): Run `curl -sf <endpoint>/v1/traces >/dev/null` to check connectivity.
3. **Hooks active**: Verify `.cursor/hooks.json` exists in the project root and contains the Arize hook entries.

### Confirm

Tell the user:
- Config saved to `~/.arize/harness/config.yaml`
- Cursor hooks activated via `.cursor/hooks.json`
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

When configuring `.cursor/hooks.json`, include both IDE and CLI events:

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

Removes the Arize hook entries from `~/.cursor/hooks.json` and the `harnesses.cursor` config entry.

## Troubleshoot

Run `ax-trace doctor` first. Then:

| Problem | Fix |
|---------|-----|
| No traces | Verify `~/.cursor/hooks.json` has `arize-hook-cursor` entries; restart Cursor |
| Phoenix unreachable | `curl -sf <endpoint>/v1/traces` |
| Test without sending | `ARIZE_DRY_RUN=true` before launching Cursor |
| Verbose logging | `ax-trace config set verbose true` (or `ARIZE_VERBOSE=true`); errors always go to `~/.arize/harness/logs/cursor.log` |
| Wrong project name | `ax-trace config set harnesses.cursor.project_name <name>` |
| Spans missing user attribution | `ax-trace config set user_id <id>` (or `ARIZE_USER_ID` env) |
