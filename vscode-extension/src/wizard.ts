/**
 * WizardPanel — hosts the setup wizard in a VS Code WebviewPanel.
 *
 * Singleton panel that communicates with the wizard.js webview script
 * via the message contract defined in media/wizard.js.
 */

import * as vscode from "vscode";
import type { InstallerBridge } from "./installer";
import type { HarnessKey, InstallRequest } from "./types";

export interface WizardOpenOptions {
  prefillHarness?: HarnessKey;
}

export class WizardPanel implements vscode.Disposable {
  static currentPanel: WizardPanel | undefined;

  private readonly _panel: vscode.WebviewPanel;
  private readonly _installer: InstallerBridge;
  private _opts: WizardOpenOptions;
  private _abortController: AbortController | undefined;
  private _disposed = false;

  /** Open or focus the singleton panel. */
  static open(
    extensionUri: vscode.Uri,
    installer: InstallerBridge,
    opts?: WizardOpenOptions,
  ): WizardPanel {
    if (WizardPanel.currentPanel) {
      WizardPanel.currentPanel._panel.reveal();
      if (opts?.prefillHarness) {
        WizardPanel.currentPanel._opts = opts;
        WizardPanel.currentPanel._sendPrefill();
      }
      return WizardPanel.currentPanel;
    }

    const panel = vscode.window.createWebviewPanel(
      "arize-wizard",
      "Arize Tracing Setup",
      vscode.ViewColumn.One,
      {
        enableScripts: true,
        localResourceRoots: [vscode.Uri.joinPath(extensionUri, "media")],
      },
    );

    const instance = new WizardPanel(panel, extensionUri, installer, opts ?? {});
    WizardPanel.currentPanel = instance;
    return instance;
  }

  private constructor(
    panel: vscode.WebviewPanel,
    extensionUri: vscode.Uri,
    installer: InstallerBridge,
    opts: WizardOpenOptions,
  ) {
    this._panel = panel;
    this._installer = installer;
    this._opts = opts;

    // Build HTML
    const nonce = getNonce();
    const webview = panel.webview;
    const mediaUri = vscode.Uri.joinPath(extensionUri, "media");
    const cssUri = webview.asWebviewUri(vscode.Uri.joinPath(mediaUri, "wizard.css"));
    const jsUri = webview.asWebviewUri(vscode.Uri.joinPath(mediaUri, "wizard.js"));

    webview.html = this._buildHtml(nonce, webview.cspSource, cssUri, jsUri);

    // Listen for messages from the webview
    webview.onDidReceiveMessage((msg) => this._onMessage(msg));

    // Clean up on close
    panel.onDidDispose(() => this.dispose());
  }

  dispose(): void {
    if (this._disposed) return;
    this._disposed = true;

    // Cancel any in-flight operation
    if (this._abortController) {
      this._abortController.abort();
      this._abortController = undefined;
    }

    WizardPanel.currentPanel = undefined;
    this._panel.dispose();
  }

  // ---- Message handling ----------------------------------------------------

  private async _onMessage(msg: { type: string; [key: string]: unknown }): Promise<void> {
    switch (msg.type) {
      case "ready":
        await this._sendPrefill();
        break;

      case "install":
        await this._handleInstall(msg.request as InstallRequest);
        break;

      case "uninstall":
        await this._handleUninstall(msg.harness as HarnessKey);
        break;

      case "pickFolder":
        await this._handlePickFolder(typeof msg.current === "string" ? msg.current : undefined);
        break;

      case "cancel":
        this.dispose();
        break;
    }
  }

  private async _handlePickFolder(current?: string): Promise<void> {
    const defaultUri = current
      ? vscode.Uri.file(current)
      : vscode.workspace.workspaceFolders?.[0]?.uri ?? undefined;
    const result = await vscode.window.showOpenDialog({
      canSelectFolders: true,
      canSelectFiles: false,
      canSelectMany: false,
      defaultUri,
      openLabel: "Select workspace folder",
    });
    this._post({ type: "folderPicked", path: result?.[0]?.fsPath ?? null });
  }

  private async _sendPrefill(): Promise<void> {
    const harness = this._opts.prefillHarness;
    const workspaceFolder = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? undefined;

    let status;
    try {
      status = await this._installer.loadStatus();
    } catch {
      this._post(
        harness
          ? { type: "prefill", harness, workspace_folder: workspaceFolder }
          : { type: "prefill", workspace_folder: workspaceFolder },
      );
      return;
    }

    // Reconfigure path: prefer this harness's own existing config.
    if (harness) {
      const item = status.harnesses.find((h) => h.name === harness);
      if (item?.configured && item.backend) {
        this._post({
          type: "prefill",
          harness,
          workspace_folder: workspaceFolder,
          request: {
            backend: {
              target: item.backend.target,
              endpoint: item.backend.endpoint,
              api_key: item.backend.api_key,
              space_id: item.backend.space_id,
            },
            project_name: item.project_name ?? "",
            user_id: status.user_id ?? null,
            with_skills: false,
            kiro_options: item.kiro_options ?? null,
          },
        });
        return;
      }
    }

    // Setup-or-fresh-harness path: borrow backend from any already-configured
    // harness so the user doesn't have to retype api_key / space_id / endpoint.
    const donor = status.harnesses.find((h) => h.configured && h.backend);
    if (donor && donor.backend) {
      this._post({
        type: "prefill",
        harness,
        workspace_folder: workspaceFolder,
        request: {
          backend: {
            target: donor.backend.target,
            endpoint: donor.backend.endpoint,
            api_key: donor.backend.api_key,
            space_id: donor.backend.space_id,
          },
          // Don't carry the donor's project_name across harnesses — each
          // harness should default to its own name (handled by the webview).
          project_name: "",
          user_id: status.user_id ?? null,
          with_skills: false,
        },
      });
      return;
    }

    // Nothing configured yet — at minimum carry the user_id if we have one.
    this._post(
      status.user_id
        ? {
            type: "prefill",
            harness,
            workspace_folder: workspaceFolder,
            request: { user_id: status.user_id, with_skills: false },
          }
        : harness
        ? { type: "prefill", harness, workspace_folder: workspaceFolder }
        : { type: "prefill", workspace_folder: workspaceFolder },
    );
  }

  private async _handleInstall(request: InstallRequest): Promise<void> {
    this._abortController = new AbortController();
    const signal = this._abortController.signal;

    const result = await this._installer.install(
      request,
      (level, msg) => this._post({ type: "log", level, message: msg }),
      signal,
    );

    if (!this._disposed) {
      this._post({ type: "result", payload: result });
    }
    this._abortController = undefined;
  }

  private async _handleUninstall(harness: HarnessKey): Promise<void> {
    this._abortController = new AbortController();
    const signal = this._abortController.signal;

    const result = await this._installer.uninstall(
      harness,
      (level, msg) => this._post({ type: "log", level, message: msg }),
      signal,
    );

    if (!this._disposed) {
      this._post({ type: "result", payload: result });
    }
    this._abortController = undefined;
  }

  private _post(msg: Record<string, unknown>): void {
    if (!this._disposed) {
      this._panel.webview.postMessage(msg);
    }
  }

  // ---- HTML generation -----------------------------------------------------

  private _buildHtml(
    nonce: string,
    cspSource: string,
    cssUri: vscode.Uri,
    jsUri: vscode.Uri,
  ): string {
    return /* html */ `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta
    http-equiv="Content-Security-Policy"
    content="default-src 'none'; style-src ${cspSource}; script-src 'nonce-${nonce}';"
  />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Arize Tracing Setup</title>
  <link rel="stylesheet" href="${cssUri}" />
</head>
<body>
  <div id="wizard-root"></div>
  <script nonce="${nonce}" src="${jsUri}"></script>
</body>
</html>`;
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function getNonce(): string {
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  let nonce = "";
  for (let i = 0; i < 32; i++) {
    nonce += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return nonce;
}
