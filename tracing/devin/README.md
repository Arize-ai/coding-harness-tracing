# Devin CLI Tracing

Automatic [OpenInference](https://github.com/Arize-ai/openinference) tracing for the Devin CLI. Spans are exported to [Arize AX](https://arize.com) or [Phoenix](https://github.com/Arize-ai/phoenix). Each traced session emits a session-level AGENT span, per-step LLM spans with real token counts, and TOOL spans for tool calls — all reconstructed from Devin's local ATIF transcript.

Because Devin's hook payloads are thin (no session ID, no token or model data), spans are emitted in one shot when the session ends: the integration registers a single `SessionEnd` hook, resolves the session's transcript, parses it, and emits the full span tree. Tokens only exist at session end and OTLP spans are immutable once exported, so deferred emission is correct. **Spans therefore appear in Arize AX or Phoenix only after the Devin session exits.**

## Setup

The installer prompts for your backend (Phoenix or Arize AX) and project name, writes credentials to `~/.arize/harness/config.json`, and registers a `SessionEnd` command hook under the top-level `"hooks"` key in `~/.config/devin/config.json`.

### Remote setup

#### macOS / Linux

Install:

```bash
curl -sSL https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.sh | bash -s -- devin
```

Uninstall:

```bash
curl -sSL https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.sh | bash -s -- uninstall devin
```

#### Windows (PowerShell)

Install:

```powershell
iwr -useb https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat devin
```

Uninstall:

```powershell
iwr -useb https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat uninstall devin
```

### Local setup

```bash
git clone https://github.com/Arize-ai/coding-harness-tracing.git
cd coding-harness-tracing
```

**macOS / Linux**

Install:

```bash
./install.sh devin
```

Uninstall:

```bash
./install.sh uninstall devin
```

**Windows (PowerShell)**

Install:

```powershell
install.bat devin
```

Uninstall:

```powershell
install.bat uninstall devin
```

## Default Settings

| Setting | Default |
|---------|---------|
| Harness key | `devin` |
| Project name | `devin` |
| Phoenix endpoint | `http://localhost:6006` |
| Arize AX endpoint | `otlp.arize.com:443` |
| Hook config file | `~/.config/devin/config.json` |
| Hook events registered | `SessionEnd` |
| Transcript source | `~/.local/share/devin/cli/transcripts/<session_id>.json` (ATIF-v1.7) |
| State directory | `~/.arize/harness/state/devin/` |
| Log file | `~/.arize/harness/logs/devin.log` |

## What's traced

When a Devin session ends, the `SessionEnd` hook resolves the current session (mapping `DEVIN_PROJECT_DIR` to a `session_id`), reads the schema-versioned ATIF transcript, and emits:

- **A session-level AGENT span** — the root of the trace, carrying the model name, session-level token totals, and the combined user prompt / final assistant output.
- **Per-step LLM spans** — one for each `agent` step in the transcript, with that step's prompt/completion token counts, model name, and reasoning content.
- **TOOL spans** — one per tool call issued by a step, parented to the LLM step that issued it, with the tool input and its matching observation as output.

Errors always land in `~/.arize/harness/logs/devin.log`; set `export ARIZE_VERBOSE=true` before launching Devin to also see routine hook activity. See the [main README's Environment variables section](../../README.md#environment-variables) for the full list of runtime overrides (`ARIZE_TRACE_ENABLED`, `ARIZE_DRY_RUN`, `ARIZE_USER_ID`, etc.).

## Span shape

### AGENT span (session root)

| Attribute | Description |
|-----------|-------------|
| `session.id` | Devin session ID from the transcript |
| `openinference.span.kind` | `AGENT` |
| `input.value` | Combined user prompts for the session |
| `output.value` | Final assistant output |
| `llm.model_name` | Model name reported by the agent |
| `llm.token_count.prompt` | Session-level prompt tokens (omitted when 0) |
| `llm.token_count.completion` | Session-level completion tokens (omitted when 0) |
| `llm.token_count.total` | Session-level total tokens (omitted when 0) |
| `llm.token_count.prompt_details.cache_read` | Cached prompt tokens, a subset of prompt (omitted when 0) |
| `project.name` | Project name (config/env, else working-dir basename) |
| `user.id` | Optional user identifier |
| `devin.backend` | Agent backend (e.g. `Windsurf`) |
| `devin.agent_version` | Agent version string |

### LLM span (per agent step)

| Attribute | Description |
|-----------|-------------|
| `session.id` | Devin session ID |
| `openinference.span.kind` | `LLM` |
| `output.value` | Assistant message for the step |
| `llm.output_messages` | Structured assistant response (JSON string) |
| `llm.model_name` | Model name for the step |
| `llm.reasoning` | Step reasoning content (when present) |
| `llm.token_count.prompt` | Per-step prompt tokens (omitted when 0) |
| `llm.token_count.completion` | Per-step completion tokens (omitted when 0) |
| `llm.token_count.total` | Per-step total tokens (omitted when 0) |

### TOOL span

| Attribute | Description |
|-----------|-------------|
| `session.id` | Devin session ID |
| `openinference.span.kind` | `TOOL` |
| `tool.name` | Tool name from the tool call |
| `input.value` | Serialized tool-call arguments |
| `output.value` | Matching observation result for the call |

TOOL spans are parented to the LLM step that issued the call.

## Uninstall

Uninstall removes the `SessionEnd` hook entry from `~/.config/devin/config.json`, leaving any hooks you added yourself untouched.
