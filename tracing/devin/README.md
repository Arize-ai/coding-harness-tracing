# Devin CLI Tracing

Automatic [OpenInference](https://github.com/Arize-ai/openinference) tracing for the Devin CLI. Spans are exported to [Arize AX](https://arize.com) or [Phoenix](https://github.com/Arize-ai/phoenix). Each agent interaction emits its own trace — a root AGENT span, per-generation LLM spans with real token counts, and TOOL spans for tool calls — reconstructed from Devin's live session database.

Devin's hook payloads are thin (no session ID, no token or model data), so the rich data comes from Devin's live SQLite DB at `~/.local/share/devin/cli/sessions.db`. The integration registers a `Stop` hook, which fires at the end of **each** agent response: it resolves the session, reads the generations that have appeared since the last emission, and emits one self-contained trace for that interaction. A `SessionEnd` hook is also registered as a final flush for an interrupted last turn. **Traces therefore appear in Arize AX or Phoenix as each interaction completes — not only after the session exits.** Interactions from the same session are grouped by `session.id`.

## Setup

The installer prompts for your backend (Phoenix or Arize AX) and project name, writes credentials to `~/.arize/harness/config.json`, and registers `Stop` and `SessionEnd` command hooks under the top-level `"hooks"` key in `~/.config/devin/config.json`.

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
| Hook events registered | `Stop`, `SessionEnd` |
| Data source | `~/.local/share/devin/cli/sessions.db` (live, read-only) |
| State directory | `~/.arize/harness/state/devin/` |
| Log file | `~/.arize/harness/logs/devin.log` |

## What's traced

When Devin fires `Stop` (end of an agent response) — or `SessionEnd` — the hook resolves the current session (mapping `DEVIN_PROJECT_DIR` to a `session_id`), reads the session's message nodes from the live DB, and emits one trace per interaction:

- **A root AGENT span** — carries the interaction's user prompt as input, the final assistant text as output, the model name, and the interaction's token totals.
- **Per-generation LLM spans** — one per real LLM generation, with that generation's prompt/completion (and cache) token counts, model name, and reasoning content.
- **TOOL spans** — one per tool call issued by a generation, parented to that LLM span, with the serialized tool arguments as input.

Real generations are deduped by `metadata.request_id`: Devin rebuilds the message chain as the conversation grows (the same generation reappears under new node IDs), and a per-`(session, request_id)` watermark ensures each generation is emitted exactly once. The DB is opened read-only in WAL-respecting mode so the newest turn's rows are visible without disturbing Devin's writers.

Errors always land in `~/.arize/harness/logs/devin.log`; set `export ARIZE_VERBOSE=true` before launching Devin to also see routine hook activity. See the [main README's Environment variables section](../../README.md#environment-variables) for the full list of runtime overrides (`ARIZE_TRACE_ENABLED`, `ARIZE_DRY_RUN`, `ARIZE_USER_ID`, etc.).

## Span shape

### AGENT span (interaction root)

| Attribute | Description |
|-----------|-------------|
| `session.id` | Devin session ID |
| `openinference.span.kind` | `AGENT` |
| `input.value` | User prompt for the interaction |
| `output.value` | Final assistant text for the interaction |
| `llm.model_name` | Model name for the interaction |
| `llm.token_count.prompt` | Interaction prompt tokens (omitted when 0) |
| `llm.token_count.completion` | Interaction completion tokens (omitted when 0) |
| `llm.token_count.total` | Interaction total tokens (omitted when 0) |
| `llm.token_count.prompt_details.cache_read` | Cached prompt tokens read, a subset of prompt (omitted when 0) |
| `llm.token_count.prompt_details.cache_write` | Prompt tokens written to cache, a subset of prompt (omitted when 0) |
| `project.name` | Project name (config/env, else working-dir basename) |
| `user.id` | Optional user identifier |
| `devin.backend` | Agent backend (e.g. `Windsurf`) |

### LLM span (per generation)

| Attribute | Description |
|-----------|-------------|
| `session.id` | Devin session ID |
| `openinference.span.kind` | `LLM` |
| `output.value` | Assistant text for the generation, or its reasoning when the generation produced only thinking + tool calls |
| `llm.output_messages` | Structured assistant response (JSON string) |
| `llm.model_name` | Model name for the generation |
| `llm.reasoning` | Reasoning content (when present) |
| `llm.token_count.prompt` | Per-generation prompt tokens (omitted when 0) |
| `llm.token_count.completion` | Per-generation completion tokens (omitted when 0) |
| `llm.token_count.total` | Per-generation total tokens (omitted when 0) |
| `llm.token_count.prompt_details.cache_read` | Cached prompt tokens read (omitted when 0) |
| `llm.token_count.prompt_details.cache_write` | Prompt tokens written to cache (omitted when 0) |

### TOOL span

| Attribute | Description |
|-----------|-------------|
| `session.id` | Devin session ID |
| `openinference.span.kind` | `TOOL` |
| `tool.name` | Tool name from the tool call |
| `input.value` | Serialized tool-call arguments |
| `output.value` | Tool result, when available in the live DB (often empty — see note) |

TOOL spans are parented to the LLM span that issued the call.

## Notes

- **LLM spans do not carry `input.value`.** The per-generation prompt messages sent to the model are not reconstructed from the DB; the interaction's user prompt lives on the root AGENT span. A generation that issued only tool calls (no assistant text or reasoning) will have an empty `output.value` — its visible output appears on the later generation that answers the user. This is expected: no output is lost, it is attributed to the generation that produced it.
- **TOOL `output.value` is best-effort.** Tool results are not reliably present in the live DB at the moment `Stop` fires, so a TOOL span may carry only its input arguments.

## Uninstall

Uninstall removes the `Stop` and `SessionEnd` hook entries from `~/.config/devin/config.json`, leaving any hooks you added yourself untouched.
