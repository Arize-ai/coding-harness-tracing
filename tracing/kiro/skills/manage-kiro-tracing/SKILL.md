---
name: manage-kiro-tracing
description: Set up and manage Arize tracing for Kiro CLI sessions using the acht CLI. Use when users want to set up Kiro tracing, configure Arize AX or Phoenix for Kiro, edit config, run diagnostics, choose or set a default traced agent, enable/disable tracing, or troubleshoot Kiro tracing. Triggers on "set up kiro tracing", "configure Arize for Kiro", "acht", "enable kiro tracing", "setup-kiro-tracing", "kiro agent tracing", or any request about connecting Kiro CLI to Arize or Phoenix for observability.
---

# Manage Kiro Tracing

Configure OpenInference tracing for the Kiro CLI to Arize AX (cloud) or Phoenix (self-hosted). Spans are sent directly to the backend from hooks — no background process or backend-specific Python deps run in the user's environment. Each session emits LLM turns, tool calls, cost in credits, model info, and turn duration.

The primary tool is the **`acht`** CLI. It installs a managed Python runtime (via [uv](https://github.com/astral-sh/uv)), writes the tracing hooks into a Kiro agent, and manages config. Reach for the repo only to inspect hook/handler internals: <https://github.com/Arize-ai/coding-harness-tracing> (Kiro code under `tracing/kiro/`).

## Kiro-specific: tracing is per-agent

Unlike the other harnesses, Kiro registers hooks **per agent**, not globally. Each agent is a JSON file at `~/.kiro/agents/<name>.json` with its own `hooks` block. Tracing only fires for the agent it was installed into, and **the agent must be selected at runtime**:

```bash
kiro-cli chat --agent arize-traced      # run the traced agent explicitly
kiro-cli agent set-default arize-traced  # ...or make it the default so `kiro-cli chat` uses it
```

So setup is tied to a specific agent name. `acht add kiro` asks which agent (default `arize-traced`); it either creates that agent fresh or merges the five hook entries into an existing agent you name. Whenever you debug, **confirm which agent the user actually runs** — tracing in a different agent won't fire.

## How to use this skill

1. **Installing / adding tracing?** → [Install](#install)
2. **Have credentials, changing a setting?** → [Configure via the CLI](#configure-via-the-cli)
3. **Not working / debugging?** → [Diagnose with doctor](#diagnose-with-doctor) then [Troubleshoot](#troubleshoot)
4. **No backend account yet?** → [Backends](#backends) first

## Install

```bash
go install github.com/Arize-ai/coding-harness-tracing/cmd/acht@latest
acht add kiro
```

`acht add kiro` bootstraps the runtime, writes hooks into the chosen agent file, and runs the wizard. Fields collected:

| Field | Notes |
|-------|-------|
| Backend | `arize` or `phoenix` |
| API key | `ARIZE_API_KEY` / `PHOENIX_API_KEY` — env var or masked prompt, never a flag |
| Space ID | Arize only |
| OTLP / Phoenix endpoint | Arize defaults to `otlp.arize.com:443`; Phoenix to `http://localhost:6006` |
| Project name | Defaults to `kiro` |
| User ID | Optional; added to every span as `user.id` |
| Agent name | **Kiro-specific** — defaults to `arize-traced`; name an existing agent to add tracing without switching |
| Set as default? | **Kiro-specific** — runs `kiro-cli agent set-default <name>` so plain `kiro-cli chat` uses it |
| Content logging | Three prompts (prompts / tool details / tool content), default **on** |
| Verbose | Terminal trace summaries, default **off** |

All five Kiro events (`agentSpawn`, `userPromptSubmit`, `preToolUse`, `postToolUse`, `stop`) route to a single `arize-hook-kiro` entry point that dispatches on the event name.

**Non-interactive:**

```bash
export ARIZE_API_KEY=...
acht add kiro --backend arize --space-id SPACE_ID --project-name kiro --non-interactive
```

After install, run with `kiro-cli chat --agent <name>` (or just `kiro-cli chat` if set as default).

## Backends

### Arize AX (cloud)

SaaS uses `otlp.arize.com:443`; on-prem needs a custom OTLP endpoint. Get credentials: log in (https://app.arize.com), **Settings** → **Space ID** on Space Settings; **API Keys** tab to create/copy a key. Both `api_key` and `space_id` required.

### Phoenix (self-hosted)

```bash
pip install arize-phoenix && phoenix serve   # or: docker run -p 6006:6006 arizephoenix/phoenix:latest
```

UI at `http://localhost:6006`. Verify: `curl -sf http://localhost:6006/v1/traces >/dev/null && echo ok`.

## Configure via the CLI

Backend credentials live in `~/.arize/harness/config.yaml`. The hook wiring lives in the agent file `~/.kiro/agents/<name>.json` (written by `acht add kiro`). Edit backend settings with `acht config`:

```bash
acht config show                              # api_key masked
acht config set harnesses.kiro.project_name kiro
acht config set verbose true
acht config edit
```

Schema:

```yaml
harnesses:
  kiro:
    project_name: kiro
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
| `✗ venv` | Runtime missing/broken → `acht add kiro` or `acht update` |
| `✗ settings:kiro` | Default agent file `~/.kiro/agents/arize-traced.json` missing → re-run `acht add kiro`. **Note:** doctor checks the *default* agent path; if the user installed into a differently-named agent this can falsely fail — verify with `cat ~/.kiro/agents/<their-agent>.json` |
| `✗ env:kiro` | No creds in env or config → `acht config set harnesses.kiro.api_key ...` |
| `✗ otlp_endpoint` | Endpoint unreachable → check network/endpoint |
| all `✓` but no traces | Confirm the user runs the traced agent (`--agent <name>` or set-default); see [Troubleshoot](#troubleshoot) |

## Uninstall

```bash
acht uninstall --kiro     # remove Kiro tracing, keep the shared runtime
acht uninstall            # remove all harnesses + the shared runtime
```

Removes the Arize hook entries from each agent file; deletes the agent file only if the installer created it.

## Troubleshoot

Run `acht doctor` first. Then:

| Problem | Fix |
|---------|-----|
| No traces | Confirm the user runs the traced agent: `kiro-cli chat --agent <name>`, or `kiro-cli agent set-default <name>` |
| Hooks in wrong agent | Check `cat ~/.kiro/agents/<name>.json` has the five `arize-hook-kiro` entries; re-run `acht add kiro` naming the right agent |
| Phoenix unreachable | `curl -sf <endpoint>/v1/traces` |
| Agent config rejected | `kiro-cli agent validate --path ~/.kiro/agents/<name>.json` |
| Test without sending | `ARIZE_DRY_RUN=true` (set before launching Kiro) |
| Verbose logging | `acht config set verbose true` (or `ARIZE_VERBOSE=true`); errors always go to `~/.arize/harness/logs/kiro.log` |
| Wrong project name | `acht config set harnesses.kiro.project_name <name>` |
