---
name: manage-codex-tracing
description: Set up and configure Arize tracing for OpenAI Codex CLI sessions. Use when users want to set up Codex tracing, configure Arize AX or Phoenix for Codex, enable/disable tracing, or troubleshoot Codex tracing issues. Triggers on "set up codex tracing", "configure Arize for Codex", "configure Phoenix for Codex", "enable codex tracing", "setup-codex-tracing", or any request about connecting Codex to Arize or Phoenix for observability.
---

# Setup Codex Tracing

Configure OpenInference tracing for OpenAI Codex CLI sessions to Arize AX (cloud) or Phoenix (self-hosted).

## Architecture Overview

Codex tracing uses real Codex CLI lifecycle hooks plus the legacy `notify` hook as a token-usage backstop. Spans are sent directly to the backend from the `Stop` hook:

1. **Direct send** (`core/common.py`) — spans are sent directly to Phoenix (REST) or Arize AX (gRPC) from the hook handlers via `send_span()`. Per-harness backend credentials are read from `harnesses.codex.*` in config.

2. **Real Codex hooks** — `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PermissionRequest`, and `Stop` events are dispatched to three entry points:
   - `arize-hook-codex-session` (`SessionStart`, `UserPromptSubmit`) — mutates per-thread state file at `~/.arize/harness/state/codex/state_<thread_id>.yaml`.
   - `arize-hook-codex-tool` (`PreToolUse`, `PostToolUse`, `PermissionRequest`) — appends rows to `~/.arize/harness/state/codex/spans_<thread_id>.jsonl`.
   - `arize-hook-codex-stop` (`Stop`) — reads state + JSONL, builds the parent LLM span plus TOOL child spans, sends the multi-span payload, then clears turn-scoped state and deletes the JSONL.

3. **Notify hook** (`arize-hook-codex-notify`) — Fires on `agent-turn-complete`. Real Codex hook payloads don't carry exact token counts, so `notify` is kept purely as a token-usage backstop: it extracts `token_usage` and `last-assistant-message` from the notify payload and writes them into the state file. If the state file doesn't exist (hooks not yet trusted, first run), notify falls back to its legacy behavior of emitting a single flat Turn span.

```
Codex CLI
  |
  |-- SessionStart / UserPromptSubmit --> arize-hook-codex-session
  |     |--> updates state_<thread_id>.yaml
  |
  |-- PreToolUse / PostToolUse / PermissionRequest --> arize-hook-codex-tool
  |     |--> appends row to spans_<thread_id>.jsonl
  |
  |-- agent-turn-complete (notify) --> arize-hook-codex-notify
  |     |--> writes token_usage into state_<thread_id>.yaml
  |
  |-- Stop --> arize-hook-codex-stop
        |--> reads state + spans JSONL
        |--> builds parent LLM span + TOOL child spans
        |--> send_span() --> Phoenix/Arize AX
        |--> clears turn-scoped state, deletes spans JSONL
```

**Graceful degradation**: If hooks aren't trusted yet (Codex requires explicit `/hooks` approval), the `notify` hook still produces a single flat Turn span.

## Trust prompt

Codex requires explicit user trust for non-managed hooks before they fire. After install, the user must:

1. Start a Codex session: `codex`
2. Type `/hooks` and approve each `arize-hook-codex-*` entry.

Without this one-time approval, the real hooks never fire and tracing falls back to `notify`-only (single LLM span per turn, no tool spans).

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
1. **Backend config** at `~/.arize/harness/config.yaml`
2. **Environment variables** in `~/.codex/arize-env.sh`
3. **Hook entries** in `~/.codex/config.toml` (real Codex hooks + `notify` token-usage backstop)
4. **Trust prompt** inside Codex (`/hooks`) — required before non-managed hooks fire

### Determine the integration path

Ask the user: **"Where is the Codex tracing directory located?"**

Common locations:
- If cloned: `./coding-harness-tracing/tracing/codex`
- If installed via the curl installer: `~/.arize/harness/tracing/codex`

Store this as `INTEGRATION_PATH` for the hook config.

### Step 1: Write the backend config

Write `~/.arize/harness/config.yaml` with the backend credentials. The config file is the single source of truth for backend and harness settings.

**Important: read-merge-write.** If `~/.arize/harness/config.yaml` already exists, read it first, add or update the `harnesses.codex` entry, and preserve existing backend credentials. Only prompt the user for backend credentials if there is no existing config.

**Phoenix:**
```bash
mkdir -p ~/.arize/harness/{logs,state/codex}
# Merge: add/update harnesses.codex, preserve existing backend settings
arize-config set harnesses.codex.project_name codex
```

If no config exists yet, create it:
```yaml
harnesses:
  codex:
    project_name: codex
    target: phoenix
    endpoint: http://localhost:6006
    api_key: ""
```

**Arize AX:**
```bash
mkdir -p ~/.arize/harness/{logs,state/codex}
arize-config set harnesses.codex.project_name codex
```

If no config exists yet, create it:
```yaml
harnesses:
  codex:
    project_name: codex
    target: arize
    endpoint: otlp.arize.com:443
    api_key: <key>
    space_id: <space-id>
```

### Step 2: Write the environment file (optional)

Environment variables are optional overrides — all backend credentials are in `~/.arize/harness/config.yaml`. If the user needs env-var overrides, create `~/.codex/arize-env.sh`:

```bash
cat > ~/.codex/arize-env.sh << 'EOF'
export ARIZE_TRACE_ENABLED=true
EOF
chmod 600 ~/.codex/arize-env.sh
```

If the user wants to associate spans with a user ID, add `export ARIZE_USER_ID="<user-id>"`.

### Step 3: Add the hook entries to config.toml

Read `~/.codex/config.toml`. Add the `notify` line at the top level (NOT inside any `[section]`) — this is the token-usage backstop:

```toml
notify = ["~/.arize/harness/venv/bin/arize-hook-codex-notify"]
```

Then add one `[[hooks.<Event>]]` entry per real Codex lifecycle event:

```toml
[[hooks.SessionStart]]
command = ["~/.arize/harness/venv/bin/arize-hook-codex-session"]

[[hooks.UserPromptSubmit]]
command = ["~/.arize/harness/venv/bin/arize-hook-codex-session"]

[[hooks.PreToolUse]]
command = ["~/.arize/harness/venv/bin/arize-hook-codex-tool"]

[[hooks.PostToolUse]]
command = ["~/.arize/harness/venv/bin/arize-hook-codex-tool"]

[[hooks.PermissionRequest]]
command = ["~/.arize/harness/venv/bin/arize-hook-codex-tool"]

[[hooks.Stop]]
command = ["~/.arize/harness/venv/bin/arize-hook-codex-stop"]
```

**Important:** If `notify` already exists in the config, update the existing line. If `[[hooks.<Event>]]` entries already exist that match our handlers (managed block), leave them alone — the installer manages the block idempotently.

### Step 4: Approve the hooks inside Codex

Codex requires explicit user trust for non-managed hooks before they fire. The user must:

1. Start a Codex session: `codex`
2. Type `/hooks` and approve each `arize-hook-codex-*` entry.

Until this is done, only the `notify` token-usage backstop will run (flat single-span tracing). The real hooks will not fire.

**Note:** The installer handles Steps 1-3 automatically. Step 4 (the `/hooks` trust prompt) is a one-time user action — the installer cannot automate it.

### Validate

After writing the config, validate:

1. **Check config.toml is valid:**
```bash
cat ~/.codex/config.toml
```
Visually confirm the `notify` line is at the top level and the `[[hooks.SessionStart]]`, `[[hooks.UserPromptSubmit]]`, `[[hooks.PreToolUse]]`, `[[hooks.PostToolUse]]`, `[[hooks.PermissionRequest]]`, and `[[hooks.Stop]]` entries are present.

2. **Check env file:**
```bash
source ~/.codex/arize-env.sh && echo "ARIZE_TRACE_ENABLED=$ARIZE_TRACE_ENABLED"
```

3. **Check the hooks are trusted inside Codex:**
   Start `codex`, run `/hooks`, and confirm each `arize-hook-codex-*` entry is listed and approved.

4. **Phoenix connectivity** (if using Phoenix):
```bash
curl -sf ${PHOENIX_ENDPOINT}/v1/traces >/dev/null && echo "Phoenix reachable" || echo "Phoenix not reachable"
```

5. **Dry run test:**
```bash
ARIZE_DRY_RUN=true arize-hook-codex-notify '{"type":"agent-turn-complete","thread-id":"test-123","turn-id":"turn-1","cwd":"/tmp","input-messages":"hello","last-assistant-message":"hi there"}'
```
Should print: `[arize] DRY RUN:` followed by the span name.

### Confirm

Tell the user:
- Configuration saved to `~/.codex/config.toml`, `~/.codex/arize-env.sh`, and `~/.arize/harness/config.yaml`
- Spans are sent directly to the backend from the `Stop` hook
- The real Codex hooks (`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PermissionRequest`, `Stop`) build rich span trees with TOOL children per tool call
- The `notify` hook is now a token-usage backstop — it writes exact token counts into the per-thread state file so they appear on the parent LLM span
- Until the user approves the hooks via `/hooks` inside Codex, only the `notify`-based fallback runs (single flat LLM span per turn)
- Mention `ARIZE_DRY_RUN=true` to test without sending data
- Mention `ARIZE_VERBOSE=true` and `ARIZE_TRACE_DEBUG=true` for debug output
- Logs: `~/.arize/harness/logs/codex.log` (errors always; routine activity requires `ARIZE_VERBOSE=true` in `~/.codex/arize-env.sh` or the shell)

### Environment Variables Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ARIZE_API_KEY` | For AX | - | Arize AX API key |
| `ARIZE_SPACE_ID` | For AX | - | Arize AX space ID |
| `ARIZE_OTLP_ENDPOINT` | No | `otlp.arize.com:443` | OTLP gRPC endpoint (on-prem Arize) |
| `PHOENIX_ENDPOINT` | For Phoenix | `http://localhost:6006` | Phoenix collector URL |
| `PHOENIX_API_KEY` | No | - | Phoenix API key for auth |
| `ARIZE_PROJECT_NAME` | No | `codex` | Project name in Arize/Phoenix |
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
| Hooks not firing | Run `codex` → `/hooks` and confirm each `arize-hook-codex-*` entry is trusted. If they aren't listed at all, re-run the installer. |
| `notify` hook not firing | Verify `notify` line in `~/.codex/config.toml` points to correct path |
| Phoenix unreachable | Verify Phoenix is running: `curl -sf <endpoint>/v1/traces` |
| No output in terminal | Hooks run in background; check `~/.arize/harness/logs/codex.log` |
| Want to test without sending | Set `ARIZE_DRY_RUN=true` in env or `export ARIZE_DRY_RUN=true` |
| Want verbose logging | Set `ARIZE_VERBOSE=true` in env or `export ARIZE_VERBOSE=true` |
| Wrong project name | Set `ARIZE_PROJECT_NAME` in `~/.codex/arize-env.sh` (default: `codex`) |
| Existing `notify` hook | Codex supports only one `notify` — create a wrapper script that calls both |
| Stale state files | Run: `rm -rf ~/.arize/harness/state/codex/state_*.yaml ~/.arize/harness/state/codex/spans_*.jsonl` |
| Flat spans only (no children) | The real hooks haven't been trusted yet. Run `codex` → `/hooks` and approve each `arize-hook-codex-*` entry. |
| User ID not appearing on spans | Set `ARIZE_USER_ID` in `~/.codex/arize-env.sh` or export before running Codex |
