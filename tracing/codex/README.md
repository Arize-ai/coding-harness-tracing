# Codex CLI Tracing

Automatic [OpenInference](https://github.com/Arize-ai/openinference) tracing for the OpenAI Codex CLI. Spans are exported to [Arize AX](https://arize.com) or [Phoenix](https://github.com/Arize-ai/phoenix).

## Setup
The installer prompts for your backend (Phoenix or Arize AX) and project name, writes credentials to `~/.arize/harness/config.json`, and sets `notify` in `~/.codex/config.toml` to the `arize-hook-codex-notify` entry point. There are no Codex hooks to approve.

When a turn completes, Codex runs the notify hook. The hook reads that turn from the session transcript Codex saves under `~/.codex/sessions/` and sends one LLM span for the turn, with a TOOL span for each tool call. This covers the Codex CLI and the [Codex SDK](https://developers.openai.com/codex/sdk), which runs the same Codex runtime.

Codex runs a single `notify` command: `notify[0]` is the program, the rest of the array is its arguments, and Codex appends the event JSON as the last argument. If you already had a `notify` command, the installer saves it as `harnesses.codex.previous_notify` in `~/.arize/harness/config.json`, the hook passes every event on to it, and uninstall puts it back. A saved command that calls the hook itself, such as a wrapper you wrote to run both, is fine: the hook ignores calls that come from its own forwarding.

Pass `--with-skills` to also copy the `manage-codex-tracing` skill into the current directory's `.agents/skills/` so coding agents in this workspace can help manage Codex tracing configuration.

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
| Hook config file | `~/.codex/config.toml` (`notify`) |
| Events handled | `agent-turn-complete` (via `notify`); turn details come from the session transcript in `~/.codex/sessions/` |
| Env override file | `~/.codex/arize-env.sh` |
| Log file | `~/.arize/harness/logs/codex.log` |

## Verifying tracing

Run any Codex command:

```bash
codex exec "explain what this file does" path/to/file.py
```

Then check that spans appear in your configured project in Arize AX or Phoenix.

Only errors are logged by default, so an empty `~/.arize/harness/logs/codex.log` doesn't mean the hook didn't run. To log routine hook activity (a `Turn sent` line per turn), add `export ARIZE_VERBOSE=true` to `~/.codex/arize-env.sh` (or your shell) and re-run. See the [main README's Environment variables section](../../README.md#environment-variables) for the full list of runtime overrides (`ARIZE_TRACE_ENABLED`, `ARIZE_DRY_RUN`, `ARIZE_TRACE_DEBUG`, etc.).

## Troubleshooting

**Hook not firing.** Check that `notify` in `~/.codex/config.toml` is exactly `['<path>/arize-hook-codex-notify']` and that the path exists. Codex runs only the first element of `notify`, so if another program comes first, the hook never runs. Re-run the installer to fix it.

**No spans appear.** Re-source your shell profile (or open a new terminal) so `~/.codex/arize-env.sh` is loaded. Check `~/.arize/harness/logs/codex.log` for backend/auth errors.

**Spans land in the wrong project when Codex runs inside another tool.** The hook inherits the environment of the process that started Codex. If that process is a traced session of another harness (for example, a Codex SDK script run from Claude Code), it can pass down that harness's `ARIZE_PROJECT_NAME`, which overrides the Codex project. Unset it before starting Codex. An inherited `ARIZE_TRACE_ENABLED` doesn't matter: the hook loads `~/.codex/arize-env.sh`, which sets it again.

**Disable temporarily.** Set `ARIZE_TRACE_ENABLED=false` in `~/.codex/arize-env.sh` and restart Codex. A saved `previous_notify` command keeps receiving events. Full uninstall: `./install.sh uninstall codex`.
