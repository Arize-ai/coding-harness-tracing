import * as vscode from "vscode";
import { SidebarProvider } from "./sidebar";
import { SidebarController } from "./sidebarState";
import { StatusBarManager, registerStatusBarMenuCommand } from "./statusBar";
import { WizardPanel } from "./wizard";
import { createBridgeInstaller } from "./installer";
import { ensureBridge } from "./bootstrap";
import { teardownAll } from "./teardown";
import { HARNESS_KEYS } from "./types";
import type { HarnessKey } from "./types";

// Lazy module-scoped output channel singleton
let _outputChannel: vscode.OutputChannel | undefined;

/** Reset module-scoped state between tests. */
export function _resetForTesting(): void {
  _outputChannel = undefined;
}

function promptForHarness(): Thenable<HarnessKey | undefined> {
  return vscode.window.showQuickPick([...HARNESS_KEYS], {
    placeHolder: "Select a harness",
  }) as Thenable<HarnessKey | undefined>;
}

export function activate(ctx: vscode.ExtensionContext): void {
  // 0. Output channel (lazy singleton, reusable across modules)
  if (!_outputChannel) {
    _outputChannel = vscode.window.createOutputChannel("Arize Tracing");
  }
  ctx.subscriptions.push(_outputChannel);

  // 1. Sidebar provider
  const sidebar = new SidebarProvider(ctx.extensionUri);
  ctx.subscriptions.push(sidebar);

  // 2. Register webview view provider
  ctx.subscriptions.push(
    vscode.window.registerWebviewViewProvider("arize-sidebar", sidebar),
  );

  // 3. Installer bridge
  const installer = createBridgeInstaller();

  // 4. Status bar
  const statusBar = new StatusBarManager();
  ctx.subscriptions.push(statusBar);
  statusBar.start();

  // 5. Sidebar controller
  const controller = new SidebarController(sidebar);
  ctx.subscriptions.push(controller);
  controller.attach();

  // 6. Bootstrap — ensureBridge then refresh
  const outputChannel = _outputChannel;
  void vscode.window.withProgress(
    {
      location: vscode.ProgressLocation.Notification,
      title: "Setting up Arize tracing",
      cancellable: true,
    },
    async (_progress, token) => {
      const ctrl = new AbortController();
      token.onCancellationRequested(() => ctrl.abort());
      const result = await ensureBridge({
        extensionPath: ctx.extensionPath,
        onLog: (level, msg) => outputChannel.appendLine(`[${level}] ${msg}`),
        signal: ctrl.signal,
      });
      if (!result.ok) {
        controller.surfaceError(result.error!, result.errorMessage ?? "Bootstrap failed.");
      }
      await controller.refresh();
      await statusBar.refresh();
    },
  );

  // 7. Status bar menu command
  registerStatusBarMenuCommand(ctx, statusBar);

  // 8. Commands
  ctx.subscriptions.push(
    vscode.commands.registerCommand("arize.setup", () =>
      WizardPanel.open(ctx.extensionUri, installer),
    ),
  );

  ctx.subscriptions.push(
    vscode.commands.registerCommand(
      "arize.reconfigure",
      async (harness?: HarnessKey) => {
        const h = harness ?? (await promptForHarness());
        if (h) WizardPanel.open(ctx.extensionUri, installer, { prefillHarness: h });
      },
    ),
  );

  ctx.subscriptions.push(
    vscode.commands.registerCommand(
      "arize.uninstall",
      async (harness?: HarnessKey) => {
        const h = harness ?? (await promptForHarness());
        if (h) await controller.handleAction({ type: "uninstall", harness: h });
      },
    ),
  );

  ctx.subscriptions.push(
    vscode.commands.registerCommand("arize.refreshStatus", () =>
      Promise.all([controller.refresh(), statusBar.refresh()]),
    ),
  );

  ctx.subscriptions.push(
    vscode.commands.registerCommand("arize.setUser", async () => {
      let currentUserId = "";
      try {
        const status = await installer.loadStatus();
        currentUserId = status.user_id ?? "";
      } catch {
        // Bridge unavailable — proceed with empty default; setUserId will
        // surface its own error if persisting fails.
      }

      const input = await vscode.window.showInputBox({
        title: "Set Arize User ID",
        prompt: "User ID attached to every span as user.id. Leave blank to clear.",
        value: currentUserId,
        ignoreFocusOut: true,
      });
      if (input === undefined) return;

      const trimmed = input.trim();
      if (trimmed === currentUserId) return;

      const result = await installer.setUserId(trimmed);
      if (!result.success) {
        for (const line of result.logs) {
          outputChannel.appendLine(line);
        }
        const action = await vscode.window.showErrorMessage(
          `Failed to update user ID: ${result.error ?? "unknown error"}`,
          "Show details",
        );
        if (action === "Show details") outputChannel.show();
        return;
      }

      await Promise.all([controller.refresh(), statusBar.refresh()]);
      vscode.window.showInformationMessage(
        trimmed ? `Arize user ID set to "${trimmed}".` : "Arize user ID cleared.",
      );
    }),
  );

  ctx.subscriptions.push(
    vscode.commands.registerCommand("arize.startCodexBuffer", () =>
      controller.startCodexBuffer(),
    ),
  );

  ctx.subscriptions.push(
    vscode.commands.registerCommand("arize.stopCodexBuffer", () =>
      controller.stopCodexBuffer(),
    ),
  );

  // arize.statusBarMenu already registered in step 7

  ctx.subscriptions.push(
    vscode.commands.registerCommand("arize.uninstallAll", async () => {
      const choice = await vscode.window.showWarningMessage(
        "Uninstall Arize tracing from all configured tools and delete the local Python venv?",
        {
          modal: true,
          detail:
            "This removes tracing hooks/configs from claude-code, codex, cursor, copilot, and gemini wherever they are configured, then deletes ~/.arize/harness/venv.",
        },
        "Uninstall",
      );
      if (choice !== "Uninstall") return;

      const result = await vscode.window.withProgress(
        {
          location: vscode.ProgressLocation.Notification,
          title: "Uninstalling Arize tracing",
          cancellable: true,
        },
        async (_progress, token) => {
          const ctrl = new AbortController();
          token.onCancellationRequested(() => ctrl.abort());
          return teardownAll({
            onLog: (level, msg) =>
              outputChannel.appendLine(`[${level}] ${msg}`),
            signal: ctrl.signal,
          });
        },
      );

      await controller.refresh();
      await statusBar.refresh();

      if (result.ok) {
        const action = await vscode.window.showInformationMessage(
          "Arize tracing fully removed.",
          "Show details",
        );
        if (action === "Show details") outputChannel.show();
      } else {
        const action = await vscode.window.showInformationMessage(
          "Arize tracing partially removed. See output for details.",
          "Show details",
        );
        if (action === "Show details") outputChannel.show();
      }
    }),
  );

  // 9. Controller event subscriptions
  ctx.subscriptions.push(
    controller.onOpenSetup(() => {
      vscode.commands.executeCommand("arize.setup");
    }),
  );

  ctx.subscriptions.push(
    controller.onOpenReconfigure((harness: HarnessKey) => {
      vscode.commands.executeCommand("arize.reconfigure", harness);
    }),
  );

  ctx.subscriptions.push(
    controller.onSetUser(() => {
      vscode.commands.executeCommand("arize.setUser");
    }),
  );
}

export function deactivate(): void {
  // No-op — VS Code disposes everything via ctx.subscriptions.
}
