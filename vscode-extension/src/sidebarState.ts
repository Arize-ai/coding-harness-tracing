/**
 * Sidebar state controller.
 *
 * Pure orchestration over the bridge client and `SidebarProvider.render`.
 * No DOM, no vscode UI calls — just data flow and action dispatch.
 */

import * as vscode from "vscode";
import * as bridge from "./bridge";
import { HARNESS_KEYS } from "./types";
import type { HarnessKey, StatusPayload, CodexBufferPayload } from "./types";
import type { EnsureBridgeError } from "./bootstrap";
import type { SidebarProvider, SidebarViewState, SidebarAction } from "./sidebar";

// ---------------------------------------------------------------------------
// View-state translation
// ---------------------------------------------------------------------------

function backendLabel(backend: { target: string } | null): string | null {
  if (!backend) return null;
  if (backend.target === "arize") return "Arize AX";
  if (backend.target === "phoenix") return "Phoenix";
  return null;
}

function toViewState(
  status: StatusPayload,
  codex: CodexBufferPayload | null,
): SidebarViewState {
  const codexConfigured = status.harnesses.some(
    (h) => h.name === "codex" && h.configured,
  );

  return {
    harnesses: HARNESS_KEYS.map((key) => {
      const h = status.harnesses.find((x) => x.name === key);
      return {
        name: key,
        configured: h?.configured ?? false,
        projectName: h?.project_name ?? null,
        backendLabel: backendLabel(h?.backend ?? null),
      };
    }),
    userId: status.user_id,
    codexBuffer:
      codexConfigured && codex
        ? { state: codex.state, host: codex.host, port: codex.port }
        : null,
    bridgeError: status.success === false ? (status.error ?? "bridge_error") : null,
  };
}

function emptyState(bridgeError: string | null = null): SidebarViewState {
  return {
    harnesses: HARNESS_KEYS.map((key) => ({
      name: key,
      configured: false,
      projectName: null,
      backendLabel: null,
    })),
    userId: null,
    codexBuffer: null,
    bridgeError,
  };
}

// ---------------------------------------------------------------------------
// SidebarController
// ---------------------------------------------------------------------------

export class SidebarController implements vscode.Disposable {
  private readonly _provider: SidebarProvider;
  private readonly _refreshIntervalMs: number;
  private _timer: ReturnType<typeof setInterval> | undefined;
  private _disposables: vscode.Disposable[] = [];
  private _lastState: SidebarViewState = emptyState();

  private readonly _onOpenSetup = new vscode.EventEmitter<void>();
  public readonly onOpenSetup: vscode.Event<void> = this._onOpenSetup.event;

  private readonly _onOpenReconfigure = new vscode.EventEmitter<HarnessKey>();
  public readonly onOpenReconfigure: vscode.Event<HarnessKey> =
    this._onOpenReconfigure.event;

  private readonly _onSetUser = new vscode.EventEmitter<void>();
  public readonly onSetUser: vscode.Event<void> = this._onSetUser.event;

  constructor(provider: SidebarProvider, refreshIntervalMs?: number) {
    this._provider = provider;
    this._refreshIntervalMs = refreshIntervalMs ?? 30_000;
  }

  // ---- Public API ---------------------------------------------------------

  /** Wire onAction → controller handlers. Call once after construction. */
  attach(): void {
    // Subscribe to webview actions
    this._disposables.push(
      this._provider.onAction((action) => this.handleAction(action)),
    );

    // Visibility-driven refresh
    this._disposables.push(
      this._provider.onDidChangeVisibility((visible) => {
        if (visible) {
          void this.refresh();
        }
      }),
    );

    // Periodic refresh
    this._timer = setInterval(() => {
      if (this._provider.visible) {
        void this.refresh();
      }
    }, this._refreshIntervalMs);
  }

  /** Trigger a re-fetch and re-render. Safe to call repeatedly. */
  async refresh(): Promise<void> {
    let status: StatusPayload;
    try {
      status = await bridge.getStatus();
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      this._provider.render(emptyState(msg));
      return;
    }

    if (!status.success) {
      this._provider.render(toViewState(status, null));
      return;
    }

    const codexConfigured = status.harnesses.some(
      (h) => h.name === "codex" && h.configured,
    );

    let codex: CodexBufferPayload | null = null;
    if (codexConfigured) {
      const [, codexResult] = await Promise.all([
        Promise.resolve(status), // status already resolved; keeps the Promise.all shape
        bridge.codexBufferStatus().catch(() => null as CodexBufferPayload | null),
      ]);
      codex = codexResult;
    }

    const state = toViewState(status, codex);
    this._lastState = state;
    this._provider.render(state);
  }

  private _renderError(error: string): void {
    const state: SidebarViewState = { ...this._lastState, bridgeError: error };
    this._provider.render(state);
  }

  /**
   * Surface a bootstrap error into the sidebar view state.
   * The next `refresh()` overwrites it with real data from the bridge.
   */
  surfaceError(code: EnsureBridgeError, detail: string): void {
    this._renderError(`${code}: ${detail}`);
  }

  /** Dispatch any SidebarAction as if it had come from the webview. */
  async handleAction(action: SidebarAction): Promise<void> {
    switch (action.type) {
      case "setup":
        this._onOpenSetup.fire();
        break;

      case "setUser":
        this._onSetUser.fire();
        break;

      case "reconfigure":
        this._onOpenReconfigure.fire(action.harness);
        break;

      case "uninstall": {
        try {
          const result = await bridge.uninstall(action.harness);
          if (!result.success) {
            this._renderError(result.error ?? "uninstall_failed");
            return;
          }
        } catch (err) {
          const msg = err instanceof Error ? err.message : String(err);
          this._renderError(msg);
          return;
        }
        await this.refresh();
        break;
      }

      case "refresh":
        await this.refresh();
        break;

      case "startCodexBuffer":
        await this.startCodexBuffer();
        break;

      case "stopCodexBuffer":
        await this.stopCodexBuffer();
        break;
    }
  }

  async startCodexBuffer(): Promise<void> {
    try {
      const result = await bridge.codexBufferStart();
      if (!result.success) {
        this._renderError(result.error ?? "codex_buffer_start_failed");
        return;
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      this._renderError(msg);
      return;
    }
    await this.refresh();
  }

  async stopCodexBuffer(): Promise<void> {
    try {
      const result = await bridge.codexBufferStop();
      if (!result.success) {
        this._renderError(result.error ?? "codex_buffer_stop_failed");
        return;
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      this._renderError(msg);
      return;
    }
    await this.refresh();
  }

  dispose(): void {
    if (this._timer !== undefined) {
      clearInterval(this._timer);
      this._timer = undefined;
    }
    for (const d of this._disposables) {
      d.dispose();
    }
    this._disposables = [];
    this._onOpenSetup.dispose();
    this._onOpenReconfigure.dispose();
    this._onSetUser.dispose();
  }
}
