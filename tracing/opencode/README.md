# opencode Tracing

Automatic [OpenInference](https://github.com/Arize-ai/openinference) tracing for [opencode](https://opencode.ai) terminal coding sessions. Spans are exported to [Arize AX](https://arize.com) or [Phoenix](https://github.com/Arize-ai/phoenix).

Supports **OpenCode V1 >=1.3.4 and V2** from the same installed file. V1 before 1.3.4 cannot load a default-object plugin: OpenCode logs "failed to load plugin", tracing is skipped, and startup continues. The shim uses the [documented V1/V2 entrypoint](https://opencode.ai/v2/docs/build/plugins/migrate-v1#support-v1-and-v2-from-one-package): `server` for V1, `id` and `setup` for V2, with no runtime SDK dependency.

## What gets traced

One trace per **turn** (one user prompt → the assistant's response → `session.idle`). Each trace preserves the requesting message and subagent hierarchy:

| Span | Kind | Notes |
|------|------|-------|
| `Turn` | CHAIN | Root span. `input.value` is the user prompt; `output.value` is the assistant's final text. |
| `LLM: <model>` | LLM | Child of `Turn` (or child subagent `AGENT`). Carries `llm.message_id`, model/provider, prompt/completion/reasoning token counts, cache read/write tokens, and cost. One per assistant message. |
| `<tool>` | TOOL | Child of the requesting `LLM`, correlated by `ToolPart.messageID`; falls back to `Turn` only when that relation is unavailable. One per completed `callID`. |
| `Agent: <name>` | AGENT | Child of a `task` (V1) or `subagent` (V2) TOOL for an authoritative child `Session.parentID`; child-session LLM and TOOL spans are nested beneath it. |

Timestamps come from opencode's own millisecond clocks (`message.time.created` / `time.completed`, `toolPart.state.time.start` / `.end`) rather than wall-clock time on the tracing process.

## Architecture

opencode is fundamentally different from every other harness in this repo: extensions are [plugins](https://opencode.ai/docs/plugins/) that opencode loads **in-process** inside its Bun runtime — there is no per-event subprocess and no stdin payload. The integration is split into two pieces:

1. **TypeScript plugin shim** (`~/.config/opencode/plugin/arize-tracing.ts`). A bridge. V1 uses `message.updated` (assistant completed) and `session.idle`, with SDK session snapshots. V2 subscribes to `session.step.ended` / `.failed` and `session.execution.succeeded` / `.failed` / `.interrupted` (except shutdown), using `ctx.session.get` and `ctx.session.context`. V2 messages and tools are normalized to the existing Python snapshot contract. Child tools are followed through `state.metadata.sessionId` (V1) or `.sessionID` (V2); child lifecycle events are suppressed as independent roots and exported with their parent snapshot instead. Unloading the V2 plugin aborts its event subscription and reconnect timer.
2. **Python snapshot reconciler** (`arize-hook-opencode`). Reads the snapshot, reconstructs `Turn → LLM → TOOL → AGENT → child LLM`, and emits only new spans using stable IDs durably reserved under keys derived from message IDs, `callID`, and session IDs. Same-session hook processes are serialized to make dedup safe across concurrent lifecycle events.

Snapshots repeat across firings — that's what dedup is for. There is no streaming-chunk forwarding.

## Setup
The installer prompts for your backend (Phoenix or Arize AX) and project name, writes credentials to `~/.arize/harness/config.json`, and copies the plugin shim into `~/.config/opencode/plugin/arize-tracing.ts`. opencode auto-discovers plugins in that directory ([config docs](https://opencode.ai/docs/config/)) — no `opencode.json` edit is required. Spans are sent directly to the backend from the reconciler — no separate buffer/collector service is required.

Pass `--with-skills` to also symlink the `manage-opencode-tracing` skill into the current directory's `.agents/skills/` so coding agents in this workspace can help manage opencode tracing configuration.

### Remote setup

#### macOS / Linux

Install:

```bash
curl -sSL https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.sh | bash -s -- opencode
```

Uninstall:

```bash
curl -sSL https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.sh | bash -s -- uninstall opencode
```

#### Windows (PowerShell)

Install:

```powershell
iwr -useb https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat opencode
```

Uninstall:

```powershell
iwr -useb https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat uninstall opencode
```

### Local setup

```bash
git clone https://github.com/Arize-ai/coding-harness-tracing.git
cd coding-harness-tracing
```

**macOS / Linux**

Install:

```bash
./install.sh opencode
```

Uninstall:

```bash
./install.sh uninstall opencode
```

**Windows (PowerShell)**

Install:

```powershell
install.bat opencode
```

Uninstall:

```powershell
install.bat uninstall opencode
```

Uninstall deletes the plugin file at `~/.config/opencode/plugin/arize-tracing.ts` (only if it carries the Arize header marker — your own plugins are left alone) and removes the `harnesses.opencode` block from `~/.arize/harness/config.json`.

## Default Settings

| Setting | Default |
|---------|---------|
| Harness key | `opencode` |
| Project name | `opencode` |
| Phoenix endpoint | `http://localhost:6006` |
| Arize AX endpoint | `otlp.arize.com:443` |
| Plugin file | `~/.config/opencode/plugin/arize-tracing.ts` |
| Lifecycle events forwarded | V1: assistant `message.updated`, `session.idle`; V2: step ended/failed, execution succeeded/failed/interrupted (not shutdown) |
| Span tree | `Turn` (CHAIN) → `LLM` → `TOOL`; task/subagent tools may contain `AGENT` → child `LLM`/`TOOL` |
| Trace granularity | one trace per turn |
| State directory | `~/.arize/harness/state/opencode/` |
| Log file | `~/.arize/harness/logs/opencode.log` |

## Verifying tracing

Run any opencode session as you normally would. opencode loads the plugin on startup; the shim listens for completion events and forwards snapshots to the Python reconciler.

- Errors and reconciler stderr land in `~/.arize/harness/logs/opencode.log` always (the adapter redirects Python stderr there via `ARIZE_LOG_FILE`); set `export ARIZE_VERBOSE=true` before launching opencode to also see routine reconciler activity (snapshot ingest, span emits, dedup hits).
- Confirm spans appear in your configured project in Arize AX or Phoenix.
- On V2, run `opencode api get /api/plugin` and confirm `arize-tracing` is loaded. After updating the shim, use `opencode service restart` to reload the shared service.
- Set `ARIZE_TRACE_DEBUG=true` to dump the raw snapshot payloads under `~/.arize/harness/state/debug/` (files are named `opencode_reconcile_<ts>.json` / `opencode_close_<ts>.json`) for inspection.

See the [main README's Environment variables section](../../README.md#environment-variables) for the full list of runtime overrides (`ARIZE_TRACE_ENABLED`, `ARIZE_DRY_RUN`, `ARIZE_USER_ID`, `ARIZE_PROJECT_NAME`, `ARIZE_VERBOSE`, `ARIZE_TRACE_DEBUG`, etc.).

## Limitations

- Child-session linking requires authoritative tool metadata (`sessionId` or `sessionID`), the tool call ID, and `Session.parentID`. When a relation is absent, the reconciler does not invent a subagent edge.
- V2's supported plugin context API returns only messages after the last compaction. Already reconciled spans remain, but loading tracing after compaction cannot backfill earlier history. Compaction model calls are not traced by this shim.
- Background child sessions are captured in the next parent snapshot. Children still running when the parent closes may be incomplete; child events are not exported as independent traces.
- A disconnected event stream reconnects after five seconds. Missed events are reconciled only when a later lifecycle event produces another snapshot.
