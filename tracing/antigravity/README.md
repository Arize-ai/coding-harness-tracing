# Antigravity CLI/IDE Tracing

Automatic [OpenInference](https://github.com/Arize-ai/openinference) tracing for Google Antigravity CLI/IDE sessions. Spans are exported to [Arize AX](https://arize.com) or [Phoenix](https://github.com/Arize-ai/phoenix).

This harness is **transcript-driven**: Antigravity hooks are control-plane triggers that carry only pointers (`transcriptPath`, `conversationId`, `workspacePaths`). The real model and tool content lives in the per-turn `transcript_full.jsonl` written by the agent. The `Stop` hook parses that transcript and reconstructs spans — one trace per user turn.

## Setup

The installer prompts for your backend (Phoenix or Arize AX) and project name, writes credentials to `~/.arize/harness/config.json`, and registers hooks under the top-level `arize-tracing` key in `~/.gemini/config/hooks.json` (Antigravity's global hooks file — distinct from Gemini's `~/.gemini/settings.json`).

Pass `--with-skills` to also symlink the `manage-antigravity-tracing` skill into the current directory's `.agents/skills/` so coding agents in this workspace can help manage Antigravity tracing configuration.

### Remote setup

#### macOS / Linux

Install:

```bash
curl -sSL https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.sh | bash -s -- antigravity
```

Uninstall:

```bash
curl -sSL https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.sh | bash -s -- uninstall antigravity
```

#### Windows (PowerShell)

Install:

```powershell
iwr -useb https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat antigravity
```

Uninstall:

```powershell
iwr -useb https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat uninstall antigravity
```

### Local setup

```bash
git clone https://github.com/Arize-ai/coding-harness-tracing.git
cd coding-harness-tracing
```

**macOS / Linux**

Install:

```bash
./install.sh antigravity
```

Uninstall:

```bash
./install.sh uninstall antigravity
```

**Windows (PowerShell)**

Install:

```powershell
install.bat antigravity
```

Uninstall:

```powershell
install.bat uninstall antigravity
```

## Default Settings

| Setting | Default |
|---------|---------|
| Harness key | `antigravity` |
| Project name | `antigravity` |
| Phoenix endpoint | `http://localhost:6006` |
| Arize AX endpoint | `otlp.arize.com:443` |
| Hook config file | `~/.gemini/config/hooks.json` |
| Hook events registered | `PreInvocation`, `Stop` |
| State directory | `~/.arize/harness/state/antigravity/` |
| Log file | `~/.arize/harness/logs/antigravity.log` |

## Verifying tracing

Run any Antigravity CLI/IDE session as you normally would. The installed hooks fire on `PreInvocation` (before each model invocation) and `Stop` (after the user turn completes).

- Errors land in `~/.arize/harness/logs/antigravity.log` always; set `export ARIZE_VERBOSE=true` before launching Antigravity to also see routine hook activity.
- Confirm spans appear in your configured project in Arize AX or Phoenix.
- Each hook has a 30-second timeout — see `HOOK_TIMEOUT_SECONDS` in `constants.py` if you need to adjust.

See the [main README's Environment variables section](../../README.md#environment-variables) for the full list of runtime overrides (`ARIZE_TRACE_ENABLED`, `ARIZE_DRY_RUN`, `ARIZE_USER_ID`, etc.).

## Limitations

- **Token counts are not captured.** Antigravity does not expose per-turn token usage on any local surface (neither the hook payload nor the transcript). `llm.token_count.*` attributes are intentionally absent on Antigravity spans rather than reported as 0.
