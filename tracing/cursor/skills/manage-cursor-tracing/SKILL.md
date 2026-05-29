---
name: manage-cursor-tracing
description: Set up and manage Arize tracing for Cursor IDE / CLI sessions using the acht CLI. Use when users want to set up Cursor tracing, configure Arize AX or Phoenix for Cursor, edit config, run diagnostics, enable/disable tracing, or troubleshoot Cursor tracing. Triggers on "set up cursor tracing", "configure Arize for Cursor", "acht", "enable cursor tracing", "setup-cursor-tracing", or any request about connecting Cursor to Arize or Phoenix for observability.
---

# Manage Cursor Tracing

Configure OpenInference tracing for Cursor (IDE and CLI) to Arize AX (cloud) or Phoenix (self-hosted). Spans are sent directly to the backend from hooks — no background process or backend-specific Python deps run in the user's environment.

The primary tool is the **`acht`** CLI. It installs a managed Python runtime (via [uv](https://github.com/astral-sh/uv)), registers the Cursor hooks, and manages config. Reach for the repo only to inspect hook/handler internals: <https://github.com/Arize-ai/coding-harness-tracing> (Cursor code under `tracing/cursor/`).

## How to use this skill

1. **Installing / adding tracing?** → [Install](#install)
2. **Have credentials, changing a setting?** → [Configure via the CLI](#configure-via-the-cli)
3. **Not working / debugging?** → [Diagnose with doctor](#diagnose-with-doctor) then [Troubleshoot](#troubleshoot)
4. **No backend account yet?** → [Backends](#backends) first

## Install

```bash
go install github.com/Arize-ai/coding-harness-tracing/cmd/acht@latest
acht add cursor
```

`acht add cursor` bootstraps the runtime, registers hooks in `~/.cursor/hooks.json`, and runs the wizard. Fields collected:

| Field | Notes |
|-------|-------|
| Backend | `arize` or `phoenix` |
| API key | `ARIZE_API_KEY` / `PHOENIX_API_KEY` — env var or masked prompt, never a flag |
| Space ID | Arize only |
| OTLP / Phoenix endpoint | Arize defaults to `otlp.arize.com:443`; Phoenix to `http://localhost:6006` |
| Project name | Defaults to `cursor` |
| User ID | Optional; added to every span as `user.id` |
| Content logging | Three prompts (prompts / tool details / tool content), default **on** |
| Verbose | Terminal trace summaries, default **off** |

A single `arize-hook-cursor` entry point handles all Cursor events (IDE + CLI), dispatching on the event name in the payload. Restart Cursor after install.

**Non-interactive:**

```bash
export ARIZE_API_KEY=...
acht add cursor --backend arize --space-id SPACE_ID --project-name cursor --non-interactive
```

## Backends

### Arize AX (cloud)

SaaS uses `otlp.arize.com:443`; on-prem needs a custom OTLP endpoint. Get credentials: log in (https://app.arize.com), **Settings** → **Space ID** on Space Settings; **API Keys** tab to create/copy a key. Both `api_key` and `space_id` required.

### Phoenix (self-hosted)

```bash
pip install arize-phoenix && phoenix serve   # or: docker run -p 6006:6006 arizephoenix/phoenix:latest
```

UI at `http://localhost:6006`. Verify: `curl -sf http://localhost:6006/v1/traces >/dev/null && echo ok`.

## Configure via the CLI

Backend credentials live in `~/.arize/harness/config.yaml`. The hook wiring lives in `~/.cursor/hooks.json` (written by `acht add cursor`). Edit backend settings with `acht config`:

```bash
acht config show                              # api_key masked
acht config set harnesses.cursor.project_name cursor
acht config set verbose true
acht config edit
```

Schema:

```yaml
harnesses:
  cursor:
    project_name: cursor
    target: arize                   # arize | phoenix
    endpoint: otlp.arize.com:443    # OTLP (arize) or Phoenix URL
    api_key: <key>
    space_id: <id>                  # arize only
logging:
  prompts: true
  tool_details: true
  tool_content: true
user_id: ""
verbose: false                      # ARIZE_VERBOSE env wins over this
```

## Diagnose with doctor

```bash
acht doctor
```

Pure-Go health check (works even when the venv is broken). `✓`/`✗` per check with remediation; non-zero exit on failure.

| Verdict | Meaning / fix |
|---------|---------------|
| `✗ venv` | Runtime missing/broken → `acht add cursor` or `acht update` |
| `✗ settings:cursor` | `~/.cursor/hooks.json` missing or unparseable → re-run `acht add cursor`, or fix JSON |
| `✗ env:cursor` | No creds in env or config → `acht config set harnesses.cursor.api_key ...` |
| `✗ otlp_endpoint` | Endpoint unreachable → check network/endpoint |
| all `✓` but no traces | Restart Cursor; see [Troubleshoot](#troubleshoot) |

## Uninstall

```bash
acht uninstall --cursor   # remove Cursor tracing, keep the shared runtime
acht uninstall            # remove all harnesses + the shared runtime
```

Removes the Arize hook entries from `~/.cursor/hooks.json` and the `harnesses.cursor` config entry.

## Troubleshoot

Run `acht doctor` first. Then:

| Problem | Fix |
|---------|-----|
| No traces | Verify `~/.cursor/hooks.json` has `arize-hook-cursor` entries; restart Cursor |
| Phoenix unreachable | `curl -sf <endpoint>/v1/traces` |
| Test without sending | `ARIZE_DRY_RUN=true` before launching Cursor |
| Verbose logging | `acht config set verbose true` (or `ARIZE_VERBOSE=true`); errors always go to `~/.arize/harness/logs/cursor.log` |
| Wrong project name | `acht config set harnesses.cursor.project_name <name>` |
| Spans missing user attribution | `acht config set user_id <id>` (or `ARIZE_USER_ID` env) |
