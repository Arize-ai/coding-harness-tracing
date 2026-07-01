# Claude Code Tracing

Automatic [OpenInference](https://github.com/Arize-ai/openinference) tracing for the Claude Code CLI and the Claude Agent SDK. Spans are exported to [Arize AX](https://arize.com) or [Phoenix](https://github.com/Arize-ai/phoenix).

## Setup
The installer prompts for your backend (Phoenix or Arize AX) and project name, writes credentials to `~/.arize/harness/config.json`, and registers the hooks in `~/.claude/settings.json`.

Pass `--with-skills` to also symlink the `manage-claude-code-tracing` skill into the current directory's `.agents/skills/` so Claude can help you manage the configuration interactively.

### Claude Code marketplace

The marketplace flow registers the hooks but skips the interactive wizard, so backend credentials and content-logging preferences must be set directly in `~/.claude/settings.json` under `env`:

```json
{
  "env": {
    "ARIZE_PROJECT_NAME": "claude-code",
    "ARIZE_API_KEY": "<your-arize-api-key>",
    "ARIZE_SPACE_ID": "<your-arize-space-id>",
    "ARIZE_LOG_PROMPTS": "true",
    "ARIZE_LOG_TOOL_DETAILS": "true",
    "ARIZE_LOG_TOOL_CONTENT": "true"
  }
}
```

For Phoenix, swap the Arize keys for `PHOENIX_ENDPOINT` (and optional `PHOENIX_API_KEY`). Each `ARIZE_LOG_*` flag accepts `"true"` or `"false"` — set to `"false"` to opt out per category. Env values take precedence over `~/.arize/harness/config.json`.

Install:

```bash
claude plugin marketplace add Arize-ai/coding-harness-tracing
claude plugin install claude-code-tracing@coding-harness-tracing
```

Uninstall:

```bash
claude plugin uninstall claude-code-tracing@coding-harness-tracing
claude plugin marketplace remove Arize-ai/coding-harness-tracing
```

### Remote setup

#### macOS / Linux

Install:

```bash
curl -sSL https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.sh | bash -s -- claude
```

Uninstall:

```bash
curl -sSL https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.sh | bash -s -- uninstall claude
```

#### Windows (PowerShell)

Install:

```powershell
iwr -useb https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat claude
```

Uninstall:

```powershell
iwr -useb https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat uninstall claude
```

### Local setup

```bash
git clone https://github.com/Arize-ai/coding-harness-tracing.git
cd coding-harness-tracing
```

**macOS / Linux**

Install:

```bash
./install.sh claude
```

Uninstall:

```bash
./install.sh uninstall claude
```

**Windows (PowerShell)**

Install:

```powershell
install.bat claude
```

Uninstall:

```powershell
install.bat uninstall claude
```

## Default Settings

| Setting | Default |
|---------|---------|
| Harness key | `claude-code` |
| Project name | `claude-code` |
| Phoenix endpoint | `http://localhost:6006` |
| Arize AX endpoint | `otlp.arize.com:443` |
| Hook config file | `~/.claude/settings.json` |
| Hook events registered | `SessionStart`, `SessionEnd`, `UserPromptSubmit`, `UserPromptExpansion`, `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `Stop`, `StopFailure`, `SubagentStart`, `SubagentStop`, `Notification`, `PermissionRequest`, `PermissionDenied`, `PreCompact`, `PostCompact` |
| State directory | `~/.arize/harness/state/claude-code/` |
| Log file | `~/.arize/harness/logs/claude-code.log` |

## Verifying tracing

Run any Claude Code session as you normally would (e.g. `claude` or `claude -p "hello"`). The installed hooks fire on every `SessionStart`, `UserPromptSubmit`, `PreToolUse`, etc.

- Errors and `ARIZE_VERBOSE=true` activity land in `~/.arize/harness/logs/claude-code.log`. To see routine hook activity (`session_start fired`, `emitted LLM span`, etc.), add `"ARIZE_VERBOSE": "true"` under `env` in `~/.claude/settings.json` and re-run a session.
- Confirm spans appear in your configured project in Arize AX or Phoenix.
- Set `"ARIZE_TRACE_ENABLED": "false"` under `env` to temporarily disable tracing without uninstalling, or `"ARIZE_DRY_RUN": "true"` to build spans without sending them.

See the [main README's Environment variables section](../../README.md#environment-variables) for the full list of runtime overrides.
