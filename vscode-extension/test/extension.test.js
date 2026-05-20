// @ts-check

jest.mock("vscode");

// Stub all modules that activate() imports
jest.mock("../src/sidebar", () => {
  return {
    SidebarProvider: jest.fn().mockImplementation(() => ({
      onAction: jest.fn(() => ({ dispose: jest.fn() })),
      onDidChangeVisibility: jest.fn(() => ({ dispose: jest.fn() })),
      render: jest.fn(),
      dispose: jest.fn(),
      visible: false,
    })),
  };
});

jest.mock("../src/sidebarState", () => {
  const onOpenSetup = jest.fn(() => ({ dispose: jest.fn() }));
  const onOpenReconfigure = jest.fn(() => ({ dispose: jest.fn() }));
  const onSetUser = jest.fn(() => ({ dispose: jest.fn() }));
  return {
    SidebarController: jest.fn().mockImplementation(() => ({
      attach: jest.fn(),
      refresh: jest.fn().mockResolvedValue(undefined),
      surfaceError: jest.fn(),
      handleAction: jest.fn().mockResolvedValue(undefined),
      startCodexBuffer: jest.fn().mockResolvedValue(undefined),
      stopCodexBuffer: jest.fn().mockResolvedValue(undefined),
      onOpenSetup,
      onOpenReconfigure,
      onSetUser,
      dispose: jest.fn(),
    })),
  };
});

jest.mock("../src/statusBar", () => {
  const vsc = require("vscode");
  return {
    StatusBarManager: jest.fn().mockImplementation(() => ({
      start: jest.fn(),
      refresh: jest.fn().mockResolvedValue(undefined),
      dispose: jest.fn(),
    })),
    registerStatusBarMenuCommand: jest.fn((_ctx, _mgr) => {
      // Simulate the real function registering the command
      vsc.commands.registerCommand("arize.statusBarMenu", jest.fn());
    }),
  };
});

jest.mock("../src/wizard", () => {
  return {
    WizardPanel: {
      open: jest.fn(),
    },
  };
});

jest.mock("../src/installer", () => {
  return {
    createBridgeInstaller: jest.fn(() => ({ _mock: true })),
  };
});

jest.mock("../src/bridge", () => ({}));

jest.mock("../src/bootstrap", () => ({
  ensureBridge: jest.fn().mockResolvedValue({ ok: true, bridgePath: "/x" }),
}));

jest.mock("../src/teardown", () => ({
  teardownAll: jest.fn().mockResolvedValue({
    ok: true,
    harnesses: [],
    venvRemoved: true,
  }),
}));

const vscode = require("vscode");
const { activate, deactivate, _resetForTesting } = require("../src/extension");
const { SidebarProvider } = require("../src/sidebar");
const { SidebarController } = require("../src/sidebarState");
const { StatusBarManager, registerStatusBarMenuCommand } = require("../src/statusBar");
const { WizardPanel } = require("../src/wizard");
const { ensureBridge } = require("../src/bootstrap");
const { teardownAll } = require("../src/teardown");

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const EXPECTED_COMMANDS = [
  "arize.setup",
  "arize.reconfigure",
  "arize.uninstall",
  "arize.refreshStatus",
  "arize.setUser",
  "arize.startCodexBuffer",
  "arize.stopCodexBuffer",
  "arize.statusBarMenu",
  "arize.uninstallAll",
];

function makeCtx() {
  return {
    extensionUri: { scheme: "file", path: "/mock/ext" },
    extensionPath: "/mock/ext",
    subscriptions: [],
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("activate(ctx)", () => {
  /** @type {ReturnType<typeof makeCtx>} */
  let ctx;
  /** @type {Record<string, (...args: unknown[]) => unknown>} */
  let commandHandlers;

  beforeEach(() => {
    jest.clearAllMocks();
    _resetForTesting();
    ctx = makeCtx();

    // Track command registrations
    commandHandlers = {};
    vscode.commands.registerCommand.mockImplementation((id, handler) => {
      commandHandlers[id] = handler;
      return { dispose: jest.fn() };
    });

    activate(ctx);
  });

  test("registers exactly the 8 commands declared in package.json", () => {
    const registeredIds = Object.keys(commandHandlers);
    expect(registeredIds.sort()).toEqual(EXPECTED_COMMANDS.sort());
  });

  test("registerWebviewViewProvider is called with 'arize-sidebar' and the SidebarProvider instance", () => {
    expect(vscode.window.registerWebviewViewProvider).toHaveBeenCalledWith(
      "arize-sidebar",
      expect.objectContaining({ render: expect.any(Function) }),
    );
  });

  test("controller.attach() and controller.refresh() are called in order", async () => {
    const controllerInstance = SidebarController.mock.results[0].value;
    expect(controllerInstance.attach).toHaveBeenCalled();
    // refresh is called inside the withProgress callback (async)
    await vscode.window.withProgress.mock.results[0].value;
    const attachOrder = controllerInstance.attach.mock.invocationCallOrder[0];
    const refreshOrder = controllerInstance.refresh.mock.invocationCallOrder[0];
    expect(controllerInstance.refresh).toHaveBeenCalled();
    expect(attachOrder).toBeLessThan(refreshOrder);
  });

  test("statusBar.start() is called", () => {
    const statusBarInstance = StatusBarManager.mock.results[0].value;
    expect(statusBarInstance.start).toHaveBeenCalled();
  });

  test("arize.setup handler invokes WizardPanel.open with no prefill", () => {
    commandHandlers["arize.setup"]();
    expect(WizardPanel.open).toHaveBeenCalledWith(
      ctx.extensionUri,
      expect.anything(),
    );
    // Should not have prefill options
    expect(WizardPanel.open).toHaveBeenCalledWith(
      expect.anything(),
      expect.anything(),
    );
    const callArgs = WizardPanel.open.mock.calls[0];
    expect(callArgs.length).toBeLessThanOrEqual(2);
  });

  test("arize.reconfigure with harness arg invokes WizardPanel.open with prefillHarness and does not call quick-pick", async () => {
    await commandHandlers["arize.reconfigure"]("cursor");
    expect(WizardPanel.open).toHaveBeenCalledWith(
      ctx.extensionUri,
      expect.anything(),
      { prefillHarness: "cursor" },
    );
    expect(vscode.window.showQuickPick).not.toHaveBeenCalled();
  });

  test("arize.startCodexBuffer calls controller.startCodexBuffer()", async () => {
    const controllerInstance = SidebarController.mock.results[0].value;
    await commandHandlers["arize.startCodexBuffer"]();
    expect(controllerInstance.startCodexBuffer).toHaveBeenCalled();
  });

  test("deactivate() does not throw", () => {
    expect(() => deactivate()).not.toThrow();
  });

  test("the 'Arize Tracing' OutputChannel is created and pushed to ctx.subscriptions", () => {
    expect(vscode.window.createOutputChannel).toHaveBeenCalledWith("Arize Tracing");
    // Should be among the subscriptions (first item pushed)
    const outputChannel = vscode.window.createOutputChannel.mock.results[0].value;
    expect(ctx.subscriptions).toContain(outputChannel);
  });
});

// ---------------------------------------------------------------------------
// Bootstrap integration tests
// ---------------------------------------------------------------------------

describe("activate(ctx) bootstrap integration", () => {
  /** @type {ReturnType<typeof makeCtx>} */
  let ctx;

  beforeEach(() => {
    jest.clearAllMocks();
    _resetForTesting();
    ctx = makeCtx();

    // Default: registerCommand just returns disposable
    vscode.commands.registerCommand.mockImplementation((_id, _handler) => {
      return { dispose: jest.fn() };
    });
  });

  test("calls ensureBridge exactly once with extensionPath from ctx", async () => {
    ensureBridge.mockResolvedValue({ ok: true, bridgePath: "/x" });
    activate(ctx);
    // withProgress runs the callback synchronously in our mock, returning a promise
    await vscode.window.withProgress.mock.results[0].value;

    expect(ensureBridge).toHaveBeenCalledTimes(1);
    expect(ensureBridge).toHaveBeenCalledWith(
      expect.objectContaining({ extensionPath: ctx.extensionPath }),
    );
  });

  test("on success, controller.refresh() is called and surfaceError is not", async () => {
    ensureBridge.mockResolvedValue({ ok: true, bridgePath: "/x" });
    activate(ctx);
    await vscode.window.withProgress.mock.results[0].value;

    const controllerInstance = SidebarController.mock.results[0].value;
    expect(controllerInstance.refresh).toHaveBeenCalled();
    expect(controllerInstance.surfaceError).not.toHaveBeenCalled();
    const statusBarInstance = StatusBarManager.mock.results[0].value;
    expect(statusBarInstance.refresh).toHaveBeenCalled();
  });

  test("on failure, surfaceError is called with error code and message, then refresh is called", async () => {
    ensureBridge.mockResolvedValue({
      ok: false,
      error: "python_not_found",
      errorMessage: "Python >= 3.9 not found on PATH.",
    });
    activate(ctx);
    await vscode.window.withProgress.mock.results[0].value;

    const controllerInstance = SidebarController.mock.results[0].value;
    expect(controllerInstance.surfaceError).toHaveBeenCalledTimes(1);
    expect(controllerInstance.surfaceError).toHaveBeenCalledWith(
      "python_not_found",
      "Python >= 3.9 not found on PATH.",
    );
    expect(controllerInstance.refresh).toHaveBeenCalled();
    const statusBarInstance = StatusBarManager.mock.results[0].value;
    expect(statusBarInstance.refresh).toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Teardown command tests
// ---------------------------------------------------------------------------

describe("arize.uninstallAll command", () => {
  /** @type {ReturnType<typeof makeCtx>} */
  let ctx;
  /** @type {Record<string, (...args: unknown[]) => unknown>} */
  let commandHandlers;

  beforeEach(() => {
    jest.clearAllMocks();
    _resetForTesting();
    ctx = makeCtx();

    commandHandlers = {};
    vscode.commands.registerCommand.mockImplementation((id, handler) => {
      commandHandlers[id] = handler;
      return { dispose: jest.fn() };
    });

    activate(ctx);
  });

  test("the command arize.uninstallAll is registered", () => {
    expect(commandHandlers["arize.uninstallAll"]).toBeDefined();
  });

  test("shows a modal warning message when invoked", async () => {
    vscode.window.showWarningMessage.mockResolvedValueOnce(undefined);

    await commandHandlers["arize.uninstallAll"]();

    expect(vscode.window.showWarningMessage).toHaveBeenCalledWith(
      expect.any(String),
      expect.objectContaining({ modal: true }),
      "Uninstall",
    );
  });

  test("when user confirms Uninstall, teardownAll is called once", async () => {
    vscode.window.showWarningMessage.mockResolvedValueOnce("Uninstall");

    await commandHandlers["arize.uninstallAll"]();

    expect(teardownAll).toHaveBeenCalledTimes(1);
  });

  test("when user cancels the modal, teardownAll is not called", async () => {
    vscode.window.showWarningMessage.mockResolvedValueOnce(undefined);

    await commandHandlers["arize.uninstallAll"]();

    expect(teardownAll).not.toHaveBeenCalled();
  });
});
