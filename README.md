# Arize Coding Harness Tracing

Trace AI coding sessions to [Arize AX](https://arize.com) or [Phoenix](https://github.com/Arize-ai/phoenix) with [OpenInference](https://github.com/Arize-ai/openinference) spans. Each harness integration emits spans for prompts, tool calls, model responses, and session lifecycle events.

Claude Code tracing reconstructs each turn as a `CHAIN` containing per-response `LLM` spans, correlated `TOOL` spans, and a foreground subagent `AGENT` subtree when available. See [Claude Code trace structure and current limitations](tracing/claude_code/README.md#trace-structure) for details.

## Supported Harnesses

| Harness Integration | Install command | Name |
|---------------------|-----------------|------|
| [Claude Code CLI / Agent SDK](tracing/claude_code/README.md) | [macOS / Linux](tracing/claude_code/README.md#macos--linux) · [Windows](tracing/claude_code/README.md#windows-powershell) | `claude` |
| [Claude Code CLI / Agent SDK](tracing/claude_code/README.md) | [Claude Plugin](tracing/claude_code/README.md#claude-code-marketplace) | `claude-code-tracing` |
| [OpenAI Codex CLI](tracing/codex/README.md) | [macOS / Linux](tracing/codex/README.md#macos--linux) · [Windows](tracing/codex/README.md#windows-powershell) | `codex` |
| [Cursor IDE / CLI](tracing/cursor/README.md) | [macOS / Linux](tracing/cursor/README.md#macos--linux) · [Windows](tracing/cursor/README.md#windows-powershell) | `cursor` |
| [Cursor IDE / CLI](tracing/cursor/README.md) | [Cursor Plugin](tracing/cursor/README.md#plugin-install) | `cursor-tracing` |
| [Devin](tracing/devin/README.md) | [macOS / Linux](tracing/devin/README.md#macos--linux) · [Windows](tracing/devin/README.md#windows-powershell) | `devin` |
| [GitHub Copilot (VS Code + CLI)](tracing/copilot/README.md) | [macOS / Linux](tracing/copilot/README.md#macos--linux) · [Windows](tracing/copilot/README.md#windows-powershell) | `copilot` |
| [Gemini CLI](tracing/gemini/README.md) | [macOS / Linux](tracing/gemini/README.md#macos--linux) · [Windows](tracing/gemini/README.md#windows-powershell) | `gemini` |
| [Kiro CLI](tracing/kiro/README.md) | [macOS / Linux](tracing/kiro/README.md#macos--linux) · [Windows](tracing/kiro/README.md#windows-powershell) | `kiro` |
| [Google Antigravity CLI / IDE](tracing/antigravity/README.md) | [macOS / Linux](tracing/antigravity/README.md#macos--linux) · [Windows](tracing/antigravity/README.md#windows-powershell) | `antigravity` |
| [Opencode CLI](tracing/opencode/README.md) | [macOS / Linux](tracing/opencode/README.md#macos--linux) · [Windows](tracing/opencode/README.md#windows-powershell) | `opencode` |
| [Oh My Pi (omp)](tracing/omp/README.md) | [macOS / Linux](tracing/omp/README.md#macos--linux) · [Windows](tracing/omp/README.md#windows-powershell) | `omp` |

> **Each install link opens the ready-to-paste command for your OS — copy it and run it in a terminal**

> Installing Claude Code tracing via the Claude marketplace? See [Claude Code Tracing](tracing/claude_code/README.md#claude-code-marketplace) for the marketplace-specific flow — backend credentials must be set directly in `~/.claude/settings.json` since the install wizard is skipped.

> Installing Cursor tracing via the Cursor marketplace? The `cursor-tracing` plugin registers all hook events automatically; run the bundled `manage-cursor-tracing` skill once after install to write backend credentials to `~/.arize/harness/config.json`. See [Cursor IDE Tracing](tracing/cursor/README.md#plugin-install) for the full flow.

### Setup walkthrough

The installer involves a brief interactive setup. The steps below run in order. To skip all of them, see [Non-interactive install](#non-interactive-install).

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

#### 3. User ID (optional)

A free-form identifier attached to every span as `user.id`. Useful when multiple teammates share the same backend. Leave blank to skip. If the entered ID is blank or is not an email address, Arize AX also checks `ARIZE_USER_ID` when choosing the project default.

#### 4. Project name

The project that groups spans for this harness. Arize AX defaults to `harness/<email>`, using the first email address found in this order:

1. The user ID entered during installation.
2. `ARIZE_USER_ID`. The file named by `ARIZE_ENV_FILE` takes precedence over the process environment.
3. The saved `harnesses.<harness>.user_id`.
4. The saved top-level `user_id`.
5. `git user.email`.

Values that are not email addresses are skipped. If no email is found, the default is the harness name and the installer prints commands that set an email. Phoenix defaults to the harness name. Selecting an email for the project does not change the saved user ID.

Press Enter to accept the default or type another project name. A backend-specific project setting in the file named by `ARIZE_ENV_FILE` overrides the saved project and generated default. Without that file setting, reinstalling keeps the saved project. The installer ignores project variables inherited from the process environment in both interactive and non-interactive mode. A typed project name wins over every default.

#### 5. Content logging

Three Y/n opt-outs that apply to **all** harnesses:

- Log user prompts?
- Log what tools were asked to do (commands, file paths, URLs)?
- Log what tools returned (file contents, command output)?

You're only asked these the first time you install a harness — subsequent installs reuse the existing `logging:` block. You can edit them later in `~/.arize/harness/config.json`.

### Non-interactive install

Pass `--non-interactive` (or `-y`) to skip every prompt above and use the resolution rules below. A missing required value is an error rather than a prompt. Use this mode from a script, from CI, or when a coding agent drives the install.

Values come from the environment, or from a dotenv file named explicitly with `ARIZE_ENV_FILE`. Using a file keeps the API key out of the command line and shell history.

**A named file takes precedence over existing environment variables** — an already-installed harness exports `ARIZE_API_KEY`, `ARIZE_SPACE_ID` and `ARIZE_PROJECT_NAME` into every agent session, and those inherited values should not beat credentials you just wrote to a file. Only the keys below are read out of it, so a file full of unrelated settings is safe to use.

Parsing is [`python-dotenv`](https://pypi.org/project/python-dotenv/)'s `dotenv_values`, so quoting, `export` prefixes, comments and escapes behave as they do for every other dotenv consumer. It is the package's only runtime dependency, imported by the installer alone — the hooks that run inside a traced session still use nothing outside the stdlib. A line it cannot parse is a hard error rather than a skip: a credential quietly falling back to the environment is the outcome this whole section exists to avoid.

There is deliberately **no automatic `./.env` search**. Because a named file outranks the environment, reading the working directory would let a cloned repository's dotenv choose `ARIZE_OTLP_ENDPOINT` or `PHOENIX_ENDPOINT` while your real credentials came from the environment — installing a config that sends spans and a bearer API key to an endpoint the repo picked, for every later session on the machine. Name the file you mean:

```bash
ARIZE_ENV_FILE=~/.arize/onboarding.env ./install.sh claude --non-interactive
```

```bash
./install.sh claude --non-interactive
```

| Variable | Default | Description |
|----------|---------|-------------|
| `ARIZE_API_KEY` + `ARIZE_SPACE_ID` | — | Arize AX credentials. Both required for the Arize backend. |
| `PHOENIX_ENDPOINT`, `PHOENIX_API_KEY` | `http://localhost:6006` | Phoenix endpoint and optional API key. |
| `ARIZE_BACKEND` | inferred | `arize` or `phoenix`. Inferred when unset: a space ID means Arize AX, a Phoenix endpoint means Phoenix. When both are present, or an Arize key appears with only a Phoenix endpoint, the install stops and asks you to set this rather than guess — guessing would discard one backend's credentials. |
| `ARIZE_PROJECT_NAME` | `harness/<email>` | Arize AX project. Read only from the file named by `ARIZE_ENV_FILE`, before a saved project or generated default. The installer ignores the process environment value. |
| `PHOENIX_PROJECT`, `PHOENIX_PROJECT_NAME` | harness name | Phoenix project. Read only from the file named by `ARIZE_ENV_FILE`. `PHOENIX_PROJECT` wins when both are set in that file. The installer ignores process environment values. |
| `ARIZE_USER_ID` | — | Optional `user.id` on every span. Also an email source for the Arize AX project default when the entered install user ID is blank or is not an email address. |
| `ARIZE_OTLP_ENDPOINT` | `otlp.arize.com:443` | Override for hosted/dedicated Arize instances. |
| `ARIZE_LOG_PROMPTS` | `false` | Set `true` to capture prompt text. |
| `ARIZE_LOG_TOOL_DETAILS` | `false` | Set `true` to capture tool commands, file paths and URLs. |
| `ARIZE_LOG_TOOL_CONTENT` | `false` | Set `true` to capture tool output. |
| `ARIZE_ENV_FILE` | — | Dotenv file to read. No file is read unless this is set. A path that is not a readable file is an error, not a fall-back to the environment. |
| `ARIZE_WHEEL_DIR` | — | Install from local wheels in this directory instead of downloading the repo. Same as `--wheel-dir`; no network and no remote code execution. Supported by `install.sh` and `install.bat` alike. |
| `ARIZE_KIRO_AGENT` | `arize-traced` | Kiro only — which agent to install hooks into. |
| `ARIZE_KIRO_SET_DEFAULT` | `false` | Kiro only — also make that agent Kiro's default. |

In a dotenv file, an unquoted value ends at a whitespace-preceded `#`, so `ARIZE_SPACE_ID=abc # my space` yields `abc`. Quote the value to keep a literal `#`.

Content logging is **off by default here**, unlike the interactive wizard where each question defaults to yes. A `[Y/n]` default is a person declining to change an answer they were shown; the same default unattended would capture prompts, commands and file contents that nobody agreed to — and `update` runs non-interactively whenever there is no terminal. Set the `ARIZE_LOG_*` variables you want to `true`.

The API key is never echoed. The installer reports only that it found one and where it came from. Resolved settings include their source, such as a dotenv path, `$VAR`, `saved project`, or `default`, so a wrong-credentials install is diagnosable:

```
[arize] Backend: Arize AX at otlp.arize.com:443 (from default)
[arize]   space ID: my-space (from /path/to/.env)
[arize]   API key: found (from /path/to/.env)
[arize] Project name: harness/dev@example.com (from default)
```

An API key on its own is rejected as ambiguous, since both backends use one.

### Updating

`install.sh update` pulls the latest code and re-registers every harness already in `config.json`. Re-registering runs each harness's installer, so in a terminal it still asks for each project name, exactly as before.

With no terminal to answer on — CI, a cron job, a script — it takes the stored values instead of failing. Credentials aren't re-read on that path; only the project name is confirmed, and it keeps whatever is in `config.json`.

### Checking what's installed

`status` reports which harnesses are configured and whether their hooks are actually wired into the harness's own settings file — the two things that have to both be true for traces to appear.

```bash
./install.sh status
./install.sh status --json    # machine-readable
```

`--json` is the one to use from a script or a coding agent — you can gate on the exit code without parsing output:

| Exit | Meaning |
|------|---------|
| `0` | every configured harness is wired up |
| `1` | nothing configured |
| `2` | configured, but at least one harness's hooks are missing |

The JSON payload carries the same verdict as `"healthy"`, plus `"unregistered"` listing any harness whose hooks are missing. It contains no secrets — an API key appears only as `"api_key_present": true` — so it is safe to paste into a bug report.

```
Harnesses:
  claude-code
    project:  claude-code
    backend:  arize → otlp.arize.com:443
    space:    my-space
    API key:  present
    hooks:    registered (/Users/me/.claude/settings.json)
```

`hooks: NOT registered` means credentials are saved but the harness was never wired up (or something removed the hooks) — re-run the install for that harness.

### Environment variables

Most settings live in `.arize/harness/config.json`, but a small set of env vars affect runtime behavior on every harness. The installers wire most of these for you; set them yourself when you want to override behavior for a single session or debug locally.

| Variable | Default | Description |
|----------|---------|-------------|
| `ARIZE_TRACE_ENABLED` | `true` | Master toggle. Set to `false` to disable hooks without uninstalling. |
| `ARIZE_VERBOSE` | `false` | Enables `[arize] ...` log lines in `~/.arize/harness/logs/<harness>.log`. Errors are always logged; verbose adds routine activity (hook fires, span emits, state transitions). |
| `ARIZE_DRY_RUN` | `false` | Build spans but skip the backend send. Useful for confirming hook wiring without writing data. |
| `ARIZE_USER_ID` | — | Attached to every span as `user.id`. Mirrors the `user_id` field in `config.json`; env wins if both are set. |
| `ARIZE_PROJECT_NAME` | per-harness | Overrides `harnesses.<name>.project_name` from `config.json` for a single session. **Arize backend only** — ignored on the Phoenix backend (use `PHOENIX_PROJECT` there). |
| `ARIZE_LOG_FILE` | per-harness | Path the harness writes its log to. Adapters default to `~/.arize/harness/logs/<harness>.log`. |
| `ARIZE_TRACE_DEBUG` | `false` | Dump raw hook payloads as JSON under `~/.arize/harness/state/<harness>/debug/`. Codex hooks use this for span-tree inspection. |
| `OTEL_RESOURCE_ATTRIBUTES` | — | Standard OTel attribute string (`team=payments,environment=prod`) added to every span. Overrides `config.json` `attributes`/`harnesses.<name>.attributes` on key collision; set per-harness by placing it in that harness's settings env block. |

**Backend overrides** (set if you want env to take priority over `config.json` for a single run):

| Variable | Description |
|----------|-------------|
| `ARIZE_API_KEY`, `ARIZE_SPACE_ID`, `ARIZE_OTLP_ENDPOINT` | Arize AX credentials and endpoint. |
| `PHOENIX_ENDPOINT`, `PHOENIX_API_KEY` | Phoenix endpoint and (optional) API key. |
| `PHOENIX_PROJECT`, `PHOENIX_PROJECT_NAME` | Project override on the **Phoenix backend** (mirrors `ARIZE_PROJECT_NAME` for Arize). `PHOENIX_PROJECT` wins if both are set; both override `harnesses.<name>.project_name`. |

The Phoenix OTLP path sends `Authorization: Bearer <api_key>` when `api_key` is set. An optional `headers` object on that harness is copied onto the request, for a different `Authorization` scheme or any extra headers. It is not written by the installer. If `headers` already includes `Authorization`, `api_key` is not turned into a second Bearer header. `Content-Type` stays `application/x-protobuf`.

```json
"headers": {
  "Authorization": "Basic <token>",
  "X-Custom-Header": "value"
}
```

> Claude Code plugin reads env vars from `~/.claude/settings.json` under the `env` block

## Links

- [Arize AX](https://arize.com)
- [Phoenix](https://github.com/Arize-ai/phoenix)
- [OpenInference](https://github.com/Arize-ai/openinference)

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, the contribution process, and the CLA

## License

[Apache 2.0](LICENSE)
