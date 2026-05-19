import * as vscode from "vscode";
import { HarnessKey, HARNESS_KEYS } from "./types";

// ---------------------------------------------------------------------------
// State & action shapes
// ---------------------------------------------------------------------------

export interface SidebarViewState {
  harnesses: Array<{
    name: HarnessKey;
    configured: boolean;
    projectName: string | null;
    backendLabel: string | null;
    // Copilot-only: list of repos where hooks are installed. May be undefined
    // for other harnesses or until sidebarState.ts::toViewState passes it
    // through from StatusPayload.harnesses[*].repo_paths.
    // TODO(repo-paths-followup): wire repo_paths through in sidebarState.ts::toViewState.
    repoPaths?: string[];
  }>;
  userId: string | null;
  codexBuffer: {
    state: "running" | "stopped" | "stale" | "unknown";
    host: string | null;
    port: number | null;
  } | null;
  bridgeError: string | null;
}

export type SidebarAction =
  | { type: "setup" }
  | { type: "setUser" }
  | { type: "reconfigure"; harness: HarnessKey }
  | { type: "uninstall"; harness: HarnessKey }
  | { type: "refresh" }
  | { type: "startCodexBuffer" }
  | { type: "stopCodexBuffer" };

// ---------------------------------------------------------------------------
// Webview message types
// ---------------------------------------------------------------------------

type WebviewToExtension =
  | { type: "ready" }
  | { type: "action"; action: SidebarAction };

type ExtensionToWebview = { type: "render"; state: SidebarViewState };

// ---------------------------------------------------------------------------
// Human-readable harness names
// ---------------------------------------------------------------------------

const HARNESS_LABELS: Record<HarnessKey, string> = {
  "claude-code": "Claude Code",
  codex: "Codex",
  cursor: "Cursor",
  copilot: "Copilot",
  gemini: "Gemini",
  kiro: "Kiro",
};

// ---------------------------------------------------------------------------
// SidebarProvider
// ---------------------------------------------------------------------------

export class SidebarProvider implements vscode.WebviewViewProvider {
  private _view: vscode.WebviewView | undefined;
  private _disposables: vscode.Disposable[] = [];

  private readonly _onAction = new vscode.EventEmitter<SidebarAction>();
  public readonly onAction: vscode.Event<SidebarAction> = this._onAction.event;

  private readonly _onDidChangeVisibility = new vscode.EventEmitter<boolean>();
  public readonly onDidChangeVisibility: vscode.Event<boolean> =
    this._onDidChangeVisibility.event;

  constructor(private readonly _extensionUri: vscode.Uri) {}

  public get visible(): boolean {
    return this._view?.visible ?? false;
  }

  // ---- WebviewViewProvider ------------------------------------------------

  public resolveWebviewView(
    view: vscode.WebviewView,
    _ctx: vscode.WebviewViewResolveContext,
    _token: vscode.CancellationToken,
  ): void {
    this._view = view;

    view.webview.options = { enableScripts: true };

    // Set initial HTML shell
    const nonce = getNonce();
    view.webview.html = this._buildHtml(nonce);

    // Listen for messages from the webview
    view.webview.onDidReceiveMessage(
      (msg: WebviewToExtension) => {
        if (msg.type === "action") {
          this._onAction.fire(msg.action);
        }
        // "ready" — no-op on the provider side; sidebar-state drives render()
      },
      undefined,
      this._disposables,
    );

    // Track visibility changes
    view.onDidChangeVisibility(
      () => {
        this._onDidChangeVisibility.fire(view.visible);
      },
      undefined,
      this._disposables,
    );
  }

  // ---- Public API ---------------------------------------------------------

  /** Push new state into the webview. Idempotent. Called by sidebar-state. */
  public render(state: SidebarViewState): void {
    if (this._view) {
      this._view.webview.postMessage({
        type: "render",
        state,
      } satisfies ExtensionToWebview);
    }
  }

  public dispose(): void {
    for (const d of this._disposables) {
      d.dispose();
    }
    this._disposables = [];
    this._onAction.dispose();
    this._onDidChangeVisibility.dispose();
  }

  // ---- HTML generation ----------------------------------------------------

  private _buildHtml(nonce: string): string {
    return /* html */ `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta
    http-equiv="Content-Security-Policy"
    content="default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}';"
  />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <style nonce="${nonce}">
    :root {
      --fg: var(--vscode-foreground, #ccc);
      --bg: var(--vscode-sideBar-background, #1e1e1e);
      --border: var(--vscode-panel-border, #333);
      --btn-bg: var(--vscode-button-background, #0e639c);
      --btn-fg: var(--vscode-button-foreground, #fff);
      --btn-hover: var(--vscode-button-hoverBackground, #1177bb);
      --error-bg: var(--vscode-inputValidation-errorBackground, #5a1d1d);
      --error-border: var(--vscode-inputValidation-errorBorder, #be1100);
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: var(--vscode-font-family, sans-serif); font-size: 13px; color: var(--fg); background: var(--bg); padding: 8px; }
    .header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; gap: 6px; }
    .header-title { font-weight: 600; font-size: 13px; opacity: 0.8; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .header-actions { display: flex; align-items: center; gap: 4px; }
    .icon-btn { background: none; border: none; color: var(--fg); cursor: pointer; font-size: 14px; padding: 2px 4px; opacity: 0.7; }
    .icon-btn:hover { opacity: 1; }
    .error-banner { background: var(--error-bg); border: 1px solid var(--error-border); padding: 6px 8px; border-radius: 3px; margin-bottom: 8px; font-size: 12px; }
    .harness-list { list-style: none; }
    .harness-row { border-bottom: 1px solid var(--border); padding: 6px 0; }
    .harness-name { font-weight: 600; }
    .harness-meta { font-size: 12px; opacity: 0.7; margin-top: 2px; }
    .harness-meta.unconfigured { font-style: italic; }
    .harness-actions { margin-top: 4px; display: flex; gap: 6px; }
    .btn { background: var(--btn-bg); color: var(--btn-fg); border: none; padding: 3px 8px; border-radius: 3px; cursor: pointer; font-size: 12px; }
    .btn:hover { background: var(--btn-hover); }
    .btn-secondary { background: transparent; border: 1px solid var(--border); color: var(--fg); }
    .btn-secondary:hover { background: var(--border); }
  </style>
</head>
<body>
  <div id="root"></div>
  <script nonce="${nonce}">
    const vscode = acquireVsCodeApi();

    const HARNESS_LABELS = ${JSON.stringify(HARNESS_LABELS)};
    const HARNESS_KEYS = ${JSON.stringify(HARNESS_KEYS)};

    function sendAction(action) {
      vscode.postMessage({ type: "action", action });
    }

    function renderState(state) {
      const root = document.getElementById("root");
      let html = "";

      // Header
      html += '<div class="header">';
      html += '<span class="header-title">' + escapeHtml(state.userId || "Arize Tracing") + '</span>';
      html += '<span class="header-actions">';
      html += '<button class="btn" data-action="setup" title="Configure tracing">Configure</button>';
      html += '<button class="btn btn-secondary" data-action="setUser" title="Set the user ID attached to every span">Set User</button>';
      html += '<button class="icon-btn" data-action="refresh" title="Refresh">&#x21bb;</button>';
      html += '</span>';
      html += '</div>';

      // Error banner
      if (state.bridgeError) {
        html += '<div class="error-banner" data-testid="error-banner">' + escapeHtml(state.bridgeError) + '</div>';
      }

      // Harness list
      html += '<ul class="harness-list">';
      for (const key of HARNESS_KEYS) {
        const h = state.harnesses.find(function(x) { return x.name === key; });
        html += '<li class="harness-row" data-harness="' + key + '">';
        html += '<div class="harness-name">' + escapeHtml(HARNESS_LABELS[key] || key) + '</div>';
        if (h && h.configured) {
          let meta = "";
          if (h.projectName) meta += h.projectName;
          if (h.backendLabel) meta += (meta ? " · " : "") + h.backendLabel;
          if (meta) html += '<div class="harness-meta">' + escapeHtml(meta) + '</div>';
          if (key === "copilot" && Array.isArray(h.repoPaths) && h.repoPaths.length > 0) {
            var count = h.repoPaths.length;
            var label = count === 1 ? "1 workspace" : count + " workspaces";
            var joined = h.repoPaths.join(", ");
            var truncated = joined.length > 40 ? joined.slice(0, 37) + "..." : joined;
            html += '<div class="harness-meta">' + escapeHtml(label) + ' · <span title="' + escapeHtml(joined) + '">' + escapeHtml(truncated) + '</span></div>';
          }
          html += '<div class="harness-actions">';
          html += '<button class="btn btn-secondary" data-action="uninstall" data-harness="' + key + '">Uninstall</button>';
          html += '</div>';
        } else {
          html += '<div class="harness-meta unconfigured">Not configured</div>';
        }
        html += '</li>';
      }
      html += '</ul>';

      root.innerHTML = html;
    }

    function escapeHtml(str) {
      if (!str) return "";
      return str.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
    }

    // CSP forbids inline handlers, so use delegated click listening on
    // data-action attributes.
    document.addEventListener("click", function(event) {
      var el = event.target.closest("[data-action]");
      if (!el) return;
      var action = el.getAttribute("data-action");
      var harness = el.getAttribute("data-harness");
      if (action === "uninstall") {
        sendAction({ type: action, harness: harness });
      } else {
        sendAction({ type: action });
      }
    });

    window.addEventListener("message", function(event) {
      var msg = event.data;
      if (msg.type === "render") {
        renderState(msg.state);
      }
    });

    vscode.postMessage({ type: "ready" });
  </script>
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
