/**
 * Status bar integration for Arize Agent Kit.
 *
 * Displays a status bar item that summarises bridge state and offers
 * a quick-pick menu for common actions.
 */

import * as vscode from "vscode";
import * as bridge from "./bridge";
import { findPython, findBridgeBinary } from "./python";
import type { StatusPayload, CodexBufferPayload, HarnessStatusItem } from "./types";
import { DerivedState, DerivedStatus, deriveStatus } from "./status";

// ── StatusBarManager ──────────────────────────────────────────────────

export class StatusBarManager implements vscode.Disposable {
  private readonly _item: vscode.StatusBarItem;
  private _timer: ReturnType<typeof setInterval> | undefined;
  private _current: DerivedStatus;
  private _lastStatus: StatusPayload | null = null;

  constructor() {
    this._item = vscode.window.createStatusBarItem(
      vscode.StatusBarAlignment.Left,
      0,
    );
    this._item.command = "arize.statusBarMenu";
    this._current = {
      state: DerivedState.NoHarnesses,
      configuredCount: 0,
      totalCount: 5,
      codexBuffer: null,
      errorMessage: null,
    };
    this._render();
    this._item.show();
  }

  /** The most recently computed derived status. */
  get current(): DerivedStatus {
    return this._current;
  }

  /** Begin periodic refresh. */
  start(refreshIntervalMs = 30_000): void {
    this.refresh();
    this._timer = setInterval(() => this.refresh(), refreshIntervalMs);
  }

  /** Run one refresh cycle: probe python, bridge, status, codex. */
  async refresh(): Promise<void> {
    let pythonFound = false;
    let bridgeFound = false;
    let status: StatusPayload | null = null;
    let codexBuffer: CodexBufferPayload | null = null;
    let bridgeError: string | null = null;

    try {
      pythonFound = (await findPython()) !== null;
    } catch {
      pythonFound = false;
    }

    if (pythonFound) {
      try {
        bridgeFound = (await findBridgeBinary()) !== null;
      } catch {
        bridgeFound = false;
      }
    }

    if (pythonFound && bridgeFound) {
      try {
        status = await bridge.getStatus();
      } catch (err) {
        bridgeError = err instanceof Error ? err.message : String(err);
      }
    }

    // Fetch codex buffer only when codex is configured
    if (
      status !== null &&
      status.success &&
      status.harnesses.some((h) => h.name === "codex" && h.configured)
    ) {
      try {
        codexBuffer = await bridge.codexBufferStatus();
      } catch {
        codexBuffer = null;
      }
    }

    this._lastStatus = status;
    this._current = deriveStatus({
      pythonFound,
      bridgeFound,
      status,
      codexBuffer,
      bridgeError,
    });
    this._render();
  }

  /** Configured harnesses from the last status fetch. */
  get configuredHarnesses(): HarnessStatusItem[] {
    if (!this._lastStatus?.harnesses) return [];
    return this._lastStatus.harnesses.filter((h) => h.configured);
  }

  dispose(): void {
    if (this._timer !== undefined) {
      clearInterval(this._timer);
      this._timer = undefined;
    }
    this._item.dispose();
  }

  // ── private rendering ───────────────────────────────────────────────

  private _render(): void {
    const s = this._current;

    switch (s.state) {
      case DerivedState.PythonMissing:
        this._item.text = "$(warning) Arize: Python missing";
        this._item.tooltip = "Python \u2265 3.9 not found on PATH.";
        break;

      case DerivedState.BridgeMissing:
        this._item.text = "$(warning) Arize: bridge missing";
        this._item.tooltip =
          "Run install.sh to set up `arize-vscode-bridge`.";
        break;

      case DerivedState.BridgeError:
        this._item.text = "$(error) Arize: error";
        this._item.tooltip = s.errorMessage ?? "Unknown error";
        break;

      case DerivedState.NoHarnesses:
        this._item.text = "$(circle-slash) Arize: 0 harnesses";
        this._item.tooltip = "Click to set up tracing.";
        break;

      case DerivedState.Configured: {
        const n = s.configuredCount;
        this._item.text = `$(check) Arize: ${n} harness${n === 1 ? "" : "es"}`;
        this._item.tooltip = this._buildConfiguredTooltip();
        break;
      }
    }
  }

  private _buildConfiguredTooltip(): string {
    const lines: string[] = [];

    if (this._lastStatus?.harnesses) {
      for (const h of this._lastStatus.harnesses) {
        if (h.configured) {
          lines.push(
            `${h.name}: ${h.project_name ?? "\u2014"} \u2192 ${h.backend?.target ?? "\u2014"}`,
          );
        }
      }
    }

    if (
      this._current.codexBuffer &&
      this._current.codexBuffer.state === "stopped"
    ) {
      lines.push("(Codex buffer stopped)");
    }

    return lines.join("\n") || "Configured";
  }
}

// ── Status bar menu command ───────────────────────────────────────────

interface QuickPickActionItem extends vscode.QuickPickItem {
  _action: () => void;
  kind?: vscode.QuickPickItemKind;
}

export function registerStatusBarMenuCommand(
  ctx: vscode.ExtensionContext,
  manager: StatusBarManager,
): void {
  ctx.subscriptions.push(
    vscode.commands.registerCommand("arize.statusBarMenu", async () => {
      const items = buildMenuItems(manager.current, manager.configuredHarnesses);
      if (items.length === 0) return;

      const picked = await vscode.window.showQuickPick(items, {
        placeHolder: "Arize Agent Kit",
      });
      if (picked) {
        (picked as QuickPickActionItem)._action();
      }
    }),
  );
}

function buildMenuItems(
  status: DerivedStatus,
  configuredHarnesses: HarnessStatusItem[],
): QuickPickActionItem[] {
  const items: QuickPickActionItem[] = [];

  // PythonMissing / BridgeMissing → single install instructions item
  if (
    status.state === DerivedState.PythonMissing ||
    status.state === DerivedState.BridgeMissing
  ) {
    items.push({
      label: "Open install instructions",
      _action: () => {
        /* placeholder — opens docs */
      },
    });
    return items;
  }

  // Configured with codex buffer stopped/running → buffer action + Refresh
  if (status.state === DerivedState.Configured && status.codexBuffer) {
    if (status.codexBuffer.state === "stopped") {
      items.push({
        label: "Start Codex buffer",
        _action: () =>
          vscode.commands.executeCommand("arize.startCodexBuffer"),
      });
      items.push({
        label: "Refresh",
        _action: () => vscode.commands.executeCommand("arize.refreshStatus"),
      });
      return items;
    }
    if (status.codexBuffer.state === "running") {
      items.push({
        label: "Stop Codex buffer",
        _action: () =>
          vscode.commands.executeCommand("arize.stopCodexBuffer"),
      });
      items.push({
        label: "Refresh",
        _action: () => vscode.commands.executeCommand("arize.refreshStatus"),
      });
      return items;
    }
  }

  // Otherwise → Refresh, Set up new harness, per-harness entries
  items.push({
    label: "Refresh",
    _action: () => vscode.commands.executeCommand("arize.refreshStatus"),
  });

  items.push({
    label: "Set up new harness",
    _action: () => vscode.commands.executeCommand("arize.setup"),
  });

  for (const h of configuredHarnesses) {
    items.push({
      label: `Reconfigure ${h.name}`,
      _action: () =>
        vscode.commands.executeCommand("arize.reconfigure", h.name),
    });
    items.push({
      label: `Uninstall ${h.name}`,
      _action: () =>
        vscode.commands.executeCommand("arize.uninstall", h.name),
    });
  }

  // Append "Uninstall all" when at least one harness is configured.
  if (status.state === DerivedState.Configured) {
    items.push({
      label: "",
      kind: vscode.QuickPickItemKind.Separator,
      _action: () => {},
    });
    items.push({
      label: "$(trash) Uninstall all tracing (remove venv)",
      _action: () =>
        vscode.commands.executeCommand("arize.uninstallAll"),
    });
  }

  return items;
}
