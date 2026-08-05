# Development tasks for coding-harness-tracing

# Install deps, configure git hooks, and warm pre-commit hook envs
setup:
    uv sync --all-extras --dev
    git config core.hooksPath .githooks
    uv run pre-commit install-hooks

# Run all linters (same as CI)
lint:
    uv run pre-commit run --all-files
