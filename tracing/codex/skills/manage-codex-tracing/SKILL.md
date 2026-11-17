---
name: manage-codex-tracing
description: Set up and configure Arize tracing for OpenAI Codex CLI sessions. Use when users want to set up Codex tracing, configure Arize AX or Phoenix for Codex, enable/disable tracing, or troubleshoot Codex tracing issues. Triggers on "set up codex tracing", "configure Arize for Codex", "configure Phoenix for Codex", "enable codex tracing", "setup-codex-tracing", or any request about connecting Codex to Arize or Phoenix for observability.
---

# Setup Codex Tracing

Configure OpenInference tracing for OpenAI Codex CLI sessions to Arize AX (cloud) or Phoenix (self-hosted).

## Architecture Overview

Codex tracing uses one entry point, `arize-hook-codex-notify`, registered as Codex's `notify` command. There are no Codex lifecycle hooks to register or approve. It works the same for the Codex CLI and the Codex SDK, which runs the same Codex runtime.

1. **Notify hook** — Codex runs it on every `agent-turn-complete` event, appending the event JSON as the last argument. The hook finds the session transcript Codex saved under `~/.codex/sessions/` (`rollout-*-<thread_id>.jsonl`) and reads that turn's prompt, final answer, model, token counts, and tool calls from it.
2. **Direct send** (`core/common.py`) — the hook builds one LLM span for the turn with a TOOL child span per tool call, and sends them straight to Phoenix (REST) or Arize AX (gRPC) via `send_span()`. Per-harness backend credentials are read from `harnesses.codex.*` in config.
3. **Fallback** — if the transcript or the turn can't be found, the hook sends a single flat Turn span built from the notify payload alone.

```
Codex CLI / Codex SDK
  |
  |-- agent-turn-complete (notify) --> arize-hook-codex-notify
        |--> forwards the event to harnesses.codex.previous_notify, if set
        |--> reads the turn from ~/.codex/sessions/.../rollout-*-<thread_id>.jsonl
        |--> builds parent LLM span + TOOL child spans
        |--> send_span() --> Phoenix/Arize AX
```

**One notify command.** Codex runs a single `notify` command: `notify[0]` is the program and the rest of the array is its arguments. It is not a list of hooks. If the user already had a `notify` command, the installer saves it as `harnesses.codex.previous_notify` in `~/.arize/harness/config.json`, the hook forwards every event to it, and uninstall restores it. The forwarded command runs with `ARIZE_CODEX_NOTIFY_FORWARDED=1` set, and the hook does nothing when it sees that, so a saved wrapper that also calls the hook can't loop.

## How to Use This Skill

**This skill follows a decision tree workflow.** Start by asking the user where they are in the setup process:

1. **Do they already have credentials?**
   - Yes → Jump to [Configure Codex](#configure-codex)
   - No → Continue to step 2

2. **Which backend do they want to use?**
   - Phoenix (self-hosted) → Go to [Set Up Phoenix](#set-up-phoenix)
   - Arize AX (cloud) → Go to [Set Up Arize AX](#set-up-arize-ax)

3. **Are they troubleshooting?**
   - Yes → Jump to [Troubleshoot](#troubleshoot)

**Important:** Only follow the relevant path for the user's needs. Don't go through all sections.

## Set Up Phoenix

Phoenix is self-hosted and requires no Python dependencies for tracing (spans are sent directly via `send_span()` using stdlib `urllib`).

### Install Phoenix

Ask if they already have Phoenix running. If not, walk through:

```bash
# Option A: pip
pip install arize-phoenix && phoenix serve

# Option B: Docker
docker run -p 6006:6006 arizephoenix/phoenix:latest
```

Phoenix UI will be available at `http://localhost:6006`. Confirm it's running:

```bash
curl -sf http://localhost:6006/v1/traces >/dev/null && echo "Phoenix is running" || echo "Phoenix not reachable"
```

Then proceed to [Configure Codex](#configure-codex) with `PHOENIX_ENDPOINT=http://localhost:6006`.

## Set Up Arize AX

Arize AX is available as a SaaS platform or as an on-prem deployment. Users need an account, a space, and an API key.

**First, ask the user: "Are you using the Arize SaaS platform or an on-prem instance?"**

- **SaaS** → Uses the default endpoint (`otlp.arize.com:443`). Continue below.
- **On-prem** → The user will need to provide their custom OTLP endpoint (e.g., `otlp.mycompany.arize.com:443`). Ask for it and note it for the configure step where it will be set as `ARIZE_OTLP_ENDPOINT`.

### 1. Create an account

If the user doesn't have an Arize account:
- **SaaS**: Sign up at https://app.arize.com/auth/join
- **On-prem**: Contact their administrator for access

### 2. Get Space ID and API key

Walk the user through finding their credentials:
1. Log in to their Arize instance (https://app.arize.com for SaaS, or their on-prem URL)
2. Click **Settings** (gear icon) in the left sidebar
3. The **Space ID** is shown on the Space Settings page
4. Go to the **API Keys** tab
5. Click **Create API Key** or copy an existing one

Both `ARIZE_API_KEY` and `ARIZE_SPACE_ID` are required.

### 3. Python dependencies (bundled with the package)

Arize AX uses gRPC for export, but the gRPC dependencies are bundled with the package — they are **not** required in the user's Python environment.  No `pip install` step is needed for basic tracing.

Then proceed to [Configure Codex](#configure-codex).

## Configure Codex

This section configures:
1. **Backend config** at `~/.arize/harness/config.json`
2. **Environment variables** in `~/.codex/arize-env.sh`
3. **The `notify` command** in `~/.codex/config.toml`

### Determine the integration path

Ask the user: **"Where is the Codex tracing directory located?"**

Common locations:
- If cloned: `./coding-harness-tracing/tracing/codex`
- If installed via the curl installer: `~/.arize/harness/tracing/codex`

Store this as `INTEGRATION_PATH` for the hook config.

### Step 1: Write the backend config

Write `~/.arize/harness/config.json` with the backend credentials. The config file is the single source of truth for backend and harness settings.

**Important: read-merge-write.** If `~/.arize/harness/config.json` already exists, read it first, add or update the `harnesses.codex` entry, and preserve existing backend credentials. Only prompt the user for backend credentials if there is no existing config.

**Phoenix:**
```bash
mkdir -p ~/.arize/harness/{logs,state/codex}
# Merge: add/update harnesses.codex, preserve existing backend settings
arize-config set harnesses.codex.project_name codex
```

If no config exists yet, create it:
```json
{
  "harnesses": {
    "codex": {
      "project_name": "codex",
      "target": "phoenix",
      "endpoint": "http://localhost:6006",
      "api_key": ""
    }
  }
}
```

**Arize AX:**
```bash
mkdir -p ~/.arize/harness/{logs,state/codex}
arize-config set harnesses.codex.project_name codex
```

If no config exists yet, create it:
```json
{
  "harnesses": {
    "codex": {
      "project_name": "codex",
      "target": "arize",
      "endpoint": "otlp.arize.com:443",
      "api_key": "<key>",
      "space_id": "<space-id>"
    }
  }
}
```

### Step 2: Write the environment file (optional)

Environment variables are optional overrides — all backend credentials are in `~/.arize/harness/config.json`. If the user needs env-var overrides, create `~/.codex/arize-env.sh`:

```bash
cat > ~/.codex/arize-env.sh << 'EOF'
export ARIZE_TRACE_ENABLED=true
EOF
chmod 600 ~/.codex/arize-env.sh
```

If the user wants to associate spans with a user ID, add `export ARIZE_USER_ID="<user-id>"`.

### Step 3: Set `notify` in config.toml

Read `~/.codex/config.toml`. Set `notify` at the top level (NOT inside any `[section]`) to exactly one element, the absolute path of the hook:

```toml
notify = ["/Users/<you>/.arize/harness/venv/bin/arize-hook-codex-notify"]
```

**Important:** `notify` is one command line, not a list of hooks. Never append our path to an existing `notify` array: Codex would run only the first element and pass ours to it as an argument. If `notify` already holds another command, save that array, as a JSON list, under `harnesses.codex.previous_notify` in `~/.arize/harness/config.json` before replacing it (read-merge-write the JSON; `arize-config set` would store it as a string). For example, `"previous_notify": ["python3", "/path/to/their-notify.py"]`. The hook forwards every event to it. If the existing first element is an `arize-hook-codex-notify` path from an older install, just replace it.

**Note:** The installer handles Steps 1-3 automatically, including saving an existing `notify` command.

### Validate

After writing the config, validate:

1. **Check config.toml is valid:**
```bash
cat ~/.codex/config.toml
```
Visually confirm the `notify` line is at the top level, has exactly one element, and that path exists.

2. **Check env file:**
```bash
source ~/.codex/arize-env.sh && echo "ARIZE_TRACE_ENABLED=$ARIZE_TRACE_ENABLED"
```

3. **Phoenix connectivity** (if using Phoenix):
```bash
curl -sf ${PHOENIX_ENDPOINT}/v1/traces >/dev/null && echo "Phoenix reachable" || echo "Phoenix not reachable"
```

4. **Dry run test:**
```bash
ARIZE_DRY_RUN=true arize-hook-codex-notify '{"type":"agent-turn-complete","thread-id":"test-123","turn-id":"turn-1","cwd":"/tmp","input-messages":"hello","last-assistant-message":"hi there"}'
```
Should print: `[arize] DRY RUN:` followed by the span name.

### Confirm

Tell the user:
- Configuration saved to `~/.codex/config.toml`, `~/.codex/arize-env.sh`, and `~/.arize/harness/config.json`
- Each completed turn is sent as one LLM span with a TOOL child span per tool call, built from the Codex session transcript
- There is nothing to approve inside Codex
- If they had their own `notify` command, it still receives every event
- Mention `ARIZE_DRY_RUN=true` to test without sending data
- Mention `ARIZE_VERBOSE=true` and `ARIZE_TRACE_DEBUG=true` for debug output
- Logs: `~/.arize/harness/logs/codex.log` (errors always; routine activity requires `ARIZE_VERBOSE=true` in `~/.codex/arize-env.sh` or the shell)

### Environment Variables Reference

At runtime, project variables override `harnesses.codex.project_name` in `config.json`. During installation, project overrides come only from the file named by `ARIZE_ENV_FILE`. The installer ignores inherited process project variables. Reinstalling preserves the saved name unless a file setting or typed answer replaces it.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ARIZE_API_KEY` | For AX | - | Arize AX API key |
| `ARIZE_SPACE_ID` | For AX | - | Arize AX space ID |
| `ARIZE_OTLP_ENDPOINT` | No | `otlp.arize.com:443` | OTLP gRPC endpoint (on-prem Arize) |
| `PHOENIX_ENDPOINT` | For Phoenix | `http://localhost:6006` | Phoenix collector URL |
| `PHOENIX_API_KEY` | No | - | Phoenix API key for auth |
| `ARIZE_PROJECT_NAME` | No | Saved project | Arize AX runtime override. New installs default to `harness/<email>`, or `codex` if no email is available. |
| `PHOENIX_PROJECT`, `PHOENIX_PROJECT_NAME` | No | Saved project | Phoenix runtime override. New installs default to `codex`. `PHOENIX_PROJECT` wins when both are set. |
| `ARIZE_USER_ID` | No | - | User ID to attach to all spans as `user.id` attribute |
| `ARIZE_TRACE_ENABLED` | No | `true` | Enable/disable tracing |
| `ARIZE_DRY_RUN` | No | `false` | Print spans instead of sending |
| `ARIZE_VERBOSE` | No | `false` | Enable verbose logging |
| `ARIZE_TRACE_DEBUG` | No | `false` | Write debug JSON to `~/.arize/harness/state/codex/debug/` |
| `ARIZE_LOG_FILE` | No | `~/.arize/harness/logs/codex.log` | Log file path |

## Troubleshoot

Common issues and fixes:

| Problem | Fix |
|---------|-----|
| Traces not appearing | Check `ARIZE_TRACE_ENABLED` is `true` in `~/.codex/arize-env.sh` |
| `notify` hook not firing | Verify `notify` in `~/.codex/config.toml` has exactly one element and that path exists. Codex runs only the first element, so a stale path or another program first means our hook never runs. Re-run the installer. |
| Wrong project when Codex runs inside another tool | The hook inherits the parent process's environment. A traced session of another harness (for example, a Codex SDK script run from Claude Code) can pass down its `ARIZE_PROJECT_NAME`, which overrides the Codex project. Unset it before starting Codex. An inherited `ARIZE_TRACE_ENABLED` doesn't matter, because the hook reloads `~/.codex/arize-env.sh`. |
| Phoenix unreachable | Verify Phoenix is running: `curl -sf <endpoint>/v1/traces` |
| No output in terminal | Hooks run in background; check `~/.arize/harness/logs/codex.log` |
| Want to test without sending | Set `ARIZE_DRY_RUN=true` in env or `export ARIZE_DRY_RUN=true` |
| Want verbose logging | Set `ARIZE_VERBOSE=true` in env or `export ARIZE_VERBOSE=true` |
| Wrong project name | Check `harnesses.codex.project_name` in `~/.arize/harness/config.json`. For a runtime override, set `ARIZE_PROJECT_NAME` for AX or `PHOENIX_PROJECT` for Phoenix in `~/.codex/arize-env.sh`. |
| Existing `notify` hook | Codex supports only one `notify`. The installer saves the existing command as `harnesses.codex.previous_notify` and the hook forwards every event to it; uninstall restores it. |
| Flat spans only (no children) | The hook couldn't find the turn in the session transcript (`codex.notify_fallback` is set on the span). Check `~/.codex/sessions/` holds a `rollout-*-<thread_id>.jsonl` for the thread. |
| User ID not appearing on spans | Set `ARIZE_USER_ID` in `~/.codex/arize-env.sh` or export before running Codex |
