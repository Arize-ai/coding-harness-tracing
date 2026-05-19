# GitHub Copilot Tracing

Automatic [OpenInference](https://github.com/Arize-ai/openinference) tracing for GitHub Copilot in VS Code. Spans are exported to [Arize AX](https://arize.com) or [Phoenix](https://github.com/Arize-ai/phoenix).

Copilot is installed **per-repo**, with hooks written into each workspace's `.github/hooks/hooks.json`. Unlike the other harnesses, there is no user-global install — re-run the installer in every repo where you want spans emitted.

## Setup
The installer prompts for your backend (Phoenix or Arize AX) and project name, writes credentials to `~/.arize/harness/config.yaml`, and registers Copilot Chat hooks at `<repo>/.github/hooks/hooks.json`.

Pass `--with-skills` to also symlink the `manage-copilot-tracing` skill into the current directory's `.agents/skills/` so coding agents in this workspace can help manage Copilot tracing configuration.

### Remote setup

macOS / Linux:

```bash
# Install
curl -sSL https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.sh | bash -s -- copilot

# Uninstall
curl -sSL https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.sh | bash -s -- uninstall copilot
```

Windows (PowerShell):

```powershell
# Install
iwr -useb https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat copilot

# Uninstall
iwr -useb https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat uninstall copilot
```

### Local setup

```bash
git clone https://github.com/Arize-ai/coding-harness-tracing.git
cd coding-harness-tracing
```

macOS / Linux:

```bash
# Install
./install.sh copilot

# Uninstall
./install.sh uninstall copilot
```

Windows:

```powershell
# Install
install.bat copilot

# Uninstall
install.bat uninstall copilot
```

## Per-repo installation

GitHub Copilot Chat reads its hooks configuration from `<workspace>/.github/hooks/hooks.json` — a path that is resolved relative to the open workspace, not the user's home directory. The other harnesses in this repo (Claude Code, Codex, Cursor, Gemini, Kiro) all install into user-global locations like `~/.claude/` or `~/.codex/`, so a single install covers every project on the machine. Copilot is the exception: tracing has to be wired into each repo individually.

### Where the hooks live

Each install writes a `.github/hooks/hooks.json` file inside the target repo. The hooks file follows the standard Copilot Chat hook schema (see [GitHub's hooks documentation](https://docs.github.com/en/copilot)) and registers handlers for `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`, and `SubagentStop`.

### How configured repos are tracked

Every install appends the target repo's absolute path to a list in `~/.arize/harness/config.yaml`. The list is deduplicated, so re-running the installer in the same repo is a no-op for the path list. Example:

```yaml
harnesses:
  copilot:
    repo_paths:
      - /Users/alice/code/service-a
      - /Users/alice/code/service-b
```

### Choosing the target repo

- **CLI (`./install.sh copilot` / `install.bat copilot`)** — defaults to the current working directory. `cd` into the repo before running the installer.
- **VS Code extension** — the setup wizard shows a workspace folder picker when Copilot is selected. The currently active workspace folder is the default; browse to a different folder if you want to install into another repo.

### Multi-repo workflow

To trace Copilot activity across several repos, run the installer once per repo (or run the VS Code wizard once per workspace). All configured paths accumulate under the single `harnesses.copilot.repo_paths` list and share the same backend credentials and project name.

### Uninstall

`./install.sh uninstall copilot` (or `install.bat uninstall copilot`) walks every path in `harnesses.copilot.repo_paths`, removes the Arize entries from each `.github/hooks/hooks.json`, then deletes the `harnesses.copilot` entry from `~/.arize/harness/config.yaml`.

If a tracked repo has been deleted or moved before uninstall runs, the hook removal for that path is a no-op — uninstall logs and continues rather than failing.

## Default Settings

| Setting | Default |
|---------|---------|
| Harness key | `copilot` |
| Project name | `copilot` |
| Phoenix endpoint | `http://localhost:6006` |
| Arize AX endpoint | `otlp.arize.com:443` |
| Hook config file | `.github/hooks/hooks.json` |
| Hook events | `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`, `SubagentStop` |
| State directory | `~/.arize/harness/state/copilot/` |
| Log file | `~/.arize/harness/logs/copilot.log` |

## Verifying tracing

Use GitHub Copilot Chat in VS Code (or the Copilot CLI) inside the workspace that contains `.github/hooks/hooks.json`. The hooks fire on `SessionStart`, `UserPromptSubmit`, tool invocations, and `Stop`.

- Errors land in `~/.arize/harness/logs/copilot.log` always; set `export ARIZE_VERBOSE=true` before launching VS Code / Copilot CLI to also see routine hook activity.
- Confirm spans appear in your configured project in Arize AX or Phoenix.
- The hooks file is per-workspace — repeat the install in each repo where you want Copilot tracing.

See the [main README's Environment variables section](../../README.md#environment-variables) for the full list of runtime overrides (`ARIZE_TRACE_ENABLED`, `ARIZE_DRY_RUN`, `ARIZE_USER_ID`, etc.).
