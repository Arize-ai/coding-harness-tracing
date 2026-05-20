/**
 * Tests for src/wizard.ts — WizardPanel webview host.
 */

const vscode = require("../src/__tests__/__mocks__/vscode");

// Provide the mock vscode module to ts-jest
jest.mock("vscode", () => vscode, { virtual: true });

const { WizardPanel } = require("../src/wizard");

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeInstaller(overrides = {}) {
  return {
    install: jest.fn(async () => ({ success: true, error: null, harness: "codex", logs: [] })),
    uninstall: jest.fn(async () => ({ success: true, error: null, harness: "codex", logs: [] })),
    loadStatus: jest.fn(async () => ({
      success: true,
      error: null,
      user_id: "user1",
      harnesses: [
        {
          name: "codex",
          configured: true,
          project_name: "my-proj",
          backend: {
            target: "arize",
            endpoint: "otlp.arize.com:443",
            api_key: "secret",
            space_id: "sp1",
          },
          scope: null,
        },
      ],
      logging: null,
      codex_buffer: null,
    })),
    ...overrides,
  };
}

function extensionUri() {
  return { scheme: "file", path: "/ext" };
}

/** Get the panel mock object created by vscode.window.createWebviewPanel. */
function getPanel() {
  return vscode.window.createWebviewPanel.mock.results[
    vscode.window.createWebviewPanel.mock.results.length - 1
  ].value;
}

function simulateReady(panel) {
  panel.webview._simulateMessage({ type: "ready" });
}

// ---------------------------------------------------------------------------
// Setup / teardown
// ---------------------------------------------------------------------------

beforeEach(() => {
  WizardPanel.currentPanel = undefined;
  jest.clearAllMocks();
});

// ---------------------------------------------------------------------------
// Panel creation
// ---------------------------------------------------------------------------

describe("WizardPanel.open", () => {
  test("creates a webview panel via vscode API", () => {
    const installer = makeInstaller();
    WizardPanel.open(extensionUri(), installer);

    expect(vscode.window.createWebviewPanel).toHaveBeenCalledTimes(1);
    expect(vscode.window.createWebviewPanel).toHaveBeenCalledWith(
      "arize-wizard",
      "Arize Tracing Setup",
      vscode.ViewColumn.One,
      expect.objectContaining({ enableScripts: true }),
    );
  });

  test("sets WizardPanel.currentPanel", () => {
    const installer = makeInstaller();
    const wp = WizardPanel.open(extensionUri(), installer);

    expect(WizardPanel.currentPanel).toBe(wp);
  });

  test("calling open twice returns the same instance", () => {
    const installer = makeInstaller();
    const wp1 = WizardPanel.open(extensionUri(), installer);
    const wp2 = WizardPanel.open(extensionUri(), installer);

    expect(wp1).toBe(wp2);
    expect(vscode.window.createWebviewPanel).toHaveBeenCalledTimes(1);
  });

  test("calling open twice reveals the existing panel", () => {
    const installer = makeInstaller();
    WizardPanel.open(extensionUri(), installer);
    const panel = getPanel();

    WizardPanel.open(extensionUri(), installer);
    expect(panel.reveal).toHaveBeenCalledTimes(1);
  });
});

// ---------------------------------------------------------------------------
// Prefill
// ---------------------------------------------------------------------------

describe("prefill on ready", () => {
  test("setup with no prefill harness borrows backend from any configured harness", async () => {
    const installer = makeInstaller();
    WizardPanel.open(extensionUri(), installer);
    const panel = getPanel();

    simulateReady(panel);
    await new Promise((r) => setTimeout(r, 0));

    expect(panel.webview.postMessage).toHaveBeenCalledWith(
      expect.objectContaining({
        type: "prefill",
        request: expect.objectContaining({
          backend: expect.objectContaining({
            target: "arize",
            endpoint: "otlp.arize.com:443",
            api_key: "secret",
            space_id: "sp1",
          }),
          user_id: "user1",
        }),
      }),
    );
  });

  test("sends prefill with harness data when prefillHarness is set", async () => {
    const installer = makeInstaller();
    WizardPanel.open(extensionUri(), installer, { prefillHarness: "codex" });
    const panel = getPanel();

    simulateReady(panel);
    await new Promise((r) => setTimeout(r, 0));

    expect(installer.loadStatus).toHaveBeenCalledTimes(1);
    expect(panel.webview.postMessage).toHaveBeenCalledWith(
      expect.objectContaining({
        type: "prefill",
        harness: "codex",
        request: expect.objectContaining({
          backend: expect.objectContaining({
            target: "arize",
            endpoint: "otlp.arize.com:443",
            api_key: "secret",
            space_id: "sp1",
          }),
          project_name: "my-proj",
          user_id: "user1",
          with_skills: false,
        }),
      }),
    );
  });

  test("sends harness-only prefill when harness is not configured", async () => {
    const installer = makeInstaller({
      loadStatus: jest.fn(async () => ({
        success: true,
        error: null,
        user_id: null,
        harnesses: [
          { name: "codex", configured: false, project_name: null, backend: null, scope: null },
        ],
        logging: null,
        codex_buffer: null,
      })),
    });

    WizardPanel.open(extensionUri(), installer, { prefillHarness: "codex" });
    const panel = getPanel();

    simulateReady(panel);
    await new Promise((r) => setTimeout(r, 0));

    expect(panel.webview.postMessage).toHaveBeenCalledWith({
      type: "prefill",
      harness: "codex",
    });
  });

  test("second open with prefillHarness sends a fresh prefill", async () => {
    const installer = makeInstaller();
    WizardPanel.open(extensionUri(), installer);
    const panel = getPanel();

    // First ready — no prefill harness
    simulateReady(panel);
    await new Promise((r) => setTimeout(r, 0));

    // Second open with prefillHarness
    WizardPanel.open(extensionUri(), installer, { prefillHarness: "codex" });
    await new Promise((r) => setTimeout(r, 0));

    const prefillCalls = panel.webview.postMessage.mock.calls.filter(
      (c) => c[0].type === "prefill",
    );
    expect(prefillCalls.length).toBe(2);
    expect(prefillCalls[1][0].harness).toBe("codex");
  });
});

// ---------------------------------------------------------------------------
// Install
// ---------------------------------------------------------------------------

describe("install message handling", () => {
  const REQ = {
    harness: "codex",
    backend: {
      target: "arize",
      endpoint: "otlp.arize.com:443",
      api_key: "secret",
      space_id: "sp1",
    },
    project_name: "proj",
    user_id: null,
    with_skills: false,
    logging: null,
  };

  test("install message invokes installer and streams logs", async () => {
    const logCalls = [];
    const installer = makeInstaller({
      install: jest.fn(async (req, onLog) => {
        onLog("info", "Step 1...");
        onLog("info", "Step 2...");
        return { success: true, error: null, harness: "codex", logs: [] };
      }),
    });

    WizardPanel.open(extensionUri(), installer);
    const panel = getPanel();

    panel.webview._simulateMessage({ type: "install", request: REQ });
    await new Promise((r) => setTimeout(r, 0));

    // Verify log messages were posted
    const logMsgs = panel.webview.postMessage.mock.calls.filter(
      (c) => c[0].type === "log",
    );
    expect(logMsgs.length).toBe(2);
    expect(logMsgs[0][0]).toEqual({ type: "log", level: "info", message: "Step 1..." });
    expect(logMsgs[1][0]).toEqual({ type: "log", level: "info", message: "Step 2..." });

    // Verify result message
    const resultMsg = panel.webview.postMessage.mock.calls.find(
      (c) => c[0].type === "result",
    );
    expect(resultMsg[0].payload.success).toBe(true);
  });

  test("install result is posted after completion", async () => {
    const expectedResult = { success: false, error: "install_failed", harness: "codex", logs: ["oops"] };
    const installer = makeInstaller({
      install: jest.fn(async () => expectedResult),
    });

    WizardPanel.open(extensionUri(), installer);
    const panel = getPanel();

    panel.webview._simulateMessage({ type: "install", request: REQ });
    await new Promise((r) => setTimeout(r, 0));

    expect(panel.webview.postMessage).toHaveBeenCalledWith({
      type: "result",
      payload: expectedResult,
    });
  });
});

// ---------------------------------------------------------------------------
// Uninstall
// ---------------------------------------------------------------------------

describe("uninstall message handling", () => {
  test("uninstall message invokes installer.uninstall", async () => {
    const installer = makeInstaller();
    WizardPanel.open(extensionUri(), installer);
    const panel = getPanel();

    panel.webview._simulateMessage({ type: "uninstall", harness: "cursor" });
    await new Promise((r) => setTimeout(r, 0));

    expect(installer.uninstall).toHaveBeenCalledWith(
      "cursor",
      expect.any(Function),
      expect.any(Object), // AbortSignal
    );

    const resultMsg = panel.webview.postMessage.mock.calls.find(
      (c) => c[0].type === "result",
    );
    expect(resultMsg).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// Cancel / dispose
// ---------------------------------------------------------------------------

describe("cancel and dispose", () => {
  test("cancel message disposes the panel", () => {
    const installer = makeInstaller();
    WizardPanel.open(extensionUri(), installer);
    const panel = getPanel();

    panel.webview._simulateMessage({ type: "cancel" });

    expect(WizardPanel.currentPanel).toBeUndefined();
  });

  test("dispose cancels in-flight install via abort", async () => {
    let capturedSignal;
    const installer = makeInstaller({
      install: jest.fn((req, onLog, signal) => {
        capturedSignal = signal;
        // Return a promise that never resolves naturally
        return new Promise(() => {});
      }),
    });

    WizardPanel.open(extensionUri(), installer);
    const panel = getPanel();
    const wp = WizardPanel.currentPanel;

    // Start an install
    panel.webview._simulateMessage({
      type: "install",
      request: {
        harness: "codex",
        backend: { target: "arize", endpoint: "e", api_key: "k", space_id: "s" },
        project_name: "p",
        user_id: null,
        with_skills: false,
        logging: null,
      },
    });

    // Let the install start
    await new Promise((r) => setTimeout(r, 0));
    expect(capturedSignal).toBeDefined();
    expect(capturedSignal.aborted).toBe(false);

    // Dispose the panel
    wp.dispose();

    expect(capturedSignal.aborted).toBe(true);
    expect(WizardPanel.currentPanel).toBeUndefined();
  });

  test("panel close via onDidDispose clears currentPanel", () => {
    const installer = makeInstaller();
    WizardPanel.open(extensionUri(), installer);
    const panel = getPanel();

    expect(WizardPanel.currentPanel).toBeDefined();

    panel._simulateDispose();

    expect(WizardPanel.currentPanel).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// HTML generation
// ---------------------------------------------------------------------------

describe("HTML generation", () => {
  test("panel HTML includes CSP meta tag and wizard asset references", () => {
    const installer = makeInstaller();
    WizardPanel.open(extensionUri(), installer);
    const panel = getPanel();
    const html = panel.webview.html;

    expect(html).toContain("Content-Security-Policy");
    expect(html).toContain("wizard.css");
    expect(html).toContain("wizard.js");
    expect(html).toContain("wizard-root");
    expect(html).toContain("nonce-");
  });
});
