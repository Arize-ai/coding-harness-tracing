# GitHub Copilot Tracing

Automatic [OpenInference](https://github.com/Arize-ai/openinference) tracing for GitHub Copilot in VS Code. Spans are exported to [Arize AX](https://arize.com) or [Phoenix](https://github.com/Arize-ai/phoenix).

## Setup
The installer prompts for your backend (Phoenix or Arize AX) and project name, writes credentials to `~/.arize/harness/config.yaml`, and registers Copilot Chat hooks at `.github/hooks/hooks.json`.

Pass `--with-skills` to also symlink the `manage-copilot-tracing` skill into the current directory's `.agents/skills/` so coding agents in this workspace can help manage Copilot tracing configuration.

### Recommended: ax-trace

[`ax-trace`](../../README.md) is a single static binary that bootstraps the Python runtime for you. Install it once, then use it for every harness.

macOS / Linux:

```bash
# One-time: install the ax-trace binary
curl -sSL https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install-ax-trace.sh | bash

# Install Copilot tracing
ax-trace add copilot

# Uninstall
ax-trace uninstall --copilot
```

Windows (PowerShell):

```powershell
# One-time: install the ax-trace binary
irm https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install-ax-trace.ps1 | iex

# Install Copilot tracing
ax-trace add copilot

# Uninstall
ax-trace uninstall --copilot
```

Non-interactive install (CI, scripted setup) — pass credentials via environment variables and skip prompts:

```bash
export ARIZE_API_KEY=...
export ARIZE_SPACE_ID=...
ax-trace add copilot \
  --non-interactive \
  --backend arize \
  --project-name copilot
```

For Phoenix, set `PHOENIX_API_KEY` (optional) and pass `--backend phoenix --phoenix-endpoint http://localhost:6006`.

### Alternative: install.sh / install.bat

The original shell installer still works and targets the same `~/.arize/harness/` layout. Use this if you'd rather not install the `ax-trace` binary.

#### Remote setup

macOS / Linux:

```bash
# Install
curl -sSL https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.sh | bash -s -- copilot

# Uninstall
curl -sSL https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.sh | bash -s -- uninstall copilot
```

Windows (PowerShell):

```powershell
# Install
iwr -useb https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat copilot

# Uninstall
iwr -useb https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat uninstall copilot
```

#### Local setup

```bash
git clone https://github.com/Arize-ai/coding-harness-tracing.git
cd coding-harness-tracing
```

macOS / Linux:

```bash
# Install
./install.sh copilot

# Uninstall
./install.sh uninstall copilot
```

Windows:

```powershell
# Install
install.bat copilot

# Uninstall
install.bat uninstall copilot
```

## Default Settings

| Setting | Default |
|---------|---------|
| Harness key | `copilot` |
| Project name | `copilot` |
| Phoenix endpoint | `http://localhost:6006` |
| Arize AX endpoint | `otlp.arize.com:443` |
| Hook config file | `.github/hooks/hooks.json` |
| Hook events | `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`, `SubagentStop` |
| State directory | `~/.arize/harness/state/copilot/` |
| Log file | `~/.arize/harness/logs/copilot.log` |

## Verifying tracing

Use GitHub Copilot Chat in VS Code (or the Copilot CLI) inside the workspace that contains `.github/hooks/hooks.json`. The hooks fire on `SessionStart`, `UserPromptSubmit`, tool invocations, and `Stop`.

- Errors land in `~/.arize/harness/logs/copilot.log` always; set `export ARIZE_VERBOSE=true` before launching VS Code / Copilot CLI to also see routine hook activity.
- Confirm spans appear in your configured project in Arize AX or Phoenix.
- The hooks file is per-workspace — repeat the install in each repo where you want Copilot tracing.

See the [main README's Environment variables section](../../README.md#environment-variables) for the full list of runtime overrides (`ARIZE_TRACE_ENABLED`, `ARIZE_DRY_RUN`, `ARIZE_USER_ID`, etc.).
