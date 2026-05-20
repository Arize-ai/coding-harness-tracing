// @ts-check
/** @typedef {import("../src/types").StatusPayload} StatusPayload */
/** @typedef {import("../src/types").CodexBufferPayload} CodexBufferPayload */
/** @typedef {import("../src/sidebar").SidebarViewState} SidebarViewState */
/** @typedef {import("../src/sidebar").SidebarAction} SidebarAction */

jest.mock("../src/bridge", () => ({
  getStatus: jest.fn(),
  uninstall: jest.fn(),
  codexBufferStatus: jest.fn(),
  codexBufferStart: jest.fn(),
  codexBufferStop: jest.fn(),
}));

const bridge = require("../src/bridge");
const vscode = require("vscode");
const { SidebarController } = require("../src/sidebarState");

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** @returns {StatusPayload} */
function emptyStatus() {
  return {
    success: true,
    error: null,
    user_id: null,
    harnesses: [
      { name: "claude-code", configured: false, project_name: null, backend: null, scope: null },
      { name: "codex", configured: false, project_name: null, backend: null, scope: null },
      { name: "cursor", configured: false, project_name: null, backend: null, scope: null },
      { name: "copilot", configured: false, project_name: null, backend: null, scope: null },
      { name: "gemini", configured: false, project_name: null, backend: null, scope: null },
    ],
    logging: null,
    codex_buffer: null,
  };
}

/** @returns {StatusPayload} */
function codexConfiguredStatus() {
  const s = emptyStatus();
  s.user_id = "u-123";
  s.harnesses[1] = {
    name: "codex",
    configured: true,
    project_name: "my-proj",
    backend: { target: "arize", endpoint: "https://e", api_key: "k", space_id: null },
    scope: null,
  };
  return s;
}

/** @returns {CodexBufferPayload} */
function bufferPayload(state = "running") {
  return { success: true, error: null, state, host: "127.0.0.1", port: 9090, pid: 42 };
}

function makeProvider() {
  const onActionEmitter = new vscode.EventEmitter();
  const onVisibilityEmitter = new vscode.EventEmitter();
  return {
    render: jest.fn(),
    onAction: onActionEmitter.event,
    onDidChangeVisibility: onVisibilityEmitter.event,
    visible: true,
    _fireAction: /** @param {SidebarAction} a */ (a) => onActionEmitter.fire(a),
    _fireVisibility: /** @param {boolean} v */ (v) => onVisibilityEmitter.fire(v),
    _setVisible: /** @param {boolean} v */ function (v) { this.visible = v; },
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("SidebarController", () => {
  /** @type {ReturnType<typeof makeProvider>} */
  let provider;
  /** @type {InstanceType<typeof SidebarController>} */
  let ctrl;

  beforeEach(() => {
    jest.useFakeTimers();
    jest.clearAllMocks();
    provider = makeProvider();
  });

  afterEach(() => {
    ctrl?.dispose();
    jest.useRealTimers();
  });

  // ---- refresh() ----------------------------------------------------------

  test("refresh() with empty config produces six unconfigured rows", async () => {
    bridge.getStatus.mockResolvedValue(emptyStatus());
    ctrl = new SidebarController(provider);
    await ctrl.refresh();

    expect(provider.render).toHaveBeenCalledTimes(1);
    const state = provider.render.mock.calls[0][0];
    expect(state.harnesses).toHaveLength(6);
    expect(state.harnesses.every((h) => !h.configured)).toBe(true);
    expect(state.userId).toBeNull();
    expect(state.codexBuffer).toBeNull();
    expect(state.bridgeError).toBeNull();
  });

  test("refresh() with codex configured calls both getStatus and codexBufferStatus", async () => {
    bridge.getStatus.mockResolvedValue(codexConfiguredStatus());
    bridge.codexBufferStatus.mockResolvedValue(bufferPayload("running"));
    ctrl = new SidebarController(provider);
    await ctrl.refresh();

    expect(bridge.getStatus).toHaveBeenCalled();
    expect(bridge.codexBufferStatus).toHaveBeenCalled();
    const state = provider.render.mock.calls[0][0];
    expect(state.codexBuffer).not.toBeNull();
    expect(state.codexBuffer.state).toBe("running");
  });

  test("refresh() with codex NOT configured does not call codexBufferStatus", async () => {
    bridge.getStatus.mockResolvedValue(emptyStatus());
    ctrl = new SidebarController(provider);
    await ctrl.refresh();

    expect(bridge.codexBufferStatus).not.toHaveBeenCalled();
    const state = provider.render.mock.calls[0][0];
    expect(state.codexBuffer).toBeNull();
  });

  test("bridge.getStatus rejection produces bridgeError state", async () => {
    bridge.getStatus.mockRejectedValue(new Error("network down"));
    ctrl = new SidebarController(provider);
    await ctrl.refresh();

    const state = provider.render.mock.calls[0][0];
    expect(state.bridgeError).toBe("network down");
    expect(state.harnesses).toHaveLength(6);
    expect(state.harnesses.every((h) => !h.configured)).toBe(true);
  });

  // ---- action dispatch ----------------------------------------------------

  test("attach() + onAction uninstall calls bridge.uninstall then refresh", async () => {
    const status = emptyStatus();
    bridge.getStatus.mockResolvedValue(status);
    bridge.uninstall.mockResolvedValue({ success: true, error: null, harness: "codex", logs: [] });

    ctrl = new SidebarController(provider);
    ctrl.attach();

    await ctrl.handleAction({ type: "uninstall", harness: "codex" });

    expect(bridge.uninstall).toHaveBeenCalledWith("codex");
    // refresh was called after uninstall
    expect(bridge.getStatus).toHaveBeenCalled();
  });

  test("handleAction reconfigure fires onOpenReconfigure without bridge call", async () => {
    ctrl = new SidebarController(provider);
    const fired = [];
    ctrl.onOpenReconfigure((h) => fired.push(h));

    await ctrl.handleAction({ type: "reconfigure", harness: "claude-code" });

    expect(fired).toEqual(["claude-code"]);
    expect(bridge.getStatus).not.toHaveBeenCalled();
    expect(bridge.uninstall).not.toHaveBeenCalled();
  });

  test("handleAction setup fires onOpenSetup", async () => {
    ctrl = new SidebarController(provider);
    let setupFired = false;
    ctrl.onOpenSetup(() => { setupFired = true; });

    await ctrl.handleAction({ type: "setup" });

    expect(setupFired).toBe(true);
  });

  // ---- codex buffer -------------------------------------------------------

  test("startCodexBuffer calls bridge then refreshes", async () => {
    bridge.codexBufferStart.mockResolvedValue(bufferPayload("running"));
    bridge.getStatus.mockResolvedValue(codexConfiguredStatus());
    bridge.codexBufferStatus.mockResolvedValue(bufferPayload("running"));

    ctrl = new SidebarController(provider);
    await ctrl.startCodexBuffer();

    expect(bridge.codexBufferStart).toHaveBeenCalled();
    expect(bridge.getStatus).toHaveBeenCalled();
  });

  test("startCodexBuffer failure populates bridgeError", async () => {
    bridge.codexBufferStart.mockResolvedValue({ success: false, error: "spawn failed", state: "unknown", host: null, port: null, pid: null });

    ctrl = new SidebarController(provider);
    await ctrl.startCodexBuffer();

    const state = provider.render.mock.calls[0][0];
    expect(state.bridgeError).toBe("spawn failed");
    // Should not attempt refresh after failure
    expect(bridge.getStatus).not.toHaveBeenCalled();
  });

  test("startCodexBuffer rejection populates bridgeError", async () => {
    bridge.codexBufferStart.mockRejectedValue(new Error("binary missing"));

    ctrl = new SidebarController(provider);
    await ctrl.startCodexBuffer();

    const state = provider.render.mock.calls[0][0];
    expect(state.bridgeError).toBe("binary missing");
  });

  test("stopCodexBuffer calls bridge then refreshes", async () => {
    bridge.codexBufferStop.mockResolvedValue(bufferPayload("stopped"));
    bridge.getStatus.mockResolvedValue(codexConfiguredStatus());
    bridge.codexBufferStatus.mockResolvedValue(bufferPayload("stopped"));

    ctrl = new SidebarController(provider);
    await ctrl.stopCodexBuffer();

    expect(bridge.codexBufferStop).toHaveBeenCalled();
    expect(bridge.getStatus).toHaveBeenCalled();
  });

  // ---- periodic refresh & visibility --------------------------------------

  test("periodic refresh fires when visible and skips when hidden", async () => {
    bridge.getStatus.mockResolvedValue(emptyStatus());

    ctrl = new SidebarController(provider, 1000);
    ctrl.attach();
    provider._setVisible(true);

    jest.advanceTimersByTime(1000);
    await Promise.resolve(); // flush
    expect(bridge.getStatus).toHaveBeenCalledTimes(1);

    bridge.getStatus.mockClear();
    provider._setVisible(false);

    jest.advanceTimersByTime(1000);
    await Promise.resolve();
    expect(bridge.getStatus).not.toHaveBeenCalled();
  });

  test("visibility change from false to true triggers immediate refresh", async () => {
    bridge.getStatus.mockResolvedValue(emptyStatus());

    ctrl = new SidebarController(provider);
    ctrl.attach();
    provider._setVisible(false);

    bridge.getStatus.mockClear();
    provider._fireVisibility(true);
    await Promise.resolve();

    expect(bridge.getStatus).toHaveBeenCalledTimes(1);
  });

  test("visibility change to false does NOT trigger refresh", async () => {
    bridge.getStatus.mockResolvedValue(emptyStatus());

    ctrl = new SidebarController(provider);
    ctrl.attach();

    bridge.getStatus.mockClear();
    provider._fireVisibility(false);
    await Promise.resolve();

    expect(bridge.getStatus).not.toHaveBeenCalled();
  });

  // ---- webview action forwarding ------------------------------------------

  test("attach() forwards onAction events to handleAction", async () => {
    bridge.getStatus.mockResolvedValue(emptyStatus());

    ctrl = new SidebarController(provider);
    ctrl.attach();

    provider._fireAction({ type: "refresh" });
    await Promise.resolve();

    expect(bridge.getStatus).toHaveBeenCalled();
  });

  // ---- uninstall failure --------------------------------------------------

  test("uninstall failure renders bridgeError", async () => {
    bridge.uninstall.mockResolvedValue({ success: false, error: "permission denied", harness: "codex", logs: [] });

    ctrl = new SidebarController(provider);
    await ctrl.handleAction({ type: "uninstall", harness: "codex" });

    const state = provider.render.mock.calls[0][0];
    expect(state.bridgeError).toBe("permission denied");
    // Should not refresh after failure
    expect(bridge.getStatus).not.toHaveBeenCalled();
  });

  test("uninstall bridge rejection renders bridgeError without crash", async () => {
    bridge.uninstall.mockRejectedValue(new Error("connection refused"));

    ctrl = new SidebarController(provider);
    await ctrl.handleAction({ type: "uninstall", harness: "codex" });

    const state = provider.render.mock.calls[0][0];
    expect(state.bridgeError).toBe("connection refused");
    expect(bridge.getStatus).not.toHaveBeenCalled();
  });

  test("stopCodexBuffer failure populates bridgeError", async () => {
    bridge.codexBufferStop.mockResolvedValue({ success: false, error: "not running", state: "unknown", host: null, port: null, pid: null });

    ctrl = new SidebarController(provider);
    await ctrl.stopCodexBuffer();

    const state = provider.render.mock.calls[0][0];
    expect(state.bridgeError).toBe("not running");
    expect(bridge.getStatus).not.toHaveBeenCalled();
  });

  test("handleAction startCodexBuffer dispatches to startCodexBuffer()", async () => {
    bridge.codexBufferStart.mockResolvedValue(bufferPayload("running"));
    bridge.getStatus.mockResolvedValue(codexConfiguredStatus());
    bridge.codexBufferStatus.mockResolvedValue(bufferPayload("running"));

    ctrl = new SidebarController(provider);
    await ctrl.handleAction({ type: "startCodexBuffer" });

    expect(bridge.codexBufferStart).toHaveBeenCalled();
    expect(bridge.getStatus).toHaveBeenCalled();
  });

  test("handleAction stopCodexBuffer dispatches to stopCodexBuffer()", async () => {
    bridge.codexBufferStop.mockResolvedValue(bufferPayload("stopped"));
    bridge.getStatus.mockResolvedValue(codexConfiguredStatus());
    bridge.codexBufferStatus.mockResolvedValue(bufferPayload("stopped"));

    ctrl = new SidebarController(provider);
    await ctrl.handleAction({ type: "stopCodexBuffer" });

    expect(bridge.codexBufferStop).toHaveBeenCalled();
    expect(bridge.getStatus).toHaveBeenCalled();
  });

  // ---- backendLabel mapping -----------------------------------------------

  test("backendLabel maps arize to 'Arize AX' and phoenix to 'Phoenix'", async () => {
    const status = emptyStatus();
    status.harnesses[0] = {
      name: "claude-code",
      configured: true,
      project_name: "proj",
      backend: { target: "arize", endpoint: "e", api_key: "k", space_id: null },
      scope: null,
    };
    status.harnesses[2] = {
      name: "cursor",
      configured: true,
      project_name: "proj2",
      backend: { target: "phoenix", endpoint: "e2", api_key: "k2", space_id: null },
      scope: null,
    };
    bridge.getStatus.mockResolvedValue(status);

    ctrl = new SidebarController(provider);
    await ctrl.refresh();

    const state = provider.render.mock.calls[0][0];
    expect(state.harnesses.find((h) => h.name === "claude-code").backendLabel).toBe("Arize AX");
    expect(state.harnesses.find((h) => h.name === "cursor").backendLabel).toBe("Phoenix");
    expect(state.harnesses.find((h) => h.name === "codex").backendLabel).toBeNull();
  });

  // ---- dispose cleans up --------------------------------------------------

  test("dispose clears interval and subscriptions", () => {
    ctrl = new SidebarController(provider, 500);
    ctrl.attach();
    ctrl.dispose();

    // After dispose, timer ticks should not trigger refresh
    bridge.getStatus.mockResolvedValue(emptyStatus());
    jest.advanceTimersByTime(1000);
    expect(bridge.getStatus).not.toHaveBeenCalled();
  });
});
