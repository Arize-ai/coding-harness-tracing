/**
 * Tests for status.ts (deriveStatus) and statusBar.ts (StatusBarManager, registerStatusBarMenuCommand).
 */

import {
  DerivedState,
  deriveStatus,
  DeriveStatusInput,
} from "../status";
import type { StatusPayload, CodexBufferPayload, HarnessStatusItem } from "../types";
import { HARNESS_KEYS } from "../types";

// ── Helpers ───────────────────────────────────────────────────────────

function makeHarness(
  name: string,
  configured: boolean,
): HarnessStatusItem {
  return {
    name: name as HarnessStatusItem["name"],
    configured,
    project_name: configured ? "my-project" : null,
    backend: configured
      ? { target: "arize", endpoint: "https://arize.com", api_key: "key", space_id: null }
      : null,
    scope: null,
    kiro_options: null,
    repo_paths: null,
  };
}

function makeStatusPayload(
  overrides: Partial<StatusPayload> = {},
): StatusPayload {
  return {
    success: true,
    error: null,
    user_id: "u1",
    harnesses: HARNESS_KEYS.map((k) => makeHarness(k, false)),
    logging: null,
    codex_buffer: null,
    ...overrides,
  };
}

function makeCodexBuffer(
  state: CodexBufferPayload["state"] = "running",
): CodexBufferPayload {
  return {
    success: true,
    error: null,
    state,
    host: "127.0.0.1",
    port: 9090,
    pid: 12345,
  };
}

// ── deriveStatus tests ────────────────────────────────────────────────

describe("deriveStatus", () => {
  it("returns PythonMissing when pythonFound is false", () => {
    const result = deriveStatus({
      pythonFound: false,
      bridgeFound: true,
      status: makeStatusPayload(),
      codexBuffer: null,
      bridgeError: null,
    });
    expect(result.state).toBe(DerivedState.PythonMissing);
    expect(result.configuredCount).toBe(0);
    expect(result.totalCount).toBe(HARNESS_KEYS.length);
    expect(result.errorMessage).toBeNull();
  });

  it("returns BridgeMissing when pythonFound but bridgeFound is false", () => {
    const result = deriveStatus({
      pythonFound: true,
      bridgeFound: false,
      status: makeStatusPayload(),
      codexBuffer: null,
      bridgeError: null,
    });
    expect(result.state).toBe(DerivedState.BridgeMissing);
    expect(result.configuredCount).toBe(0);
    expect(result.errorMessage).toBeNull();
  });

  it("returns BridgeError with errorMessage from bridgeError arg", () => {
    const result = deriveStatus({
      pythonFound: true,
      bridgeFound: true,
      status: null,
      codexBuffer: null,
      bridgeError: "spawn failed",
    });
    expect(result.state).toBe(DerivedState.BridgeError);
    expect(result.errorMessage).toBe("spawn failed");
    expect(result.configuredCount).toBe(0);
  });

  it("returns BridgeError when status is null and bridgeError is null", () => {
    const result = deriveStatus({
      pythonFound: true,
      bridgeFound: true,
      status: null,
      codexBuffer: null,
      bridgeError: null,
    });
    expect(result.state).toBe(DerivedState.BridgeError);
    expect(result.errorMessage).toBe("unknown_error");
  });

  it("returns BridgeError when status.success is false", () => {
    const status = makeStatusPayload({ success: false, error: "config parse error" });
    const result = deriveStatus({
      pythonFound: true,
      bridgeFound: true,
      status,
      codexBuffer: null,
      bridgeError: null,
    });
    expect(result.state).toBe(DerivedState.BridgeError);
    expect(result.errorMessage).toBe("config parse error");
  });

  it("returns BridgeError with 'unknown_error' when status.success is false and error is null", () => {
    const status = makeStatusPayload({ success: false, error: null });
    const result = deriveStatus({
      pythonFound: true,
      bridgeFound: true,
      status,
      codexBuffer: null,
      bridgeError: null,
    });
    expect(result.state).toBe(DerivedState.BridgeError);
    expect(result.errorMessage).toBe("unknown_error");
  });

  it("returns NoHarnesses when all harnesses are unconfigured", () => {
    const result = deriveStatus({
      pythonFound: true,
      bridgeFound: true,
      status: makeStatusPayload(),
      codexBuffer: null,
      bridgeError: null,
    });
    expect(result.state).toBe(DerivedState.NoHarnesses);
    expect(result.configuredCount).toBe(0);
    expect(result.errorMessage).toBeNull();
  });

  it("returns Configured with correct count", () => {
    const harnesses = HARNESS_KEYS.map((k) =>
      makeHarness(k, k === "claude-code" || k === "cursor"),
    );
    const status = makeStatusPayload({ harnesses });
    const result = deriveStatus({
      pythonFound: true,
      bridgeFound: true,
      status,
      codexBuffer: null,
      bridgeError: null,
    });
    expect(result.state).toBe(DerivedState.Configured);
    expect(result.configuredCount).toBe(2);
    expect(result.totalCount).toBe(HARNESS_KEYS.length);
    expect(result.errorMessage).toBeNull();
  });

  it("passes through codexBuffer from input", () => {
    const buf = makeCodexBuffer("stopped");
    const harnesses = HARNESS_KEYS.map((k) =>
      makeHarness(k, k === "codex"),
    );
    const result = deriveStatus({
      pythonFound: true,
      bridgeFound: true,
      status: makeStatusPayload({ harnesses }),
      codexBuffer: buf,
      bridgeError: null,
    });
    expect(result.codexBuffer).toBe(buf);
  });

  it("PythonMissing takes priority over bridgeError", () => {
    const result = deriveStatus({
      pythonFound: false,
      bridgeFound: false,
      status: null,
      codexBuffer: null,
      bridgeError: "some error",
    });
    expect(result.state).toBe(DerivedState.PythonMissing);
  });

  it("BridgeMissing takes priority over bridgeError", () => {
    const result = deriveStatus({
      pythonFound: true,
      bridgeFound: false,
      status: null,
      codexBuffer: null,
      bridgeError: "some error",
    });
    expect(result.state).toBe(DerivedState.BridgeMissing);
  });

  it("bridgeError takes priority over status null", () => {
    const result = deriveStatus({
      pythonFound: true,
      bridgeFound: true,
      status: null,
      codexBuffer: null,
      bridgeError: "timeout",
    });
    expect(result.state).toBe(DerivedState.BridgeError);
    expect(result.errorMessage).toBe("timeout");
  });
});

// ── StatusBarManager tests ────────────────────────────────────────────

// Mock bridge and python modules
jest.mock("../bridge");
jest.mock("../python");

import { StatusBarManager, registerStatusBarMenuCommand } from "../statusBar";
import * as bridgeMod from "../bridge";
import * as pythonMod from "../python";
import * as vscode from "vscode";

const mockBridge = bridgeMod as jest.Mocked<typeof bridgeMod>;
const mockPython = pythonMod as jest.Mocked<typeof pythonMod>;

describe("StatusBarManager", () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it("refresh() calls bridge.getStatus and updates current", async () => {
    const harnesses = HARNESS_KEYS.map((k) =>
      makeHarness(k, k === "claude-code"),
    );
    const status = makeStatusPayload({ harnesses });

    mockPython.findPython.mockResolvedValue("/usr/bin/python3");
    mockPython.findBridgeBinary.mockResolvedValue("/usr/bin/arize-vscode-bridge");
    mockBridge.getStatus.mockResolvedValue(status);

    const mgr = new StatusBarManager();
    await mgr.refresh();

    expect(mockBridge.getStatus).toHaveBeenCalled();
    expect(mgr.current.state).toBe(DerivedState.Configured);
    expect(mgr.current.configuredCount).toBe(1);

    mgr.dispose();
  });

  it("refresh() calls bridge.codexBufferStatus when codex is configured", async () => {
    const harnesses = HARNESS_KEYS.map((k) =>
      makeHarness(k, k === "codex"),
    );
    const status = makeStatusPayload({ harnesses });
    const buf = makeCodexBuffer("running");

    mockPython.findPython.mockResolvedValue("/usr/bin/python3");
    mockPython.findBridgeBinary.mockResolvedValue("/usr/bin/arize-vscode-bridge");
    mockBridge.getStatus.mockResolvedValue(status);
    mockBridge.codexBufferStatus.mockResolvedValue(buf);

    const mgr = new StatusBarManager();
    await mgr.refresh();

    expect(mockBridge.codexBufferStatus).toHaveBeenCalled();
    expect(mgr.current.codexBuffer).toBe(buf);

    mgr.dispose();
  });

  it("does not fetch codexBuffer when codex is not configured", async () => {
    const harnesses = HARNESS_KEYS.map((k) =>
      makeHarness(k, k === "claude-code"),
    );
    const status = makeStatusPayload({ harnesses });

    mockPython.findPython.mockResolvedValue("/usr/bin/python3");
    mockPython.findBridgeBinary.mockResolvedValue("/usr/bin/arize-vscode-bridge");
    mockBridge.getStatus.mockResolvedValue(status);

    const mgr = new StatusBarManager();
    await mgr.refresh();

    expect(mockBridge.codexBufferStatus).not.toHaveBeenCalled();
    expect(mgr.current.codexBuffer).toBeNull();

    mgr.dispose();
  });

  it("refresh() sets BridgeError when getStatus throws", async () => {
    mockPython.findPython.mockResolvedValue("/usr/bin/python3");
    mockPython.findBridgeBinary.mockResolvedValue("/usr/bin/arize-vscode-bridge");
    mockBridge.getStatus.mockRejectedValue(new Error("bridge: no result emitted"));

    const mgr = new StatusBarManager();
    await mgr.refresh();

    expect(mgr.current.state).toBe(DerivedState.BridgeError);
    expect(mgr.current.errorMessage).toBe("bridge: no result emitted");

    mgr.dispose();
  });

  it("refresh() sets PythonMissing when findPython returns null", async () => {
    mockPython.findPython.mockResolvedValue(null);

    const mgr = new StatusBarManager();
    await mgr.refresh();

    expect(mgr.current.state).toBe(DerivedState.PythonMissing);
    expect(mockBridge.getStatus).not.toHaveBeenCalled();

    mgr.dispose();
  });

  it("refresh() sets BridgeMissing when findBridgeBinary returns null", async () => {
    mockPython.findPython.mockResolvedValue("/usr/bin/python3");
    mockPython.findBridgeBinary.mockResolvedValue(null);

    const mgr = new StatusBarManager();
    await mgr.refresh();

    expect(mgr.current.state).toBe(DerivedState.BridgeMissing);
    expect(mockBridge.getStatus).not.toHaveBeenCalled();

    mgr.dispose();
  });
});

// ── registerStatusBarMenuCommand tests ────────────────────────────────

describe("registerStatusBarMenuCommand", () => {
  it("registers a single command with id arize.statusBarMenu", () => {
    const subscriptions: { dispose: () => void }[] = [];
    const ctx = { subscriptions } as unknown as vscode.ExtensionContext;
    const mgr = new StatusBarManager();

    registerStatusBarMenuCommand(ctx, mgr);

    expect(vscode.commands.registerCommand).toHaveBeenCalledWith(
      "arize.statusBarMenu",
      expect.any(Function),
    );
    expect(subscriptions.length).toBe(1);

    mgr.dispose();
  });
});
