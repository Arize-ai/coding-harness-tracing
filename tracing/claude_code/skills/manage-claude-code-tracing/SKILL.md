---
name: manage-claude-code-tracing
description: Set up and manage Arize tracing for Claude Code sessions or Agent SDK applications using the acht CLI. Use when users want to set up tracing, configure Arize AX or Phoenix, edit tracing config, run diagnostics, enable/disable tracing, or troubleshoot Claude Code tracing. Triggers on "set up tracing", "configure Arize", "configure Phoenix", "acht", "enable tracing", "setup-claude-code-tracing", "create Arize project", "get Arize API key", "agent sdk tracing", or any request about connecting Claude Code or the Agent SDK to Arize or Phoenix for observability.
---

# Manage Claude Code Tracing

Configure OpenInference tracing for Claude Code (CLI or Agent SDK) to Arize AX (cloud) or Phoenix (self-hosted). Spans are sent directly to the backend from hooks — no background process or backend-specific Python deps run in the user's environment.

The primary tool is the **`acht`** CLI. It installs a managed Python runtime (via [uv](https://github.com/astral-sh/uv)), registers the Claude Code hooks, and manages config. Reach for the repo only when you need to inspect hook/handler internals: <https://github.com/Arize-ai/coding-harness-tracing> (Claude Code code under `tracing/claude_code/`).

## How to use this skill

Figure out which path the user needs — don't walk through every section:

1. **Installing for the first time / adding tracing?** → [Install](#install)
2. **Have credentials, need to change a setting (project name, backend, logging)?** → [Configure via the CLI](#configure-via-the-cli)
3. **Tracing not working / debugging?** → [Diagnose with doctor](#diagnose-with-doctor) then [Troubleshoot](#troubleshoot)
4. **Building on the Agent SDK (Python/TypeScript)?** → [Agent SDK setup](#agent-sdk-setup)
5. **No backend account/credentials yet?** → [Backends](#backends) first

## Install

```bash
# install the CLI (any platform with Go)
go install github.com/Arize-ai/coding-harness-tracing/cmd/acht@latest

# configure Claude Code tracing (interactive)
acht add claude
```

`acht add claude` bootstraps the runtime, registers hooks in `~/.claude/settings.json`, and runs an interactive wizard. The wizard collects:

| Field | Notes |
|-------|-------|
| Backend | `arize` or `phoenix` |
| API key | `ARIZE_API_KEY` / `PHOENIX_API_KEY` — env var or masked prompt, never a flag |
| Space ID | Arize only |
| OTLP / Phoenix endpoint | Arize defaults to `otlp.arize.com:443`; Phoenix to `http://localhost:6006` |
| Project name | Defaults to `claude-code` |
| User ID | Optional; added to every span as `user.id` |
| Content logging | Three prompts (prompts / tool details / tool content), default **on** |
| Verbose | Print trace summaries to terminal, default **off** |

**Non-interactive (CI/scripts)** — pass fields as flags, key via env var:

```bash
export ARIZE_API_KEY=...
acht add claude \
  --backend arize --space-id SPACE_ID \
  --project-name my-project --non-interactive
```

Restart the Claude Code session after install for hooks to take effect.

## Backends

### Arize AX (cloud)

Ask whether they're on SaaS or on-prem. SaaS uses `otlp.arize.com:443`; on-prem needs their custom OTLP endpoint.

To get credentials: log in (https://app.arize.com for SaaS), open **Settings** → the **Space ID** is on Space Settings; the **API Keys** tab creates/reveals an API key. Both `api_key` and `space_id` are required.

### Phoenix (self-hosted)

If they don't already run Phoenix:

```bash
pip install arize-phoenix && phoenix serve   # or: docker run -p 6006:6006 arizephoenix/phoenix:latest
```

UI at `http://localhost:6006`. Verify: `curl -sf http://localhost:6006/v1/traces >/dev/null && echo ok`.

## Configure via the CLI

Backend credentials and per-harness settings live in `~/.arize/harness/config.yaml`. Edit it with `acht config` rather than hand-editing — no bootstrap, api keys masked by default.

```bash
acht config show                                   # whole config (api_key masked)
acht config get harnesses.claude-code.project_name
acht config set harnesses.claude-code.project_name my-project
acht config set verbose true
acht config delete harnesses.claude-code.space_id
acht config edit                                   # open in $EDITOR
acht config show --reveal                          # unmask api keys
```

Schema:

```yaml
harnesses:
  claude-code:
    project_name: claude-code       # Arize/Phoenix project
    target: arize                   # arize | phoenix
    endpoint: otlp.arize.com:443    # OTLP (arize) or Phoenix URL
    api_key: <key>
    space_id: <id>                  # arize only
logging:
  prompts: true                     # log user prompt text
  tool_details: true                # log tool args (commands, paths, URLs)
  tool_content: true                # log tool input/output content
user_id: ""                         # optional, added as user.id
verbose: false                      # terminal trace summaries (ARIZE_VERBOSE env wins)
```

## Claude-specific: where config lives

Claude Code tracing has **two possible install paths**, and where settings live (and how you debug/uninstall) depends on which was used:

- **Installed via `acht` or `install.sh`:** backend credentials are in `~/.arize/harness/config.yaml`; the hook registrations + `ARIZE_TRACE_ENABLED` env live in `~/.claude/settings.json`. Use `acht config` to edit, `acht doctor` to debug, `acht uninstall --claude` to remove.
- **Installed via the Claude Plugin marketplace:** the wizard is skipped. Hooks load from the plugin, and backend credentials must be set directly in `~/.claude/settings.json` under the `env` block (`ARIZE_API_KEY`, `ARIZE_SPACE_ID`, `ARIZE_OTLP_ENDPOINT`, or `PHOENIX_*`). There may be **no** `~/.arize/harness/config.yaml`. To debug, inspect `~/.claude/settings.json`; to remove, `claude plugin uninstall claude-code-tracing@coding-harness-tracing`.

When debugging, **check which install the user has first** (does `~/.arize/harness/config.yaml` exist and contain a `harnesses.claude-code` block?) before assuming where credentials are.

## Diagnose with doctor

```bash
acht doctor
```

Pure-Go health check — works even when the venv is broken. Reads `~/.arize/harness/config.yaml`, the harness settings files, env vars, and probes the OTLP endpoint. Each line is `✓` (pass) or `✗` (fail) with a remediation hint; exits non-zero if anything fails.

| Verdict | Meaning / fix |
|---------|---------------|
| `✗ venv` | Runtime missing/broken → `acht add claude` (or `acht update`) to rebuild |
| `✗ settings:claude_code` | `~/.claude/settings.json` missing or unparseable → re-run `acht add claude`, or fix JSON |
| `✗ env:claude_code` | No `ARIZE_*`/`PHOENIX_*` creds in env or config → `acht config set harnesses.claude-code.api_key ...` (or set the env var for marketplace installs) |
| `✗ otlp_endpoint` | Endpoint unreachable (5xx/timeout) → check network/endpoint; retry |
| all `✓` but no traces | Confirm `ARIZE_TRACE_ENABLED=true` and restart the session; see [Troubleshoot](#troubleshoot) |

## Agent SDK setup

For apps built on the [Claude Agent SDK](https://platform.claude.com/docs/en/agent-sdk/overview). **Provide the snippets for the developer to add to their code — the agent can't wire this up at runtime** since plugin paths/settings must be set before the SDK session starts. The user must use `ClaudeSDKClient`; the standalone `query()` does not support hooks.

1. Pick a backend and ensure `~/.arize/harness/config.yaml` has credentials (run `acht add claude` once, or [Backends](#backends)).
2. Get the plugin path: installed via acht/install.sh → `~/.arize/harness/tracing/claude_code`; via marketplace → check `~/.claude/plugins/installed_plugins.json`; not installed → `git clone https://github.com/Arize-ai/coding-harness-tracing.git` and use `./coding-harness-tracing/tracing/claude_code`.
3. The SDK subprocess doesn't inherit shell env, so pass tracing env via a settings file:

```json
{ "env": { "ARIZE_TRACE_ENABLED": "true" } }
```

4. Wire both into `ClaudeAgentOptions`:

**Python**
```python
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

options = ClaudeAgentOptions(
    plugins=[{"type": "local", "path": "~/.arize/harness/tracing/claude_code"}],
    settings="./settings.local.json",
)
async with ClaudeSDKClient(options=options) as client:
    await client.query("...")
    async for message in client.receive_response():
        print(message)
```

**TypeScript**
```typescript
import { ClaudeSDKClient } from "@anthropic-ai/claude-agent-sdk";

const client = new ClaudeSDKClient({
  plugins: [{ type: "local", path: "~/.arize/harness/tracing/claude_code" }],
  settings: "./settings.local.json",
});
await client.connect();
await client.query("...");
```

`tracing.claude_code.agent_sdk.claude_options()` returns a pre-wired `ClaudeAgentOptions` when the harness is installed via acht/install.sh.

**Notes:** The Python SDK historically omits `SessionStart`/`SessionEnd`/`Notification`/`PermissionRequest`; the plugin lazily initializes session state on first `UserPromptSubmit`, so LLM/tool/subagent spans still work. Validate with `"ARIZE_DRY_RUN": "true"` in the settings file and check `~/.arize/harness/logs/claude-code.log`.

## Uninstall

```bash
acht uninstall --claude     # remove Claude Code tracing, keep the shared runtime
acht uninstall              # remove all harnesses + the shared runtime
```

Marketplace installs: `claude plugin uninstall claude-code-tracing@coding-harness-tracing`.

## Troubleshoot

Run `acht doctor` first. Then:

| Problem | Fix |
|---------|-----|
| Traces not appearing | `ARIZE_TRACE_ENABLED` must be `"true"` in `~/.claude/settings.json`; restart the session |
| Config missing (acht install) | `acht add claude`, or `acht config set harnesses.claude-code.api_key ...` |
| Config missing (marketplace install) | Set `ARIZE_*`/`PHOENIX_*` in `~/.claude/settings.json` `env` block — there is no config.yaml |
| Phoenix unreachable | `curl -sf <endpoint>/v1/traces` |
| No output in terminal | Hook stderr is discarded by Claude Code; read `~/.arize/harness/logs/claude-code.log` |
| Test without sending | `ARIZE_DRY_RUN=true` |
| Verbose logging | `acht config set verbose true` (or `ARIZE_VERBOSE=true`) |
| Wrong project name | `acht config set harnesses.claude-code.project_name <name>` |
| Spans missing user attribution | `acht config set user_id <id>` (or `ARIZE_USER_ID` env) |
