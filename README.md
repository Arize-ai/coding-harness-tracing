# Arize Coding Harness Tracing

Trace AI coding sessions to [Arize AX](https://arize.com) or [Phoenix](https://github.com/Arize-ai/phoenix) with [OpenInference](https://github.com/Arize-ai/openinference) spans. Each harness integration emits spans for prompts, tool calls, model responses, and session lifecycle events.

## Supported Harnesses

| Harness Integration | Install command | Name |
|---------------------|-----------------|------|
| [Claude Code CLI / Agent SDK](tracing/claude_code/README.md) | [macOS / Linux](tracing/claude_code/README.md#macos--linux) · [Windows](tracing/claude_code/README.md#windows-powershell) | `claude` |
| [Claude Code CLI / Agent SDK](tracing/claude_code/README.md) | [Claude Plugin](tracing/claude_code/README.md#claude-code-marketplace) | `claude-code-tracing` |
| [OpenAI Codex CLI](tracing/codex/README.md) | [macOS / Linux](tracing/codex/README.md#macos--linux) · [Windows](tracing/codex/README.md#windows-powershell) | `codex` |
| [Cursor IDE / CLI](tracing/cursor/README.md) | [macOS / Linux](tracing/cursor/README.md#macos--linux) · [Windows](tracing/cursor/README.md#windows-powershell) | `cursor` |
| [GitHub Copilot (VS Code + CLI)](tracing/copilot/README.md) | [macOS / Linux](tracing/copilot/README.md#macos--linux) · [Windows](tracing/copilot/README.md#windows-powershell) | `copilot` |
| [Gemini CLI](tracing/gemini/README.md) | [macOS / Linux](tracing/gemini/README.md#macos--linux) · [Windows](tracing/gemini/README.md#windows-powershell) | `gemini` |
| [Kiro CLI](tracing/kiro/README.md) | [macOS / Linux](tracing/kiro/README.md#macos--linux) · [Windows](tracing/kiro/README.md#windows-powershell) | `kiro` |
| [Opencode CLI](tracing/opencode/README.md) | [macOS / Linux](tracing/opencode/README.md#macos--linux) · [Windows](tracing/opencode/README.md#windows-powershell) | `opencode` |

> **Each install link opens the ready-to-paste command for your OS — copy it and run it in a terminal**

> Installing Claude Code tracing via the Claude marketplace? See [Claude Code Tracing](tracing/claude_code/README.md#claude-code-marketplace) for the marketplace-specific flow — backend credentials must be set directly in `~/.claude/settings.json` since the install wizard is skipped.

### Setup walkthrough

The installer involves a brief interactive setup. The steps below run in order:

#### 1. Backend selection

Choose where spans should be sent:

- **1) Phoenix** — your own Phoenix instance.
- **2) Arize AX** — the hosted Arize platform.

#### 2. Credentials

Prompts depend on the backend:

- **Phoenix:**
    - endpoint (defaults to `http://localhost:6006`)
    - optional API key (leave blank for no auth)
- **Arize AX:**
    - [Arize API key](https://arize.com/docs/ax/security-and-settings/api-keys)
    - Space ID (found in Arize settings tab along with api keys)
    - OTLP endpoint (defaults to `otlp.arize.com:443` — only override for hosted/dedicated instances).

If you've already configured another harness against the same backend, the installer offers a **copy-from** menu so you can reuse those credentials instead of re-entering them.

#### 3. Project name

The project (in Arize/Phoenix) that spans for this harness are grouped under. Defaults to the harness name (e.g. `claude-code`, `codex` etc).

#### 4. User ID (optional)

A free-form identifier attached to every span as `user.id`. Useful when multiple teammates share the same backend. Leave blank to skip.

#### 5. Content logging

Three Y/n opt-outs that apply to **all** harnesses:

- Log user prompts?
- Log what tools were asked to do (commands, file paths, URLs)?
- Log what tools returned (file contents, command output)?

You're only asked these the first time you install a harness — subsequent installs reuse the existing `logging:` block. You can edit them later in `~/.arize/harness/config.json`.

### Environment variables

Most settings live in `.arize/harness/config.json`, but a small set of env vars affect runtime behavior on every harness. The installers wire most of these for you; set them yourself when you want to override behavior for a single session or debug locally.

| Variable | Default | Description |
|----------|---------|-------------|
| `ARIZE_TRACE_ENABLED` | `true` | Master toggle. Set to `false` to disable hooks without uninstalling. |
| `ARIZE_VERBOSE` | `false` | Enables `[arize] ...` log lines in `~/.arize/harness/logs/<harness>.log`. Errors are always logged; verbose adds routine activity (hook fires, span emits, state transitions). |
| `ARIZE_DRY_RUN` | `false` | Build spans but skip the backend send. Useful for confirming hook wiring without writing data. |
| `ARIZE_USER_ID` | — | Attached to every span as `user.id`. Mirrors the `user_id` field in `config.json`; env wins if both are set. |
| `ARIZE_PROJECT_NAME` | per-harness | Overrides `harnesses.<name>.project_name` from `config.json` for a single session. |
| `ARIZE_LOG_FILE` | per-harness | Path the harness writes its log to. Adapters default to `~/.arize/harness/logs/<harness>.log`. |
| `ARIZE_TRACE_DEBUG` | `false` | Dump raw hook payloads as JSON under `~/.arize/harness/state/<harness>/debug/`. Codex hooks use this for span-tree inspection. |
| `OTEL_RESOURCE_ATTRIBUTES` | — | Standard OTel attribute string (`team=payments,environment=prod`) added to every span. Overrides `config.json` `attributes`/`harnesses.<name>.attributes` on key collision; set per-harness by placing it in that harness's settings env block. |

**Backend overrides** (set if you want env to take priority over `config.json` for a single run):

| Variable | Description |
|----------|-------------|
| `ARIZE_API_KEY`, `ARIZE_SPACE_ID`, `ARIZE_OTLP_ENDPOINT` | Arize AX credentials and endpoint. |
| `PHOENIX_ENDPOINT`, `PHOENIX_API_KEY` | Phoenix endpoint and (optional) API key. |

> Claude Code plugin reads env vars from `~/.claude/settings.json` under the `env` block

## Links

- [Arize AX](https://arize.com)
- [Phoenix](https://github.com/Arize-ai/phoenix)
- [OpenInference](https://github.com/Arize-ai/openinference)

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, the contribution process, and the CLA.

## License

[Apache 2.0](LICENSE)
