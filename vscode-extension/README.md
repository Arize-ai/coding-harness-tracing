# Arize Agent Kit — VS Code Extension

Wire up [Arize](https://arize.com) tracing for AI coding agents — Claude Code, Codex, Cursor, Copilot, Gemini, and Kiro — without leaving the editor. The extension installs and configures the Arize harness in a managed venv, then surfaces status and controls in a sidebar view.

## What it does

- **Guided setup wizard** for tracing with `claude-code`, `codex`, `cursor`, `copilot`, `gemini`, and `kiro` harnesses
- **Sidebar view** listing configured harnesses with their project names and backends
- **Reconfigure and uninstall** individual harnesses directly from the sidebar
- **Status bar item** showing current tracing state at a glance
- **Codex buffer service controls** (start/stop) when the Codex harness is configured

## Prerequisites

- Python ≥ 3.9 available on `PATH`. The first time you open the Arize
  Tracing view, the extension creates a venv at `~/.arize/harness/venv`
  and installs the bridge automatically. No terminal step is required.

## Local development

- `npm install` from `vscode-extension/`.
- `npm run build` builds the bundled wheel and the extension JS.
- Press F5 in `vscode-extension/` to launch the Extension Development
  Host with the local build.

## Usage

1. Open the **Arize Tracing** activity bar view in the sidebar.
2. Click **Set Up Tracing** and follow the wizard to configure a harness.
3. Reconfigure or uninstall a harness using the inline buttons on its sidebar row.
4. Codex buffer state appears in the sidebar automatically when the Codex harness is configured.

## Commands

| Command ID | Title |
|------------|-------|
| `arize.setup` | Arize: Set Up Tracing |
| `arize.reconfigure` | Arize: Reconfigure Tracing |
| `arize.uninstall` | Arize: Uninstall Harness |
| `arize.refreshStatus` | Arize: Refresh Status |
| `arize.startCodexBuffer` | Arize: Start Codex Buffer |
| `arize.stopCodexBuffer` | Arize: Stop Codex Buffer |
| `arize.statusBarMenu` | Arize: Status Menu |

## Filing issues

Please report bugs and feature requests on the [GitHub issue tracker](https://github.com/Arize-ai/coding-harness-tracing/issues).
