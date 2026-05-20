/**
 * Tests for teardown.ts (teardownAll).
 */

// ── Mocks must be declared before any require/import of the mocked modules ──

jest.mock("../bridge", () => ({
  getStatus: jest.fn(),
  uninstall: jest.fn(),
  codexBufferStop: jest.fn(),
}));

const mockExistsSync = jest.fn();
const mockRm = jest.fn();
jest.mock("fs", () => ({
  existsSync: mockExistsSync,
  promises: {
    rm: mockRm,
  },
}));

import * as path from "path";
import * as os from "os";
import * as bridge from "../bridge";
import { teardownAll, TeardownOptions, TeardownResult } from "../teardown";
import type { StatusPayload, OperationResult } from "../types";

const mockGetStatus = bridge.getStatus as jest.MockedFunction<typeof bridge.getStatus>;
const mockUninstall = bridge.uninstall as jest.MockedFunction<typeof bridge.uninstall>;
const mockCodexBufferStop = bridge.codexBufferStop as jest.MockedFunction<typeof bridge.codexBufferStop>;

// ── Helpers ──────────────────────────────────────────────────────────

const VENV_DIR = path.join(os.homedir(), ".arize", "harness", "venv");

function makeStatus(overrides: Partial<StatusPayload> = {}): StatusPayload {
  return {
    success: true,
    error: null,
    user_id: null,
    harnesses: [
      { name: "claude-code", configured: false, project_name: null, backend: null, scope: null, kiro_options: null, repo_paths: null },
      { name: "codex", configured: false, project_name: null, backend: null, scope: null, kiro_options: null, repo_paths: null },
      { name: "cursor", configured: false, project_name: null, backend: null, scope: null, kiro_options: null, repo_paths: null },
      { name: "copilot", configured: false, project_name: null, backend: null, scope: null, kiro_options: null, repo_paths: null },
      { name: "gemini", configured: false, project_name: null, backend: null, scope: null, kiro_options: null, repo_paths: null },
    ],
    logging: null,
    codex_buffer: null,
    ...overrides,
  };
}

function successResult(harness: string): OperationResult {
  return { success: true, error: null, harness, logs: [] };
}

function failureResult(harness: string, error: string): OperationResult {
  return { success: false, error, harness, logs: [] };
}

// ── Reset ────────────────────────────────────────────────────────────

beforeEach(() => {
  // resetAllMocks (vs clearAllMocks) drains *Once queues so a test that fails
  // mid-flow can't leak unconsumed mocks into the next test.
  jest.resetAllMocks();
  mockExistsSync.mockReturnValue(false);
  mockRm.mockResolvedValue(undefined);
});

// ── Tests ────────────────────────────────────────────────────────────

describe("teardownAll", () => {
  it("when no harnesses are configured, every entry is 'skipped' and fs.promises.rm is called once with correct options", async () => {
    mockGetStatus.mockResolvedValueOnce(makeStatus());
    mockExistsSync.mockReturnValue(true); // venv exists

    const result = await teardownAll({});

    expect(result.ok).toBe(true);
    expect(result.harnesses).toHaveLength(6);
    for (const h of result.harnesses) {
      expect(h.state).toBe("skipped");
    }
    expect(mockUninstall).not.toHaveBeenCalled();
    expect(mockRm).toHaveBeenCalledTimes(1);
    expect(mockRm).toHaveBeenCalledWith(VENV_DIR, {
      recursive: true,
      force: true,
      maxRetries: 5,
      retryDelay: 200,
    });
    expect(result.venvRemoved).toBe(true);
  });

  it("when bridge.getStatus() rejects, all entries are 'skipped' and venv removal still runs", async () => {
    mockGetStatus.mockRejectedValueOnce(new Error("bridge: binary not found"));
    mockExistsSync.mockReturnValue(true); // venv exists

    const result = await teardownAll({});

    expect(result.ok).toBe(true);
    expect(result.harnesses).toHaveLength(6);
    for (const h of result.harnesses) {
      expect(h.state).toBe("skipped");
    }
    expect(mockUninstall).not.toHaveBeenCalled();
    expect(mockRm).toHaveBeenCalledTimes(1);
    expect(result.venvRemoved).toBe(true);
  });

  it("when two harnesses are configured and the first uninstall fails, result records failure and continues", async () => {
    const status = makeStatus({
      harnesses: [
        { name: "claude-code", configured: true, project_name: "test", backend: null, scope: null, kiro_options: null, repo_paths: null },
        { name: "codex", configured: true, project_name: "test", backend: null, scope: null, kiro_options: null, repo_paths: null },
        { name: "cursor", configured: false, project_name: null, backend: null, scope: null, kiro_options: null, repo_paths: null },
        { name: "copilot", configured: false, project_name: null, backend: null, scope: null, kiro_options: null, repo_paths: null },
        { name: "gemini", configured: false, project_name: null, backend: null, scope: null, kiro_options: null, repo_paths: null },
      ],
    });
    mockGetStatus.mockResolvedValueOnce(status);
    mockUninstall
      .mockResolvedValueOnce(failureResult("claude-code", "Settings file locked"))
      .mockResolvedValueOnce(successResult("codex"));
    mockExistsSync.mockReturnValue(true);

    const result = await teardownAll({});

    expect(result.ok).toBe(false); // failed harness
    expect(result.harnesses[0]).toEqual({
      harness: "claude-code",
      state: "failed",
      error: "Settings file locked",
    });
    expect(result.harnesses[1]).toEqual({
      harness: "codex",
      state: "uninstalled",
    });
    expect(result.harnesses[2]).toEqual({ harness: "cursor", state: "skipped" });
    expect(result.venvRemoved).toBe(true);
    expect(mockRm).toHaveBeenCalledTimes(1);
  });

  it("when removeVenv is false and venv exists, fs.promises.rm is not called and venvRemoved is false", async () => {
    mockGetStatus.mockResolvedValueOnce(makeStatus());
    mockExistsSync.mockReturnValue(true); // venv exists

    const result = await teardownAll({ removeVenv: false });

    expect(mockRm).not.toHaveBeenCalled();
    expect(result.venvRemoved).toBe(false);
  });

  it("when removeVenv is false and venv does not exist, venvRemoved is true", async () => {
    mockGetStatus.mockResolvedValueOnce(makeStatus());
    mockExistsSync.mockReturnValue(false); // venv absent

    const result = await teardownAll({ removeVenv: false });

    expect(mockRm).not.toHaveBeenCalled();
    expect(result.venvRemoved).toBe(true);
  });

  it("on Windows, venv path uses os.homedir() and maxRetries: 5 is passed to fs.promises.rm", async () => {
    const ORIGINAL_PLATFORM = process.platform;
    Object.defineProperty(process, "platform", { value: "win32" });

    try {
      mockGetStatus.mockResolvedValueOnce(makeStatus());
      mockExistsSync.mockReturnValue(true);

      await teardownAll({});

      expect(mockRm).toHaveBeenCalledWith(
        path.join(os.homedir(), ".arize", "harness", "venv"),
        expect.objectContaining({ maxRetries: 5 }),
      );
    } finally {
      Object.defineProperty(process, "platform", { value: ORIGINAL_PLATFORM });
    }
  });

  it("when signal is aborted mid-teardown, in-flight bridge.uninstall receives the same signal and result reflects partial state", async () => {
    const ac = new AbortController();

    const status = makeStatus({
      harnesses: [
        { name: "claude-code", configured: true, project_name: "test", backend: null, scope: null, kiro_options: null, repo_paths: null },
        { name: "codex", configured: true, project_name: "test", backend: null, scope: null, kiro_options: null, repo_paths: null },
        { name: "cursor", configured: false, project_name: null, backend: null, scope: null, kiro_options: null, repo_paths: null },
        { name: "copilot", configured: false, project_name: null, backend: null, scope: null, kiro_options: null, repo_paths: null },
        { name: "gemini", configured: false, project_name: null, backend: null, scope: null, kiro_options: null, repo_paths: null },
      ],
    });
    mockGetStatus.mockResolvedValueOnce(status);

    // First uninstall succeeds, then we abort before the second completes.
    mockUninstall
      .mockImplementationOnce(async (_harness, opts) => {
        // After first uninstall, abort the signal.
        ac.abort();
        return successResult("claude-code");
      })
      .mockImplementationOnce(async (_harness, _opts) => {
        // This should still be called since signal check happens at loop start.
        return successResult("codex");
      });
    mockExistsSync.mockReturnValue(true); // venv exists but should not be removed due to abort

    const result = await teardownAll({ signal: ac.signal });

    // Verify signal was passed to bridge.uninstall
    expect(mockUninstall).toHaveBeenCalledWith(
      "claude-code",
      expect.objectContaining({ signal: ac.signal }),
    );
    // claude-code was uninstalled before abort
    expect(result.harnesses[0].state).toBe("uninstalled");
    // codex should be skipped because signal was aborted before its iteration
    expect(result.harnesses[1].state).toBe("skipped");
    // Venv removal should be skipped due to abort — venv still exists
    expect(mockRm).not.toHaveBeenCalled();
    expect(result.venvRemoved).toBe(false); // venv exists but not removed
    // Result should not throw.
  });

  it("when venv removal fails, venvRemoved is false and venvError is populated", async () => {
    mockGetStatus.mockResolvedValueOnce(makeStatus());
    mockExistsSync.mockReturnValue(true);
    mockRm.mockRejectedValueOnce(new Error("EBUSY: resource busy"));

    const result = await teardownAll({});

    expect(result.ok).toBe(false);
    expect(result.venvRemoved).toBe(false);
    expect(result.venvError).toBe("EBUSY: resource busy");
  });

  it("when venv does not exist and removeVenv is true, venvRemoved is true without calling rm", async () => {
    mockGetStatus.mockResolvedValueOnce(makeStatus());
    mockExistsSync.mockReturnValue(false);

    const result = await teardownAll({});

    expect(result.venvRemoved).toBe(true);
    expect(mockRm).not.toHaveBeenCalled();
  });

  it("calls codexBufferStop when codex buffer state is running", async () => {
    const status = makeStatus({
      codex_buffer: {
        success: true,
        error: null,
        state: "running",
        host: "localhost",
        port: 8080,
        pid: 1234,
      },
      harnesses: [
        { name: "claude-code", configured: false, project_name: null, backend: null, scope: null, kiro_options: null, repo_paths: null },
        { name: "codex", configured: true, project_name: "test", backend: null, scope: null, kiro_options: null, repo_paths: null },
        { name: "cursor", configured: false, project_name: null, backend: null, scope: null, kiro_options: null, repo_paths: null },
        { name: "copilot", configured: false, project_name: null, backend: null, scope: null, kiro_options: null, repo_paths: null },
        { name: "gemini", configured: false, project_name: null, backend: null, scope: null, kiro_options: null, repo_paths: null },
      ],
    });
    mockGetStatus.mockResolvedValueOnce(status);
    mockUninstall.mockResolvedValueOnce(successResult("codex"));
    mockCodexBufferStop.mockResolvedValueOnce({
      success: true,
      error: null,
      state: "stopped",
      host: null,
      port: null,
      pid: null,
    });
    mockExistsSync.mockReturnValue(false);

    await teardownAll({});

    expect(mockCodexBufferStop).toHaveBeenCalledTimes(1);
  });

  it("does not call codexBufferStop when codex buffer is not running", async () => {
    mockGetStatus.mockResolvedValueOnce(makeStatus());
    mockExistsSync.mockReturnValue(false);

    await teardownAll({});

    expect(mockCodexBufferStop).not.toHaveBeenCalled();
  });

  it("pipes onLog messages", async () => {
    mockGetStatus.mockResolvedValueOnce(makeStatus());
    mockExistsSync.mockReturnValue(false);

    const logs: Array<{ level: string; message: string }> = [];
    await teardownAll({
      onLog: (level, message) => logs.push({ level, message }),
    });

    expect(logs.length).toBeGreaterThan(0);
    expect(logs).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ level: "info" }),
      ]),
    );
  });

  it("when bridge.uninstall throws, records the harness as failed and continues", async () => {
    const status = makeStatus({
      harnesses: [
        { name: "claude-code", configured: true, project_name: "test", backend: null, scope: null, kiro_options: null, repo_paths: null },
        { name: "codex", configured: true, project_name: "test", backend: null, scope: null, kiro_options: null, repo_paths: null },
        { name: "cursor", configured: false, project_name: null, backend: null, scope: null, kiro_options: null, repo_paths: null },
        { name: "copilot", configured: false, project_name: null, backend: null, scope: null, kiro_options: null, repo_paths: null },
        { name: "gemini", configured: false, project_name: null, backend: null, scope: null, kiro_options: null, repo_paths: null },
      ],
    });
    mockGetStatus.mockResolvedValueOnce(status);
    mockUninstall
      .mockRejectedValueOnce(new Error("connection refused"))
      .mockResolvedValueOnce(successResult("codex"));
    mockExistsSync.mockReturnValue(false);

    const result = await teardownAll({});

    expect(result.harnesses[0]).toEqual({
      harness: "claude-code",
      state: "failed",
      error: "connection refused",
    });
    expect(result.harnesses[1]).toEqual({
      harness: "codex",
      state: "uninstalled",
    });
    expect(result.ok).toBe(false);
  });
});
