# CLAUDE.md

Agent notes for this repository. Human contributors should follow [CONTRIBUTING.md](CONTRIBUTING.md).

## What this is

Hooks emit OpenTelemetry / OpenInference spans from AI coding harnesses to Arize AX or Phoenix. Shared install, config, and OTLP code is in `core/`. Each harness is `tracing/<harness>/` with `install.py` and `hooks/` (`adapter.py`, `handlers.py`). The clone is the source tree. A user install under `~/.arize/harness/` is a different tree.

## Setup and gates

```bash
uv sync --all-extras --dev
uv run pytest tests/ -m "not slow"
uv run pre-commit run --all-files
```

One harness: `uv run pytest tests/tracing/<harness>/`. Claude Code tests live in `tests/tracing/claude/`, not `tests/tracing/claude_code/`.

Both gates must pass before a pull request. Run `.agents/skills/code-review/` on the branch first.

## Constraints

- Python 3.9 is the floor. No `match`/`case`, no `X | Y` unions evaluated at runtime, no other 3.10+ syntax.
- Hook code stays on the stdlib. `python-dotenv` is the only runtime PyPI dependency, and only `core/setup` imports it.
- Keep a change inside the harness and handler the issue names. Do not retune turn lifecycle unless that is the task.
- Prefer a small pure helper over a new class. Match the surrounding handler style.

## Cursor `postToolUse`

Generic tools (Grep, Glob, and anything not in `_DEDICATED_TOOL_NAMES`) emit one TOOL span from `_handle_post_tool_use`. Cursor puts the result in `tool_output`, usually a JSON string `{"output": "...", "exitCode": 0}`. Read that field, unwrap `output` when present, and leave plain text and invalid JSON unchanged. Shell exit status belongs on the dedicated `afterShellExecution` span, not here.

## Pull requests

Target `main`. Title like `fix(cursor): ...`. Body includes `resolves #<issue>`. First-time contributors sign the CLA when the bot asks (see CONTRIBUTING.md).
