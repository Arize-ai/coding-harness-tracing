# AGENTS.md

## Cursor Cloud specific instructions

### What this repo is

Python library and installer for **Arize Coding Harness Tracing** — hook adapters that emit OpenInference spans from AI coding assistants (Claude, Codex, Cursor, Copilot, Gemini, Kiro) to Phoenix or Arize AX. There is no long-running web app in this repo; development centers on the Python package, tests, and per-harness installers.

### Dependencies

- **Python 3.9+** (CI tests 3.9–3.14; local dev uses the system interpreter via uv).
- **uv** package manager — install once if missing: `curl -LsSf https://astral.sh/uv/install.sh | sh` (binary at `~/.local/bin/uv`).
- Sync deps from repo root: `uv sync --all-extras --dev` (creates `.venv`).

### Lint, test, and type-check

Match CI (`.github/workflows/ci.yml`):

```bash
uv run pre-commit run --show-diff-on-failure --all-files
uv run pytest tests/ -m "not slow"
```

Pre-commit covers ruff, black, isort, mypy (per-package), and file hygiene hooks.

### Running hook CLIs locally

After `uv sync`, entry points are available via `uv run`:

```bash
uv run arize-config dump
uv run arize-hook-session-start   # reads JSON from stdin
```

Set `ARIZE_TRACE_ENABLED=true` and a backend in `~/.arize/harness/config.yaml` (or `PHOENIX_ENDPOINT` / `ARIZE_API_KEY`+`ARIZE_SPACE_ID` env vars). Use `ARIZE_DRY_RUN=true` to build spans without sending.

### Full harness install (`install.sh`)

`./install.sh <harness>` clones into `~/.arize/harness/`, creates a venv, and runs an **interactive** setup wizard (backend credentials, project name, logging toggles). It requires a TTY; in non-interactive Cloud Agent shells, prefer the dev venv workflow above or pre-write `~/.arize/harness/config.yaml`.

### Services (E2E tracing verification)

| Service | When needed |
|---------|-------------|
| Mock HTTP server or Phoenix (`localhost:6006`) | Only to verify span export E2E |
| Real harness CLI/IDE | Only for integration with a live assistant |

The test suite mocks backends; **no external services are required** for `pytest` or pre-commit.

### Gotchas

- Hook handlers read stdin JSON; pipe payloads when testing manually.
- State files live under `~/.arize/harness/state/<harness>/`.
- `install.sh` under `curl \| bash` redirects stdin from `/dev/tty` for prompts — non-interactive runs will fail on credential prompts.
- Re-running `uv sync` after dependency changes is enough; no separate build step.
