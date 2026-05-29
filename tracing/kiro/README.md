# Kiro CLI Tracing

Automatic [OpenInference](https://github.com/Arize-ai/openinference) tracing for the Kiro CLI. Spans are exported to [Arize AX](https://arize.com) or [Phoenix](https://github.com/Arize-ai/phoenix). Each traced session emits LLM turns, tool calls, cost in credits, model information, and turn duration. Token counts (`llm.token_count.prompt`, `llm.token_count.completion`) are included only when Kiro CLI reports them — currently Kiro bills via credits, not tokens.

## Setup

The installer prompts for your backend (Phoenix or Arize AX) and project name, writes credentials to `~/.arize/harness/config.yaml`, and registers hooks in a Kiro agent config under `~/.kiro/agents/<agent>.json` (default agent: `arize-traced`). You can optionally have the installer run `kiro-cli agent set-default <agent>` so the traced agent is used by default.

Pass `--with-skills` to also symlink the `manage-kiro-tracing` skill into the current directory's `.agents/skills/` so coding agents in this workspace can help manage Kiro tracing configuration.

### Install with `acht` (recommended)

`acht` is a small Go CLI that bootstraps the Python runtime for you and wires up the harness. It works the same on macOS, Linux, and Windows, and runs `acht doctor` to diagnose problems without depending on the venv.

macOS / Linux:

```bash
# One-time: install the acht binary
curl -sSL https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install-acht.sh | bash

# Install Kiro tracing
acht add kiro

# Uninstall
acht uninstall --kiro
```

Windows (PowerShell):

```powershell
# One-time: install the acht binary
irm https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install-acht.ps1 | iex

# Install Kiro tracing
acht add kiro

# Uninstall
acht uninstall --kiro
```

#### Non-interactive install

Pre-set every prompt with flags and env vars for unattended installs (CI, devcontainers, fleet provisioning). `ARIZE_API_KEY` (or `PHOENIX_API_KEY`) is read from the environment only — never pass it as a flag.

```bash
ARIZE_API_KEY="$ARIZE_API_KEY" acht add kiro \
  --non-interactive \
  --backend arize \
  --space-id YOUR_SPACE_ID \
  --project-name kiro
```

### Alternative: `install.sh` / `install.bat`

The Python installer still works and remains supported. It targets the same `~/.arize/harness/` layout as `acht`, so the two are interchangeable.

```bash
git clone https://github.com/Arize-ai/coding-harness-tracing.git
cd coding-harness-tracing
```

macOS / Linux:

```bash
# Install
./install.sh kiro

# Uninstall
./install.sh uninstall kiro
```

Windows:

```powershell
# Install
install.bat kiro

# Uninstall
install.bat uninstall kiro
```

Or run the installer directly from GitHub:

```bash
curl -sSL https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.sh | bash -s -- kiro
```

```powershell
iwr -useb https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat kiro
```

## Default Settings

| Setting | Default |
|---------|---------|
| Harness key | `kiro` |
| Project name | `kiro` |
| Phoenix endpoint | `http://localhost:6006` |
| Arize AX endpoint | `otlp.arize.com:443` |
| Default agent name | `arize-traced` |
| Hook config file | `~/.kiro/agents/<agent>.json` |
| Hook events registered | `agentSpawn`, `userPromptSubmit`, `preToolUse`, `postToolUse`, `stop` |
| Session sidecar dir | `~/.kiro/sessions/cli/` |
| State directory | `~/.arize/harness/state/kiro/` |
| Log file | `~/.arize/harness/logs/kiro.log` |

## Usage

```bash
# If you set arize-traced as Kiro's default during install:
kiro-cli chat
# Otherwise:
kiro-cli chat --agent arize-traced
```

Errors land in `~/.arize/harness/logs/kiro.log` always; set `export ARIZE_VERBOSE=true` before launching Kiro to also see routine hook activity. See the [main README's Environment variables section](../../README.md#environment-variables) for the full list of runtime overrides (`ARIZE_TRACE_ENABLED`, `ARIZE_DRY_RUN`, `ARIZE_USER_ID`, etc.).

## Span shape

### LLM span

| Attribute | Description |
|-----------|-------------|
| `session.id` | Kiro session UUID |
| `openinference.span.kind` | `LLM` |
| `input.value` | User prompt |
| `output.value` | Assistant response |
| `llm.output_messages` | Structured assistant response |
| `llm.model_name` | Model ID from the session sidecar (e.g. `auto`) |
| `llm.token_count.prompt` | Prompt token count (when reported, omitted when 0) |
| `llm.token_count.completion` | Completion token count (when reported, omitted when 0) |
| `llm.token_count.total` | Total token count (when reported, omitted when 0) |
| `kiro.cost.credits` | Cost in credits from metering data |
| `kiro.metering_usage` | Full metering usage JSON |
| `kiro.turn_duration_ms` | Turn duration in milliseconds |
| `kiro.agent_name` | Name of the Kiro agent |
| `kiro.context_usage_percentage` | Context window usage percentage |

LLM spans are enriched from the session sidecar at `~/.kiro/sessions/cli/<session_id>.json`. Enrichment is fail-soft — if the sidecar is unavailable, the span is emitted with basic attributes only.

### TOOL span

| Attribute | Description |
|-----------|-------------|
| `tool.name` | Tool name (alias form) |
| `tool.description` | Purpose of the tool call (from `__tool_use_purpose` in tool input) |
| `input.value` | Serialized tool input JSON |
| `output.value` | Serialized tool response JSON |

TOOL spans are parented to the LLM turn they belong to.

## Known limitations

- **Token counts are 0.** `input_token_count` and `output_token_count` are reported as 0 in current Kiro CLI versions. Kiro meters in credits instead — see `kiro.cost.credits`. Token count attributes are omitted when 0.
- **FIFO tool matching.** Kiro does not expose a tool-call ID, so pre/post tool events are matched using a FIFO stack. This assumes serial tool execution within a session.
- **Sidecar read is fail-soft.** The session sidecar may not exist or may lag behind hook events due to a flush race. When this happens, the LLM span is emitted without enrichment attributes (model name, cost, duration).

## Uninstall

Uninstall removes hook entries from the agent config. If the `arize-traced` agent was created by the installer, the agent file is deleted. If hooks were added to a pre-existing agent, the hooks are removed but the agent file is preserved.
