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
- OTLP spans are append-only. Do not emit a span you will need to patch later.

## Cursor shell status

`beforeShellExecution` and `afterShellExecution` merge into one Shell TOOL span. If `afterShellExecution` already has `exit_code` or `exitCode`, emit it immediately: OK for 0, ERROR for a nonzero integer, UNSET when the value is missing or not an integer.

The IDE omits that field and sends the code on the following `postToolUse`, inside the JSON string `tool_output` (`{"output": "...", "exitCode": 0}`). Hold the merged span and emit it once from that shell `postToolUse`. Do not also emit the generic tool span. If `postToolUse` never arrives, the next shell command or generation cleanup emits the held span with UNSET. Do not infer ERROR from output text.

## Pull requests

Target `main`. Title like `fix(cursor): ...`. Body includes `resolves #<issue>`. First-time contributors sign the CLA when the bot asks (see CONTRIBUTING.md).
