# opencode Tracing

Automatic [OpenInference](https://github.com/Arize-ai/openinference) tracing for [opencode](https://opencode.ai) terminal coding sessions. Spans are exported to [Arize AX](https://arize.com) or [Phoenix](https://github.com/Arize-ai/phoenix).

## What gets traced

One trace per **turn** (one user prompt → the assistant's response → `session.idle`). Each trace is a three-level tree:

| Span | Kind | Notes |
|------|------|-------|
| `Turn` | CHAIN | Root span. `input.value` is the user prompt; `output.value` is the assistant's final text. |
| `LLM: <model>` | LLM | Child of `Turn`. Carries `llm.model_name`, `llm.provider`, prompt/completion/reasoning token counts, cache read/write tokens, and `llm.cost`. One per assistant message. |
| `<tool>` | TOOL | Child of `Turn`. One per completed `ToolPart`. Records `tool.name`, redacted input/output, and tool-specific attributes (`tool.command`, `tool.file_path`, `tool.query`, `tool.url`). |

Timestamps come from opencode's own millisecond clocks (`message.time.created` / `time.completed`, `toolPart.state.time.start` / `.end`) rather than wall-clock time on the tracing process.

## Architecture

opencode is fundamentally different from every other harness in this repo: extensions are [plugins](https://opencode.ai/docs/plugins/) that opencode loads **in-process** inside its Bun runtime — there is no per-event subprocess and no stdin payload. The integration is split into two pieces:

1. **TypeScript plugin shim** (`~/.config/opencode/plugin/arize-tracing.ts`). A dumb bridge. On `message.updated` (assistant completed) and `session.idle` it pulls the authoritative session snapshot via `client.session.messages({ path: { id } })` (see the [opencode SDK docs](https://opencode.ai/docs/sdk/)), then spawns `arize-hook-opencode` detached and pipes the snapshot to stdin. The shim contains no tracing logic.
2. **Python snapshot reconciler** (`arize-hook-opencode`). Reads the snapshot, walks `{info, parts}[]`, and emits any NEW `Turn`/`LLM`/`TOOL` spans deduped by message id and tool `callID`. opencode's `AssistantMessage` already carries final, cumulative `tokens` and `cost`, so no per-delta coalescing is needed.

Snapshots repeat across firings — that's what dedup is for. There is no streaming-chunk forwarding.

## Setup
The installer prompts for your backend (Phoenix or Arize AX) and project name, writes credentials to `~/.arize/harness/config.yaml`, and copies the plugin shim into `~/.config/opencode/plugin/arize-tracing.ts`. opencode auto-discovers plugins in that directory ([config docs](https://opencode.ai/docs/config/)) — no `opencode.json` edit is required. Spans are sent directly to the backend from the reconciler — no separate buffer/collector service is required.

Pass `--with-skills` to also symlink the `manage-opencode-tracing` skill into the current directory's `.agents/skills/` so coding agents in this workspace can help manage opencode tracing configuration.

### Remote setup

macOS / Linux:

```bash
# Install
curl -sSL https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.sh | bash -s -- opencode

# Uninstall
curl -sSL https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.sh | bash -s -- uninstall opencode
```

Windows (PowerShell):

```powershell
# Install
iwr -useb https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat opencode

# Uninstall
iwr -useb https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat uninstall opencode
```

### Local setup

```bash
git clone https://github.com/Arize-ai/coding-harness-tracing.git
cd coding-harness-tracing
```

macOS / Linux:

```bash
# Install
./install.sh opencode

# Uninstall
./install.sh uninstall opencode
```

Windows:

```powershell
# Install
install.bat opencode

# Uninstall
install.bat uninstall opencode
```

Uninstall deletes the plugin file at `~/.config/opencode/plugin/arize-tracing.ts` (only if it carries the Arize header marker — your own plugins are left alone) and removes the `harnesses.opencode` block from `~/.arize/harness/config.yaml`.

## Default Settings

| Setting | Default |
|---------|---------|
| Harness key | `opencode` |
| Project name | `opencode` |
| Phoenix endpoint | `http://localhost:6006` |
| Arize AX endpoint | `otlp.arize.com:443` |
| Plugin file | `~/.config/opencode/plugin/arize-tracing.ts` |
| Lifecycle events forwarded | `message.updated` (assistant completed), `session.idle` |
| Span tree | `Turn` (CHAIN) → `LLM` → `TOOL` |
| Trace granularity | one trace per turn |
| State directory | `~/.arize/harness/state/opencode/` |
| Log file | `~/.arize/harness/logs/opencode.log` |

## Verifying tracing

Run any opencode session as you normally would. opencode loads the plugin on startup; the shim listens for completion events and forwards snapshots to the Python reconciler.

- Errors and reconciler stderr land in `~/.arize/harness/logs/opencode.log` always (the adapter redirects Python stderr there via `ARIZE_LOG_FILE`); set `export ARIZE_VERBOSE=true` before launching opencode to also see routine reconciler activity (snapshot ingest, span emits, dedup hits).
- Confirm spans appear in your configured project in Arize AX or Phoenix.
- Set `ARIZE_TRACE_DEBUG=true` to dump the raw snapshot payloads under `~/.arize/harness/state/debug/` (files are named `opencode_reconcile_<ts>.yaml` / `opencode_close_<ts>.yaml`) for inspection.

See the [main README's Environment variables section](../../README.md#environment-variables) for the full list of runtime overrides (`ARIZE_TRACE_ENABLED`, `ARIZE_DRY_RUN`, `ARIZE_USER_ID`, `ARIZE_PROJECT_NAME`, `ARIZE_VERBOSE`, `ARIZE_TRACE_DEBUG`, etc.).

## Limitations

- **Sub-agent / `task` sessions trace independently.** opencode's built-in `task` tool spawns sub-agents that get their own `sessionID`. In v1 each sub-agent session produces its own independent trace; they are not linked back to the parent session's trace.
