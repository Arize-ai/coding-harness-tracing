# Arize Coding Harness Tracing

Trace AI coding sessions to [Arize AX](https://arize.com) or [Phoenix](https://github.com/Arize-ai/phoenix) with [OpenInference](https://github.com/Arize-ai/openinference) spans. Each harness integration emits spans for prompts, tool calls, model responses, and session lifecycle events.

## Supported Harnesses

| Harness Integration | Install | Name |
|---------------------|---------|------|
| [Claude Code CLI / Agent SDK](tracing/claude_code/README.md) | `install.sh` / `install.bat` | `claude` |
| [Claude Code CLI / Agent SDK](tracing/claude_code/README.md) | `Claude Plugin (see below)`| `claude-code-tracing` |
| [OpenAI Codex CLI](tracing/codex/README.md) | `install.sh` / `install.bat` | `codex` |
| [Cursor IDE / CLI](tracing/cursor/README.md) | `install.sh` / `install.bat` | `cursor` |
| [GitHub Copilot (VS Code + CLI)](tracing/copilot/README.md) | `install.sh` / `install.bat` | `copilot` |
| [Gemini CLI](tracing/gemini/README.md) | `install.sh` / `install.bat` | `gemini` |
| [Kiro CLI](tracing/kiro/README.md) | `install.sh` / `install.bat` | `kiro` |
| [Opencode CLI](tracing/opencode/README.md) | `install.sh` / `install.bat` | `opencode` |

Claude Code CLI and the Claude Agent SDK share the same plugin, hooks, and configuration — one install covers both.

## Install

> Installing Claude Code tracing via the Claude marketplace? See [Claude Code Tracing](tracing/claude_code/README.md#claude-code-marketplace) for the marketplace-specific flow — backend credentials must be set directly in `~/.claude/settings.json` since the install wizard is skipped.

### Quickstart

Access and run the install script remotely to setup coding harness tracing in your local environment.

**macOS / Linux:**

```bash
INSTALL_URL="https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.sh"

# claude | codex | gemini | cursor | copilot | kiro | opencode
HARNESS="claude"

# setup tracing for a harness
curl -sSL "$INSTALL_URL" | bash -s -- "$HARNESS"

# remove tracing for a harness
curl -sSL "$INSTALL_URL" | bash -s -- uninstall "$HARNESS"

# remove tracing for all harnesses
curl -sSL "$INSTALL_URL" | bash -s -- uninstall
```

**Windows:**

```powershell
$INSTALL_URL = "https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.bat"

# claude | codex | gemini | cursor | copilot | kiro | opencode
$HARNESS = "claude"

iwr -useb $INSTALL_URL -OutFile $env:TEMP\install.bat

# setup tracing for a harness
& $env:TEMP\install.bat $HARNESS

# remove tracing for a harness
& $env:TEMP\install.bat uninstall $HARNESS

# remove tracing for all harnesses
& $env:TEMP\install.bat uninstall
```

### Local Copy

Clone the repo and then run install on your own machine.

```bash
git clone https://github.com/Arize-ai/coding-harness-tracing.git
cd coding-harness-tracing
```

**macOS / Linux**
```bash
# claude | codex | gemini | cursor | copilot | kiro | opencode
HARNESS="claude"

# setup tracing for a harness
./install.sh "$HARNESS"

# remove tracing for a harness
./install.sh uninstall "$HARNESS"

# remove tracing for all harnesses
./install.sh uninstall
```

**Windows**
```powershell
# claude | codex | gemini | cursor | copilot | kiro | opencode
$HARNESS = "claude"

# setup tracing for a harness
install.bat $HARNESS

# remove tracing for a harness
install.bat uninstall $HARNESS

# remove tracing for all harnesses
install.bat uninstall
```

Uninstall removes the harness configuration and cleans up runtime files. For Codex, the buffer service is stopped. You will be prompted before any user-owned config (credentials, state files) is deleted.

### Setup walkthrough

After running `install.sh` / `install.bat` for a harness, the installer drops you into an interactive setup. The steps below run in order:

#### 1. Harness detection

The installer first checks whether the target harness (e.g. `claude`, `codex`) appears installed on this machine — looking on `PATH` and in the harness's home directory. If it isn't found, you'll see a warning and a prompt to install tracing anyway. Choose `N` to abort if the host CLI isn't ready yet.

#### 2. Backend selection

Pick where spans should be sent:

- **1) Phoenix (self-hosted)** — your own Phoenix instance.
- **2) Arize AX (cloud)** — the hosted Arize platform.

If you've already configured another harness against the same backend, the installer offers a **copy-from** menu so you can reuse those credentials instead of re-entering them.

#### 3. Credentials

Prompts depend on the backend:

- **Phoenix:** endpoint (defaults to `http://localhost:6006`) and an optional API key (leave blank for no auth).
- **Arize AX:** API key, Space ID, and OTLP endpoint (defaults to `otlp.arize.com:443` — only override for hosted/dedicated instances).

#### 4. Project name

The project (in Arize/Phoenix) that spans for this harness are grouped under. Defaults to the harness name (e.g. `claude-code`, `codex`).

#### 5. User ID (optional)

A free-form identifier attached to every span as `user.id`. Useful when multiple teammates share the same backend. Leave blank to skip.

#### 6. Content logging

Three Y/n opt-outs that apply to **all** harnesses:

- Log user prompts?
- Log what tools were asked to do (commands, file paths, URLs)?
- Log what tools returned (file contents, command output)?

You're only asked these the first time you install a harness — subsequent installs reuse the existing `logging:` block. You can edit them later in `~/.arize/harness/config.yaml`.

## Configuration

All configuration lives in `~/.arize/harness/config.yaml`, written by the installer. This file is the single source of truth for backend credentials and per-harness settings.

### config.yaml Fields

**Per-harness settings** (under `harnesses.<name>`)

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `harnesses.<name>.project_name` | No | harness name | Project name in Arize/Phoenix |
| `harnesses.<name>.target` | Yes | — | `phoenix` or `arize` |
| `harnesses.<name>.endpoint` | Yes | — | Phoenix server URL or Arize OTLP gRPC endpoint |
| `harnesses.<name>.api_key` | Arize: Yes | — | Arize AX API key (or optional Phoenix API key) |
| `harnesses.<name>.space_id` | Arize: Yes | — | Arize AX space ID |

**Codex-only** (under `harnesses.codex.collector`)

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `harnesses.codex.collector.host` | No | `127.0.0.1` | Codex buffer service listen address |
| `harnesses.codex.collector.port` | No | `4318` | Codex buffer service listen port |

**Content logging** (under top-level `logging`, applies to all harnesses)

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `logging.prompts` | No | `true` | Include user prompt text in spans |
| `logging.tool_details` | No | `true` | Include tool arguments (commands, file paths, URLs, queries) |
| `logging.tool_content` | No | `true` | Include tool input/output content (file contents, command output) |

**User**

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `user_id` | No | — | User identifier added to all spans as `user.id` |

Each harness owns its full backend configuration directly — there is no shared global backend block. This allows different harnesses to use different backends or credentials.

### Environment variables

Most settings live in `config.yaml`, but a small set of env vars affect runtime behavior on every harness. The installers wire most of these for you; set them yourself when you want to override behavior for a single session or debug locally.

| Variable | Default | Description |
|----------|---------|-------------|
| `ARIZE_TRACE_ENABLED` | `true` | Master toggle. Set to `false` to disable hooks without uninstalling. |
| `ARIZE_VERBOSE` | `false` | Enables `[arize] ...` log lines in `~/.arize/harness/logs/<harness>.log`. Errors are always logged; verbose adds routine activity (hook fires, span emits, state transitions). |
| `ARIZE_DRY_RUN` | `false` | Build spans but skip the backend send. Useful for confirming hook wiring without writing data. |
| `ARIZE_USER_ID` | — | Attached to every span as `user.id`. Mirrors the `user_id` field in `config.yaml`; env wins if both are set. |
| `ARIZE_PROJECT_NAME` | per-harness | Overrides `harnesses.<name>.project_name` from `config.yaml` for a single session. |
| `ARIZE_LOG_FILE` | per-harness | Path the harness writes its log to. Adapters default to `~/.arize/harness/logs/<harness>.log`. |
| `ARIZE_TRACE_DEBUG` | `false` | Dump raw hook payloads as YAML under `~/.arize/harness/state/<harness>/debug/`. Codex hooks use this for span-tree inspection. |

**Backend overrides** (set if you want env to take priority over `config.yaml` for a single run):

| Variable | Description |
|----------|-------------|
| `ARIZE_API_KEY`, `ARIZE_SPACE_ID`, `ARIZE_OTLP_ENDPOINT` | Arize AX credentials and endpoint. |
| `PHOENIX_ENDPOINT`, `PHOENIX_API_KEY` | Phoenix endpoint and (optional) API key. |

Claude Code reads env vars from `~/.claude/settings.json` under the `env` block; Codex from `~/.codex/arize-env.sh`; Cursor / Copilot / Gemini / Kiro pick up host shell env. See the per-harness READMEs for details.

## Links

- [Arize AX](https://arize.com)
- [Phoenix](https://github.com/Arize-ai/phoenix)
- [OpenInference](https://github.com/Arize-ai/openinference)

## License

MIT
