# Codex CLI Tracing

Automatic [OpenInference](https://github.com/Arize-ai/openinference) tracing for the OpenAI Codex CLI. Spans are exported to [Arize AX](https://arize.com) or [Phoenix](https://github.com/Arize-ai/phoenix).

## Setup
The installer prompts for your backend (Phoenix or Arize AX) and project name, writes credentials to `~/.arize/harness/config.json`, and registers the Codex `notify` handler in `~/.codex/config.toml`. Codex invokes that handler after each completed agent turn; no separate `/hooks` approval is required. The `notify` array is one program argv, not a callback list: if another notify program already owns it, installation stops with a conflict instead of appending an invalid second command. Remove the existing integration or use a dispatcher program that invokes both.

Pass `--with-skills` to also symlink the `manage-codex-tracing` skill into the current directory's `.agents/skills/` so coding agents in this workspace can help manage Codex tracing configuration.

### Remote setup

#### macOS / Linux

Install:

```bash
curl -sSL https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.sh | bash -s -- codex
```

Uninstall:

```bash
curl -sSL https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.sh | bash -s -- uninstall codex
```

#### Windows (PowerShell)

Install:

```powershell
iwr -useb https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat codex
```

Uninstall:

```powershell
iwr -useb https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat uninstall codex
```

### Local setup

```bash
git clone https://github.com/Arize-ai/coding-harness-tracing.git
cd coding-harness-tracing
```

**macOS / Linux**

Install:

```bash
./install.sh codex
```

Uninstall:

```bash
./install.sh uninstall codex
```

**Windows (PowerShell)**

Install:

```powershell
install.bat codex
```

Uninstall:

```powershell
install.bat uninstall codex
```

## Default Settings

| Setting | Default |
|---------|---------|
| Harness key | `codex` |
| Project name | `codex` |
| Phoenix endpoint | `http://localhost:6006` |
| Arize AX endpoint | `otlp.arize.com:443` |
| Notify config file | `~/.codex/config.toml` |
| Notify event handled | `agent-turn-complete` |
| Rollout source | `~/.codex/sessions/**/rollout-*-<thread_id>.jsonl` |
| Env override file | `~/.codex/arize-env.sh` |
| Log file | `~/.arize/harness/logs/codex.log` |

## Trace topology

For current Codex rollouts, one completed turn is exported as a high-fidelity tree:

```text
CHAIN (turn)
├── LLM (model response)
│   └── TOOL (function, shell, web, custom, tool search, or image generation)
└── LLM (next model response)
```

Each LLM span has its own input, assistant output, timestamps, and token usage. A tool output is correlated by `call_id` and becomes input to the next model response. `llm.token_count.prompt` includes cached input, while `llm.token_count.prompt_details.cache_read` reports the cached subset; do not add them together. When Codex reports reasoning usage, it is exposed separately as `llm.token_count.completion_details.reasoning` and remains part of the completion total.

### Compatibility and incomplete rollouts

The notify payload and extracted turn retain aggregate `token_usage` and flat `tool_calls` for compatibility. If response-cycle records are unavailable, tracing keeps the previous `LLM → TOOL` representation. If no matching rollout can be found—or a partial rollout contains only lifecycle metadata—the notify payload still produces one fallback LLM span.

Parsing is fail-soft: malformed or unknown JSONL records are skipped, missing `token_count` does not discard an otherwise visible response, and unmatched tool outputs do not prevent the basic turn trace from being exported.

## Privacy

Prompt and assistant content is controlled by `ARIZE_LOG_PROMPTS`. Tool arguments and tool output are controlled by `ARIZE_LOG_TOOL_DETAILS` and `ARIZE_LOG_TOOL_CONTENT`. Reconstructed LLM inputs preserve the provenance of every prompt-derived and tool-output-derived fragment, so these controls remain independent even when both kinds of content appear in one model input. When disabled, values are replaced with length-only placeholders in every CHAIN, LLM, and TOOL span.

Every exported textual content attribute is capped at 64 KiB and receives an explicit truncation marker when necessary. Image-generation results are always replaced with a length-only placeholder to avoid exporting inline binary payloads.

Rollout lookup treats the notify thread ID as a literal identifier, verifies matching `session_meta` when present, and rejects wildcard, path-like, and symlink-escape candidates. This prevents one session notification from selecting another session's rollout.

Operational metadata needed to correlate and diagnose traces remains exported independently of those content flags: thread/turn IDs, model, working directory/workspace, approval and sandbox modes, timestamps, and token counts. Keep project paths and identifiers non-sensitive if that metadata must not leave the machine.

## Verifying tracing

Run any Codex command:

```bash
codex exec "explain what this file does" path/to/file.py
```

Then check:

- Hook activity in `~/.arize/harness/logs/codex.log`.
- Spans appear in your configured project in Arize AX or Phoenix.

Errors are always logged. For routine hook activity, add `export ARIZE_VERBOSE=true` to `~/.codex/arize-env.sh` (or your shell) and re-run. See the [main README's Environment variables section](../../README.md#environment-variables) for the full list of runtime overrides (`ARIZE_TRACE_ENABLED`, `ARIZE_DRY_RUN`, `ARIZE_TRACE_DEBUG`, etc.).

## Troubleshooting

**Notify not firing.** Check that `notify` in `~/.codex/config.toml` points to the installed Codex handler and that `ARIZE_TRACE_ENABLED` is not set to `false`.

**No spans appear.** Re-source your shell profile (or open a new terminal) so `~/.codex/arize-env.sh` is loaded. Check `~/.arize/harness/logs/codex.log` for rollout lookup and backend/auth errors. Confirm that the matching session rollout exists under `~/.codex/sessions/`.

**Disable temporarily.** Set `ARIZE_TRACE_ENABLED=false` in `~/.codex/arize-env.sh` and restart Codex. Full uninstall: `./install.sh uninstall codex`.
