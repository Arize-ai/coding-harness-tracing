---
name: manage-copilot-tracing
description: Set up and manage Arize tracing for GitHub Copilot sessions using the acht CLI. Use when users want to set up Copilot tracing, configure Arize AX or Phoenix for Copilot, edit config, run diagnostics, enable/disable tracing, or troubleshoot Copilot tracing. Triggers on "set up copilot tracing", "configure Arize for Copilot", "acht", "enable copilot tracing", "setup-copilot-tracing", or any request about connecting GitHub Copilot to Arize or Phoenix for observability.
---

# Manage Copilot Tracing

Configure OpenInference tracing for **GitHub Copilot** (VS Code Copilot Chat) to Arize AX (cloud) or Phoenix (self-hosted). Spans are sent directly to the backend from hooks — no background process or backend-specific Python deps run in the user's environment.

The primary tool is the **`acht`** CLI. It installs a managed Python runtime (via [uv](https://github.com/astral-sh/uv)), writes the Copilot hooks, and manages config. Reach for the repo only to inspect hook/handler internals: <https://github.com/Arize-ai/coding-harness-tracing> (Copilot code under `tracing/copilot/`).

## Copilot-specific: hooks are project-local

Copilot reads hooks from **`.github/hooks/hooks.json` in the repository root** — there is no global Copilot hook file. That means:

- `acht add copilot` must be run **once per repository** you want traced. It writes `.github/hooks/hooks.json` into the current working directory's repo.
- The backend config (`~/.arize/harness/config.yaml`) **is** global and shared — credentials are entered once; only the per-repo hooks file must be re-created in each project.
- When debugging "no traces," first confirm `.github/hooks/hooks.json` exists in *this* repo.

## How to use this skill

1. **Installing / adding tracing to a repo?** → [Install](#install)
2. **Have credentials, changing a setting?** → [Configure via the CLI](#configure-via-the-cli)
3. **Not working / debugging?** → [Diagnose with doctor](#diagnose-with-doctor) then [Troubleshoot](#troubleshoot)
4. **No backend account yet?** → [Backends](#backends) first

## Install

```bash
go install github.com/Arize-ai/coding-harness-tracing/cmd/acht@latest

# run from inside the repo you want traced — re-run in each repo
cd /path/to/your/repo
acht add copilot
```

`acht add copilot` bootstraps the runtime, writes `.github/hooks/hooks.json` into the current repo, and runs the wizard. Fields collected:

| Field | Notes |
|-------|-------|
| Backend | `arize` or `phoenix` |
| API key | `ARIZE_API_KEY` / `PHOENIX_API_KEY` — env var or masked prompt, never a flag |
| Space ID | Arize only |
| OTLP / Phoenix endpoint | Arize defaults to `otlp.arize.com:443`; Phoenix to `http://localhost:6006` |
| Project name | Defaults to `copilot` |
| User ID | Optional; added to every span as `user.id` |
| Content logging | Three prompts (prompts / tool details / tool content), default **on** |
| Verbose | Terminal trace summaries, default **off** |

**Non-interactive:**

```bash
export ARIZE_API_KEY=...
acht add copilot --backend arize --space-id SPACE_ID --project-name copilot --non-interactive
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

Backend credentials live in `~/.arize/harness/config.yaml` (global). The per-repo hooks live in `.github/hooks/hooks.json` (written by `acht add copilot`). Edit backend settings with `acht config`:

```bash
acht config show                              # api_key masked
acht config set harnesses.copilot.project_name copilot
acht config set verbose true
acht config edit
```

Schema:

```yaml
harnesses:
  copilot:
    project_name: copilot
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
| `✗ venv` | Runtime missing/broken → `acht add copilot` or `acht update` |
| `settings:copilot` (always passes/skips) | Copilot has no *global* settings file, so doctor can't verify the per-repo `.github/hooks/hooks.json`. Check it manually: `cat .github/hooks/hooks.json` in the repo |
| `✗ env:copilot` | No creds in env or config → `acht config set harnesses.copilot.api_key ...` |
| `✗ otlp_endpoint` | Endpoint unreachable → check network/endpoint |
| all `✓` but no traces | Confirm `.github/hooks/hooks.json` exists in the repo you're working in |

## Uninstall

```bash
acht uninstall --copilot   # remove Copilot tracing, keep the shared runtime
acht uninstall             # remove all harnesses + the shared runtime
```

Note: uninstall removes the global config entry; the per-repo `.github/hooks/hooks.json` files are removed for the current repo — delete any others by hand if needed.

## Troubleshoot

Run `acht doctor` first. Then:

| Problem | Fix |
|---------|-----|
| No traces in a repo | Confirm `.github/hooks/hooks.json` exists in *that* repo; re-run `acht add copilot` there |
| Hooks not firing | Each `command` in `.github/hooks/hooks.json` must be the absolute venv path `~/.arize/harness/venv/bin/arize-hook-copilot-*` |
| `PreToolUse` blocking tools | Handler must print `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}`; test: `echo '{"hookEventName":"PreToolUse","tool_name":"test"}' \| arize-hook-copilot-pre-tool` |
| Phoenix unreachable | `curl -sf <endpoint>/v1/traces` |
| Test without sending | `ARIZE_DRY_RUN=true` before launching Copilot |
| Verbose logging | `acht config set verbose true` (or `ARIZE_VERBOSE=true`); errors always go to `~/.arize/harness/logs/copilot.log` |
| Wrong project name | `acht config set harnesses.copilot.project_name <name>` |
