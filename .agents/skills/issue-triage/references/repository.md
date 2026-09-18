# Repository and label routing

Read the current `README.md`, `CONTRIBUTING.md`, issue templates in `.github/ISSUE_TEMPLATE/`, and relevant source before using these pointers. The checkout is not the user's installed tree under `~/.arize/harness/`.

| Evidence | Starting point |
| --- | --- |
| Claude / Claude Code / `claude-code` | `tracing/claude_code/`; tests under `tests/tracing/claude/`; setup wrapper `core/setup/claude.py` |
| Codex, Cursor, Copilot, Gemini, Kiro, Antigravity, OpenCode, omp, Devin | Corresponding lowercase directory under `tracing/`, `tests/tracing/`, and `core/setup/` |
| Hook events, span builders, transcript parsing, session state | Relevant `tracing/<harness>/hooks/` and its tests |
| Hook registration or marketplace install | `tracing/<harness>/install.py`, plugin metadata if present, relevant harness README |
| Shared configuration or backend selection | `core/config.py`, `core/setup/`, `tests/core/` |
| Shared span/event processing or OTLP encoding | `core/common.py`, `core/event_model.py`, `core/otlp_proto.py` |
| CLI install dispatch or Windows parity | `install.sh`, `install.bat`, `core/setup/status.py`, `tests/windows/` |
| Console scripts or packaging | `pyproject.toml`, `tests/core/test_wire_entry_points.py` |
| CI or checks | `.github/workflows/`, `.pre-commit-config.yaml` |

`claude` is the install command, `claude-code` the config key, and `claude_code` the source directory. A Claude model in a Codex trace is still a Codex issue. Phoenix and Arize AX are destinations, not harnesses. Do not label every harness affected merely because a report mentions shared code; inspect its callers.

## Labels

Refresh labels on every run. Type, information, component, size, and gate judgments must still work when optional labels are missing.

- `bug`, `enhancement`, `documentation`, `cleanup`: use their live definitions; replace a demonstrably wrong type without stripping unrelated labels.
- `question`: inspect its definition; do not substitute it for `needs information` automatically.
- `good first issue`, `good student issue`: governed by the contributor policies.
- `duplicate`, `invalid`, `wontfix`, `help wanted`, priority and process labels: leave to maintainers unless the user explicitly requests a separate decision.
- Harness components: `c/claude`, `c/codex`, `c/cursor`, `c/copilot`, `c/gemini`, `c/kiro`, `c/antigravity`, `c/opencode`, `c/omp`, `c/devin`. Use `c/claude` for the `claude_code` source directory; do not create a parallel `harness:*` family.
- Shared components: `c/core`, `c/install`, `c/otlp`, `c/privacy`, `c/semcov`, `c/context-attributes`, `c/ci`. Apply each only when that dimension is part of the issue; a single-harness bug need not carry `c/core`.
- Destinations: `backend: phoenix`, `backend: arize`. Apply for backend-specific behavior, not merely because a reporter happens to use that destination.
- Intake: `triage`, `needs information`, `needs attention`, `blocked`, `new harness`. New integrations carry `new harness`; recommend a new `c/<name>` only once its identity is clear. The triage skill manages `needs information` and the opted-in removal of `triage`; recommend maintainer-owned `blocked` and `needs attention` rather than applying them automatically.
- Complexity: `size:S`, `size:M`, `size:L` describe implementation effort on issues, not PR line counts. Use one current size per issue.

Before applying any automation gate, inspect this repository's current workflows for consumers of that exact label and report the behavior it triggers. Keep `triage` unless `clear_triage` was requested; check its local consumers before removal.
