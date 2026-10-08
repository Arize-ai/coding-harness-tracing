# Contributing to Coding Harness Tracing

This repo emits OpenTelemetry and OpenInference spans from AI coding-assistant harnesses to [Arize AX](https://arize.com) or [Phoenix](https://github.com/Arize-ai/phoenix). The supported harnesses are the rows in the table in [README.md](README.md).

## What we welcome

- Bug fixes for incorrect spans, broken installs, or regressions.
- Reliability improvements such as better error handling, more robust hooks, or clearer logs.
- Documentation fixes, examples, and per-harness guides.
- New harness integrations for a coding assistant that is not already listed.

For larger features or behavior changes, open an issue first so we can discuss the approach before you write code. Keep PRs focused. Small, well-scoped changes are easier to review and merge.

## Open an issue first

Before starting non-trivial work:

1. Search [existing issues](https://github.com/Arize-ai/coding-harness-tracing/issues).
2. If nothing matches, open one with a [bug report](.github/ISSUE_TEMPLATE/bug.yml), [feature request](.github/ISSUE_TEMPLATE/feature_request.yml), or [new harness integration](.github/ISSUE_TEMPLATE/new_harness.yml) template.
3. Wait for a maintainer ack on the approach for anything beyond a small fix.

Questions that are not bugs belong in the [Arize community](https://arize.com/community). Conduct reports go to support@arize.com, as described in the [Code of Conduct](CODE_OF_CONDUCT.md).

## Set up the repo

Fork the repo and clone your fork:

```bash
git clone https://github.com/<your-username>/coding-harness-tracing.git
cd coding-harness-tracing
```

Install [uv](https://docs.astral.sh/uv/), then install dependencies:

```bash
uv sync --all-extras --dev
```

Install the git hooks:

```bash
uv run pre-commit install
```

The repo also ships `.githooks/pre-commit`. If you already set `core.hooksPath` to that directory, skip `pre-commit install`.

## Run tests and lint

These two commands are the local gates. CI runs the same pytest invocation on Python 3.9 through 3.14, and the same pre-commit hooks.

```bash
uv run pytest tests/ -m "not slow"
uv run pre-commit run --all-files
```

Both must be green before you open a PR.

To run one harness:

```bash
uv run pytest tests/tracing/cursor/
```

Claude Code tests live under `tests/tracing/claude/`, not `tests/tracing/claude_code/`.

If OpenCode plugin tests fail with Node exit code 9, install Node 22 or newer. Those tests call `node --experimental-transform-types`.

If `test_run_hook_bootstraps_on_python312_without_setuptools` fails against a uv-managed CPython 3.12, that test needs a system Python 3.12 on PATH outside the venv. It skips when it cannot find one.

You do not need to run the Windows jobs locally. CI runs `install.bat` and Windows hook delivery on `windows-latest`.

## Testing releases locally

You can rehearse build, publish, and install on your own machine before anything is public. Docker is optional. Only the clean-container helper needs it.

Start the local registry and release server. They run in the foreground, so use a separate terminal. Ctrl-C stops both.

```bash
scripts/local-registry.sh
```

This serves a pypiserver registry at `http://localhost:8080/simple/` from `~/local-pypi`. It also serves a release folder at `http://localhost:8000/` from `~/local-releases`, laid out like GitHub Releases (`releases/download/<tag>/...`). Installers that download release files can use it as their base URL. Override the folders and ports with `LOCAL_PYPI_DIR`, `LOCAL_RELEASE_DIR`, `LOCAL_REGISTRY_PORT`, and `LOCAL_RELEASE_PORT`.

Build and publish the current checkout:

```bash
scripts/publish-local.sh
```

This uploads the wheel and sdist to the registry. It also copies them into `releases/download/v<version>/` with a `SHA256SUMS` file, and writes the tag to `latest` at the release folder's root.

Install the build with a throwaway `HOME` and uv tool directory, so your real `~/.claude/settings.json` and tracing install stay untouched. `publish-local.sh` prints this command with your version filled in, so you can copy it from there. Otherwise, replace `0.1.0` below with the version you published:

```bash
export T=$(mktemp -d)
HOME=$T UV_TOOL_DIR=$T/tools UV_TOOL_BIN_DIR=$T/bin \
  uv tool install --reinstall --index http://localhost:8080/simple/ coding-harness-tracing==0.1.0
```

- Pin `==<version>` to install exactly the build you published. An unpinned install picks the newest stable version and skips development versions such as `0.2.0.dev1` while a stable one exists. Add `--prerelease allow` to an unpinned install to consider development versions too.
- Pass `--reinstall` after republishing. Without it, uv's cache can serve the previous build of the same version.
- Republishing the same version overwrites it. That is local only. Real PyPI never allows overwriting a file, so a real release always needs a new version.

To install inside a clean Linux container that reaches both servers through `host.docker.internal`:

```bash
scripts/test-in-container.sh ubuntu
scripts/test-in-container.sh fedora -- sh -c 'curl -fsS "$LOCAL_RELEASE_URL/latest"'
```

With no command, the helper checks both servers, installs uv, and installs the exact version named in `latest` from the local registry, so it always tests the build you just published. The distro defaults to `ubuntu`, so `scripts/test-in-container.sh -- <command>` also works. The container gets the server addresses as `LOCAL_REGISTRY_URL` and `LOCAL_RELEASE_URL`. The helper exits with a message if Docker is not running.

Both servers listen on `127.0.0.1` by default, because the registry accepts unauthenticated overwrites. Docker Desktop reaches them anyway. On a Linux Docker host, start the servers with `LOCAL_BIND=0.0.0.0` so the container can connect.

## Find the code you need

Shared install, config, and OTLP code lives in `core/`. Each coding assistant lives under `tracing/<harness>/`. User-facing install docs live in `tracing/<harness>/README.md`. The git clone is the source tree. A user install lives under `~/.arize/harness/` and is a different tree.

| If you are changing | Start here |
| --- | --- |
| Spans, hook handlers, or session state for one harness | `tracing/<harness>/hooks/` |
| How that harness registers with the coding assistant | `tracing/<harness>/install.py` |
| Shared config, span send, or the install wizard prompts | `core/` |
| Tests for one harness | `tests/tracing/<harness>/` |
| The `install.sh` and `install.bat` command name | `install.sh` `harness_dir()` and the dispatch `case`, plus the matching loops in `install.bat` |

### Common gotchas

Watch for these:

- `tracing/<harness>/install.py` owns that harness's install and uninstall flow. Shared installer helpers live in `core/setup/`.
- Most adapters read metadata from `core.constants.HARNESSES`. Kiro and Devin keep `SERVICE_NAME` and related fields in `tracing/<harness>/constants.py` instead. Copy the pattern the nearest harness already uses.
- Claude Code's CLI name is `claude`. Its config key and `HARNESS_NAME` are `claude-code`. `install.sh` accepts both spellings in `harness_dir()`, and only `claude` as an install command.
- Hook processes must stay on the stdlib. `python-dotenv` is the only PyPI dependency, and only `core/setup` imports it.
- Python 3.9 is the floor. CI rejects 3.10-only syntax.

## Add a harness

Open a [new harness integration](.github/ISSUE_TEMPLATE/new_harness.yml) issue first.

Copy `tracing/opencode/` if the assistant loads an in-process TypeScript or JavaScript plugin. Copy `tracing/devin/` if it invokes a subprocess hook and you do not want an entry in `core.constants.HARNESSES`. Claude Code and Cursor also have marketplace plugin extras under `tracing/claude_code/.claude-plugin/` and `tracing/cursor/.cursor-plugin/`. Those are not required for a first integration.

A new harness needs all of the following or CI fails:

1. `tracing/<harness>/` with `constants.py` (`HARNESS_NAME`), `install.py`, and `hooks/` (`adapter.py`, `handlers.py`).
2. `install.py` implements the harness install and uninstall behavior using shared helpers from `core/setup/` where appropriate.
3. `arize-hook-*` entries in `pyproject.toml` `[project.scripts]` for any hooks invoked by console script.
4. A `harness_dir()` mapping, a dispatch name, and a usage line in `install.sh`, plus the same names in `install.bat`.
5. One line in `core/setup/status.py` `_REGISTRATION`.
6. Tests under `tests/tracing/<harness>/`.
7. A mypy hook in `.pre-commit-config.yaml` for `^tracing/<harness>/`.
8. A row in the README supported-harness table.

After you wire it in, run:

```bash
uv run pytest tests/core/test_status.py tests/core/test_wire_entry_points.py tests/core/test_shell_router.py tests/core/test_bat_router.py tests/tracing/<harness>/
```

## Review your change before the PR

This repo ships an agentic code-review skill at [`.agents/skills/code-review/`](.agents/skills/code-review/). Run it on your branch and address its findings before you open a PR.

The skill reviews the diff for correctness and repo conventions, then runs the two gates above. It prints findings locally. It posts a GitHub review only if you ask it to.

Open the repo in an agent that supports skills and invoke `code-review` on your branch.

## Open a pull request

- Target the `main` branch.
- Use a conventional-commit-style PR title, for example `fix:`, `feat:`, `docs:`, `chore:`, or `refactor:`.
- Reference the issue in the PR body with `resolves #<issue-number>`.
- Keep the change focused.
- Fill out the PR template, including the test plan.

A maintainer will review the PR. Push updates to the same branch. CI and the CLA check must pass before merge. CI also runs Windows install and hook jobs, and CodeQL on same-repo PRs. Fork PRs skip the CodeQL requirement job.

## Sign the CLA

First-time contributors need to sign the CLA. After you open your first PR, the CLA bot comments with a link. Comment the following on the PR, exactly:

```
I have read the CLA Document and I hereby sign the CLA.
```

To re-run the check, comment `recheck`. Signatures are stored on the `cla` branch, not on `main`. You only need to sign once across Arize repos. See [`CLA.md`](CLA.md) for the full text.

## Code of Conduct

This project follows the [Code of Conduct](CODE_OF_CONDUCT.md). By participating, you agree to uphold it.
