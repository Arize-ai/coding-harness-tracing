# Gemini CLI Tracing

Automatic [OpenInference](https://github.com/Arize-ai/openinference) tracing for Gemini CLI sessions. Spans are exported to [Arize AX](https://arize.com) or [Phoenix](https://github.com/Arize-ai/phoenix).

## Setup
The installer prompts for your backend (Phoenix or Arize AX) and project name, writes credentials to `~/.arize/harness/config.json`, and registers the hooks in `~/.gemini/settings.json`.

Pass `--with-skills` to also symlink the `manage-gemini-tracing` skill into the current directory's `.agents/skills/` so coding agents in this workspace can help manage Gemini tracing configuration.

### Remote setup

#### macOS / Linux

Install:

```bash
curl -sSL https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.sh | bash -s -- gemini
```

Uninstall:

```bash
curl -sSL https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.sh | bash -s -- uninstall gemini
```

#### Windows (PowerShell)

Install:

```powershell
iwr -useb https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat gemini
```

Uninstall:

```powershell
iwr -useb https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat uninstall gemini
```

### Local setup

```bash
git clone https://github.com/Arize-ai/coding-harness-tracing.git
cd coding-harness-tracing
```

**macOS / Linux**

Install:

```bash
./install.sh gemini
```

Uninstall:

```bash
./install.sh uninstall gemini
```

**Windows (PowerShell)**

Install:

```powershell
install.bat gemini
```

Uninstall:

```powershell
install.bat uninstall gemini
```

## Default Settings

| Setting | Default |
|---------|---------|
| Harness key | `gemini` |
| Project name | `gemini` |
| Phoenix endpoint | `http://localhost:6006` |
| Arize AX endpoint | `otlp.arize.com:443` |
| Hook config file | `~/.gemini/settings.json` |
| Hook events registered | `SessionStart`, `SessionEnd`, `BeforeAgent`, `AfterAgent`, `BeforeModel`, `AfterModel`, `BeforeTool`, `AfterTool` |
| State directory | `~/.arize/harness/state/gemini/` |
| Log file | `~/.arize/harness/logs/gemini.log` |

## Verifying tracing

Run any Gemini CLI session as you normally would (e.g. `gemini` or `gemini -p "hello"`). The installed hooks fire on `SessionStart`, model and tool boundaries, and `SessionEnd`.

- Errors land in `~/.arize/harness/logs/gemini.log` always; set `export ARIZE_VERBOSE=true` before launching Gemini to also see routine hook activity.
- Confirm spans appear in your configured project in Arize AX or Phoenix.
- Each hook has a 30-second timeout (Gemini's default is 60s) — see `HOOK_TIMEOUT_MS` in `constants.py` if you need to adjust.

See the [main README's Environment variables section](../../README.md#environment-variables) for the full list of runtime overrides (`ARIZE_TRACE_ENABLED`, `ARIZE_DRY_RUN`, `ARIZE_USER_ID`, etc.).
