/**
 * Tests for src/installer.ts — the InstallerBridge abstraction over bridge.ts.
 */

// Mock the bridge module before importing anything.
jest.mock("../src/bridge", () => ({
  install: jest.fn(),
  uninstall: jest.fn(),
  getStatus: jest.fn(),
}));

const bridge = require("../src/bridge");
const { createBridgeInstaller } = require("../src/installer");

beforeEach(() => {
  jest.clearAllMocks();
});

// ---------------------------------------------------------------------------
// install
// ---------------------------------------------------------------------------

describe("InstallerBridge.install", () => {
  const REQ = {
    harness: "codex",
    backend: {
      target: "arize",
      endpoint: "otlp.arize.com:443",
      api_key: "secret",
      space_id: "sp123",
    },
    project_name: "my-project",
    user_id: null,
    with_skills: false,
    logging: null,
  };

  test("calls bridge.install with onLog and returns the result", async () => {
    const expectedResult = { success: true, error: null, harness: "codex", logs: ["done"] };
    bridge.install.mockImplementation(async (req, opts) => {
      // Simulate a log callback
      opts.onLog("info", "Installing codex...");
      return expectedResult;
    });

    const installer = createBridgeInstaller();
    const logs = [];
    const result = await installer.install(REQ, (level, msg) => logs.push({ level, msg }));

    expect(bridge.install).toHaveBeenCalledTimes(1);
    expect(bridge.install).toHaveBeenCalledWith(REQ, expect.objectContaining({ onLog: expect.any(Function) }));
    expect(result).toEqual(expectedResult);
    expect(logs).toEqual([{ level: "info", msg: "Installing codex..." }]);
  });

  test("passes abort signal through to bridge", async () => {
    const ac = new AbortController();
    bridge.install.mockImplementation(async (_req, opts) => {
      expect(opts.signal).toBe(ac.signal);
      return { success: true, error: null, harness: "codex", logs: [] };
    });

    const installer = createBridgeInstaller();
    await installer.install(REQ, () => {}, ac.signal);
    expect(bridge.install).toHaveBeenCalledTimes(1);
  });

  test("converts a thrown error into a failed OperationResult", async () => {
    bridge.install.mockRejectedValue(new Error("bridge: binary not found"));

    const installer = createBridgeInstaller();
    const result = await installer.install(REQ, () => {});

    expect(result).toEqual({
      success: false,
      error: "install_failed",
      harness: "codex",
      logs: [expect.stringContaining("bridge: binary not found")],
    });
  });
});

// ---------------------------------------------------------------------------
// uninstall
// ---------------------------------------------------------------------------

describe("InstallerBridge.uninstall", () => {
  test("calls bridge.uninstall and returns the result", async () => {
    const expectedResult = { success: true, error: null, harness: "cursor", logs: [] };
    bridge.uninstall.mockResolvedValue(expectedResult);

    const installer = createBridgeInstaller();
    const result = await installer.uninstall("cursor", () => {});

    expect(bridge.uninstall).toHaveBeenCalledWith("cursor", expect.objectContaining({ onLog: expect.any(Function) }));
    expect(result).toEqual(expectedResult);
  });

  test("converts a thrown error into a failed OperationResult", async () => {
    bridge.uninstall.mockRejectedValue(new Error("bridge: spawn error: ENOENT"));

    const installer = createBridgeInstaller();
    const result = await installer.uninstall("gemini", () => {});

    expect(result).toEqual({
      success: false,
      error: "uninstall_failed",
      harness: "gemini",
      logs: [expect.stringContaining("ENOENT")],
    });
  });
});

// ---------------------------------------------------------------------------
// loadStatus
// ---------------------------------------------------------------------------

describe("InstallerBridge.loadStatus", () => {
  test("calls bridge.getStatus and returns the payload", async () => {
    const payload = {
      success: true,
      error: null,
      user_id: "user1",
      harnesses: [],
      logging: null,
      codex_buffer: null,
    };
    bridge.getStatus.mockResolvedValue(payload);

    const installer = createBridgeInstaller();
    const result = await installer.loadStatus();

    expect(bridge.getStatus).toHaveBeenCalledTimes(1);
    expect(result).toEqual(payload);
  });

  test("propagates errors from bridge.getStatus", async () => {
    bridge.getStatus.mockRejectedValue(new Error("bridge: no result emitted"));

    const installer = createBridgeInstaller();
    await expect(installer.loadStatus()).rejects.toThrow("bridge: no result emitted");
  });
});
