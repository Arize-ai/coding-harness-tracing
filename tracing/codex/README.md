# Codex Tracing

Automatic [OpenInference](https://github.com/Arize-ai/openinference) tracing for OpenAI Codex CLI and desktop. Spans are exported to [Arize AX](https://arize.com) or [Phoenix](https://github.com/Arize-ai/phoenix).

## Setup
The installer prompts for your backend (Phoenix or Arize AX) and project name, writes credentials to `~/.arize/harness/config.json`, and registers an `agent-turn-complete` notification command in `~/.codex/config.toml` (or `$CODEX_HOME/config.toml`). The notification reads the completed turn from Codex's rollout file to export prompt, assistant, token-usage, and tool spans.

An existing notification command is preserved through `--previous-notify` chaining. For a recognized desktop callback, the desktop command stays outermost so Codex can retain its registration. Reinstall is idempotent; uninstall restores the preceding callback. Unsupported desktop chain shapes are rejected before the installer changes the configuration.

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
ARIZE_SOURCE_DIR="$PWD" bash ./install.sh codex
```

Uninstall:

```bash
ARIZE_SOURCE_DIR="$PWD" bash ./install.sh uninstall codex
```

To update from the same checkout:

```bash
ARIZE_SOURCE_DIR="$PWD" bash ./install.sh update
```

`ARIZE_SOURCE_DIR` makes the shell installer build this checkout instead of fetching upstream. Use it when testing a branch or local patch.

**Windows (PowerShell)**

Install:

```powershell
install.bat codex
```

Uninstall:

```powershell
install.bat uninstall codex
```

## Default settings

- Harness key and project name: `codex`.
- Phoenix endpoint: `http://localhost:6006`; Arize AX endpoint: `otlp.arize.com:443`.
- Notification config: `~/.codex/config.toml` (or `$CODEX_HOME/config.toml`).
- Environment overrides: `~/.codex/arize-env.sh`.
- State directory: `~/.arize/harness/state/codex/`.
- Log file: `~/.arize/harness/logs/codex.log`.

User prompts can come from legacy `user_message` events or newer user `response_item` messages. When content provenance is available, only `user.text` content is included, excluding injected repository instructions and environment context. Existing prompt-logging controls still apply.

## Verifying tracing

From the source checkout, check the installed registration without changing it:

```bash
~/.arize/harness/venv/bin/python -I scripts/check-desktop-notify.py
```

The checker reports chain validity and whether the hook executable exists. It does not print API keys or prove trace delivery. `-I` ensures the check imports the installed package instead of the checkout.

Then complete a Codex desktop turn or run a CLI command:

```bash
codex exec "explain what this file does" path/to/file.py
```

Then check:

- Hook activity in `~/.arize/harness/logs/codex.log`.
- Per-thread state files in `~/.arize/harness/state/codex/` show recent activity.
- Spans appear in your configured project in Arize AX or Phoenix.

Errors are always logged. For routine hook activity, add `export ARIZE_VERBOSE=true` to `~/.codex/arize-env.sh` (or your shell) and re-run. See the [main README's Environment variables section](../../README.md#environment-variables) for the full list of runtime overrides (`ARIZE_TRACE_ENABLED`, `ARIZE_DRY_RUN`, `ARIZE_TRACE_DEBUG`, etc.).

## Troubleshooting

**No spans appear.** Check the notification registration with the command above, then inspect `~/.arize/harness/logs/codex.log` for backend/auth errors. Confirm the turn completed and its rollout file is available. Restart Codex after changing environment overrides.

**Unsupported notification chain.** Inspect your existing `notify` command before changing it. The installer refuses unrecognized desktop wrapper arguments rather than replacing them.

**Disable temporarily.** Set `ARIZE_TRACE_ENABLED=false` in `~/.codex/arize-env.sh` and restart Codex. For full uninstall from a checkout, run `ARIZE_SOURCE_DIR="$PWD" bash ./install.sh uninstall codex`.
