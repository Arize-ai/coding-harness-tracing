---
name: manage-codex-tracing
description: Set up and manage Arize tracing for OpenAI Codex CLI sessions using the acht CLI. Use when users want to set up Codex tracing, configure Arize AX or Phoenix for Codex, edit config, run diagnostics, enable/disable tracing, or troubleshoot Codex tracing. Triggers on "set up codex tracing", "configure Arize for Codex", "configure Phoenix for Codex", "acht", "enable codex tracing", "setup-codex-tracing", or any request about connecting Codex to Arize or Phoenix for observability.
---

# Manage Codex Tracing

Configure OpenInference tracing for the OpenAI Codex CLI to Arize AX (cloud) or Phoenix (self-hosted). Spans are sent directly to the backend from Codex's native lifecycle hooks — no OTEL collector or background process runs in the user's environment.

The primary tool is the **`acht`** CLI. It installs a managed Python runtime (via [uv](https://github.com/astral-sh/uv)), writes the Codex hook config, and manages settings. Reach for the repo only to inspect hook/handler internals: <https://github.com/Arize-ai/coding-harness-tracing> (Codex code under `tracing/codex/`).

## Codex architecture (read this first)

Codex tracing is more involved than the other harnesses. It uses **native Codex CLI hooks** plus the legacy `notify` hook as a token-usage backstop:

- `SessionStart` / `UserPromptSubmit` → `arize-hook-codex-session` (updates per-thread state at `~/.arize/harness/state/codex/state_<thread_id>.yaml`)
- `PreToolUse` / `PostToolUse` / `PermissionRequest` → `arize-hook-codex-tool` (appends rows to `spans_<thread_id>.jsonl`)
- `Stop` → `arize-hook-codex-stop` (builds the parent LLM span + TOOL child spans, sends, clears state)
- `agent-turn-complete` (`notify`) → `arize-hook-codex-notify` (writes token counts into state; Codex hook payloads don't carry exact token usage)

There is **no OTEL exporter / collector** anymore — spans go straight to Phoenix (REST) or Arize AX (gRPC) via `send_span()`.

**Trust prompt:** Codex requires explicit approval before non-managed hooks fire. After install the user must start `codex`, run `/hooks`, and approve each `arize-hook-codex-*` entry. Until then tracing falls back to `notify`-only (a single flat Turn span per turn, no tool spans).

## How to use this skill

1. **Installing / adding tracing?** → [Install](#install)
2. **Have credentials, changing a setting?** → [Configure via the CLI](#configure-via-the-cli)
3. **Not working / debugging?** → [Diagnose with doctor](#diagnose-with-doctor) then [Troubleshoot](#troubleshoot)
4. **No backend account yet?** → [Backends](#backends) first

## Install

```bash
go install github.com/Arize-ai/coding-harness-tracing/cmd/acht@latest
acht add codex
```

`acht add codex` bootstraps the runtime, writes the managed hook block into `~/.codex/config.toml`, and runs the wizard. Fields collected:

| Field | Notes |
|-------|-------|
| Backend | `arize` or `phoenix` |
| API key | `ARIZE_API_KEY` / `PHOENIX_API_KEY` — env var or masked prompt, never a flag |
| Space ID | Arize only |
| OTLP / Phoenix endpoint | Arize defaults to `otlp.arize.com:443`; Phoenix to `http://localhost:6006` |
| Project name | Defaults to `codex` |
| User ID | Optional; added to every span as `user.id` |
| Content logging | Three prompts (prompts / tool details / tool content), default **on** |
| Verbose | Terminal trace summaries, default **off** |

**After install, remind the user to trust the hooks**: start `codex`, run `/hooks`, approve the `arize-hook-codex-*` entries.

**Non-interactive:**

```bash
export ARIZE_API_KEY=...
acht add codex --backend arize --space-id SPACE_ID --project-name codex --non-interactive
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

Backend credentials live in `~/.arize/harness/config.yaml`. The Codex hook wiring lives in `~/.codex/config.toml` (managed block, written by `acht add codex` — don't hand-edit it). Edit backend settings with `acht config`:

```bash
acht config show                              # api_key masked
acht config set harnesses.codex.project_name codex
acht config set verbose true
acht config edit
```

Schema:

```yaml
harnesses:
  codex:
    project_name: codex
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
| `✗ venv` | Runtime missing/broken → `acht add codex` or `acht update` |
| `✗ settings:codex` | `~/.codex/config.toml` missing → re-run `acht add codex` (doctor checks existence; TOML isn't parsed at v1) |
| `✗ env:codex` | No creds in env or config → `acht config set harnesses.codex.api_key ...` |
| `✗ otlp_endpoint` | Endpoint unreachable → check network/endpoint |
| all `✓` but only single flat spans (no tool spans) | Hooks not trusted yet → run `codex`, `/hooks`, approve `arize-hook-codex-*` |

## Uninstall

```bash
acht uninstall --codex     # remove Codex tracing, keep the shared runtime
acht uninstall             # remove all harnesses + the shared runtime
```

Removes the managed block from `~/.codex/config.toml` and the `harnesses.codex` config entry; clears per-thread state under `~/.arize/harness/state/codex/`.

## Troubleshoot

Run `acht doctor` first. Then:

| Problem | Fix |
|---------|-----|
| Only one flat span per turn, no tool spans | Hooks not trusted → `codex` → `/hooks` → approve `arize-hook-codex-*` |
| No traces at all | Verify creds (`acht config show`); confirm the managed block is in `~/.codex/config.toml` |
| Phoenix unreachable | `curl -sf <endpoint>/v1/traces` |
| Missing token counts | Expected if `notify` didn't fire; native hooks still produce the span tree |
| Test without sending | `ARIZE_DRY_RUN=true` |
| Verbose logging | `acht config set verbose true` (or `ARIZE_VERBOSE=true`); errors always go to `~/.arize/harness/logs/codex.log` |
| Wrong project name | `acht config set harnesses.codex.project_name <name>` |
