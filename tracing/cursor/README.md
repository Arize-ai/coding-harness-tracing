# Cursor IDE Tracing

Automatic [OpenInference](https://github.com/Arize-ai/openinference) tracing for the Cursor IDE and Cursor CLI. Spans are exported to [Arize AX](https://arize.com) or [Phoenix](https://github.com/Arize-ai/phoenix).

## Setup
The installer prompts for your backend (Phoenix or Arize AX) and project name, writes credentials to `~/.arize/harness/config.yaml`, and registers the hooks in `.cursor/hooks.json`.

Pass `--with-skills` to also symlink the `manage-cursor-tracing` skill into the current directory's `.agents/skills/` so coding agents in this workspace can help manage Cursor tracing configuration.

### Remote setup

macOS / Linux:

```bash
# Install
curl -sSL https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.sh | bash -s -- cursor

# Uninstall
curl -sSL https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.sh | bash -s -- uninstall cursor
```

Windows (PowerShell):

```powershell
# Install
iwr -useb https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat cursor

# Uninstall
iwr -useb https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat uninstall cursor
```

### Local setup

```bash
git clone https://github.com/Arize-ai/coding-harness-tracing.git
cd coding-harness-tracing
```

macOS / Linux:

```bash
# Install
./install.sh cursor

# Uninstall
./install.sh uninstall cursor
```

Windows:

```powershell
# Install
install.bat cursor

# Uninstall
install.bat uninstall cursor
```

### Cursor Cloud Agents

Cloud/Background Agents run in a separate remote VM, so they cannot use your local `~/.cursor/hooks.json`,
`~/.arize/harness/venv`, or `~/.arize/harness/config.yaml`. Each repo that should trace Cloud Agent work needs
repo-local Cursor files committed with the project. Use project hooks plus a Cloud bootstrap:

```bash
./install.sh cursor --cloud-agent
```

This writes:

- `.cursor/hooks.json` with repo-local Cursor hook entries
- `.cursor/hooks/arize-hook-cursor.sh`, a wrapper that calls the harness binary in the VM
- `.cursor/hooks/arize-cursor-cloud-setup.sh`, a bootstrap script for the VM
- `.cursor/hooks/arize-cloud-env.example`, a non-secret list of env vars to configure
- `.cursor/environment.json`, with an `install` command that surfaces the Arize env var names and runs the bootstrap

The installer intentionally does not copy credentials from your local `~/.arize/harness/config.yaml` into
`.cursor/environment.json`, because that file is normally committed and would expose secrets. Configure the real
values as Cursor Cloud environment secrets instead:

```bash
ARIZE_API_KEY=...
ARIZE_SPACE_ID=...
ARIZE_PROJECT_NAME=cursor
```

Phoenix can be used instead with `PHOENIX_ENDPOINT` and optional `ARIZE_API_KEY`.
`ARIZE_INSTALL_BRANCH` is optional; use it only when Cloud Agents must install from a branch other than `main`
(for example, while validating unmerged bootstrap changes). `ARIZE_INSTALL_URL` can also pin the raw installer URL.
The generated bootstrap checks for Python venv support and installs `python3-venv` on apt-based Cloud images when
needed before running the harness installer.

After running the installer, commit the generated `.cursor/hooks.json`, `.cursor/hooks/arize-hook-cursor.sh`,
`.cursor/hooks/arize-cursor-cloud-setup.sh`, `.cursor/hooks/arize-cloud-env.example`, and `.cursor/environment.json`
files. Cloud Agents will install the harness in their VM from `.cursor/environment.json`, then emit spans through the
repo-local hooks using the configured Cursor Cloud secrets.

For local repo-scoped hooks without Cloud bootstrap:

```bash
./install.sh cursor --project-hooks
```

## Default Settings

| Setting | Default |
|---------|---------|
| Harness key | `cursor` |
| Project name | `cursor` |
| Phoenix endpoint | `http://localhost:6006` |
| Arize AX endpoint | `otlp.arize.com:443` |
| Hook config file | `.cursor/hooks.json` |
| Hook events registered | `sessionStart`, `sessionEnd`, `beforeSubmitPrompt`, `afterAgentResponse`, `afterAgentThought`, `beforeShellExecution`, `afterShellExecution`, `beforeMCPExecution`, `afterMCPExecution`, `beforeReadFile`, `afterFileEdit`, `beforeTabFileRead`, `afterTabFileEdit`, `postToolUse`, `stop` |
| Events emitted by Cursor CLI | `sessionStart`, `sessionEnd`, `beforeShellExecution`, `afterShellExecution`, `afterFileEdit`, `postToolUse`, `stop` (subset of the above; remaining events are IDE-only) |
| State directory | `~/.arize/harness/state/cursor/` |
| Log file | `~/.arize/harness/logs/cursor.log` |

## Verifying tracing

Use Cursor (IDE or `agent` CLI) as normal. The hooks fire on agent activity within the workspace that contains `.cursor/hooks.json`.

- Errors land in `~/.arize/harness/logs/cursor.log` always; set `export ARIZE_VERBOSE=true` before launching Cursor to also see routine hook activity.
- Confirm spans appear in your configured project in Arize AX or Phoenix.
- IDE-only events (e.g. `beforeReadFile`, `beforeMCPExecution`, `afterAgentResponse`) only fire when running through the Cursor IDE; the CLI emits the subset listed in **Events emitted by Cursor CLI** above.

See the [main README's Environment variables section](../../README.md#environment-variables) for the full list of runtime overrides (`ARIZE_TRACE_ENABLED`, `ARIZE_DRY_RUN`, `ARIZE_USER_ID`, etc.).
