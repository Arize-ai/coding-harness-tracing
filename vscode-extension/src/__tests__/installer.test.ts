/**
 * Tests for installer.ts → bridge.install argv forwarding.
 *
 * Specifically: verify that Kiro-specific options (--agent-name and
 * --set-default) are forwarded to the bridge CLI when, and only when,
 * the harness is "kiro" and kiro_options is populated.
 */

// ── Mocks must be declared before any require/import of the mocked modules ──

jest.mock("../python", () => ({
  findPython: jest.fn(),
  findBridgeBinary: jest.fn(),
}));

const mockSpawn = jest.fn();
jest.mock("child_process", () => ({
  spawn: mockSpawn,
  execFile: jest.fn(),
}));

import { EventEmitter } from "events";
import { findBridgeBinary } from "../python";
import { createBridgeInstaller } from "../installer";
import type { InstallRequest } from "../types";

const mockFindBridgeBinary = findBridgeBinary as jest.MockedFunction<
  typeof findBridgeBinary
>;

// ── Helpers ──────────────────────────────────────────────────────────

const BRIDGE_PATH = "/home/user/.arize/harness/venv/bin/arize-vscode-bridge";

/**
 * Wire up a fake child process that returns a successful OperationResult
 * via the bridge's NDJSON protocol.
 */
function fakeBridgeSpawn(): void {
  const child = new EventEmitter() as any;
  child.stdout = new EventEmitter();
  child.stderr = new EventEmitter();
  child.kill = jest.fn();
  child.stdin = null;

  mockSpawn.mockReturnValueOnce(child);

  setImmediate(() => {
    const result = {
      event: "result",
      payload: {
        success: true,
        error: null,
        harness: "kiro",
        logs: [],
      },
    };
    child.stdout.emit("data", Buffer.from(JSON.stringify(result) + "\n"));
    child.emit("close", 0);
  });
}

function makeKiroRequest(
  overrides: Partial<InstallRequest> = {},
): InstallRequest {
  return {
    harness: "kiro",
    backend: {
      target: "arize",
      endpoint: "https://otlp.arize.com/v1",
      api_key: "ak-123",
      space_id: "sp-1",
    },
    project_name: "demo",
    user_id: null,
    with_skills: false,
    logging: null,
    kiro_options: { agent_name: "arize-traced", set_default: false },
    repo_path: null,
    ...overrides,
  };
}

function makeCodexRequest(
  overrides: Partial<InstallRequest> = {},
): InstallRequest {
  return {
    harness: "codex",
    backend: {
      target: "arize",
      endpoint: "https://otlp.arize.com/v1",
      api_key: "ak-123",
      space_id: "sp-1",
    },
    project_name: "demo",
    user_id: null,
    with_skills: false,
    logging: null,
    kiro_options: null,
    repo_path: null,
    ...overrides,
  };
}

/** Pull the argv array from the most recent spawn call. */
function lastSpawnArgs(): string[] {
  expect(mockSpawn).toHaveBeenCalled();
  const call = mockSpawn.mock.calls[mockSpawn.mock.calls.length - 1];
  return call[1] as string[];
}

// ── Reset ────────────────────────────────────────────────────────────

beforeEach(() => {
  jest.resetAllMocks();
  mockFindBridgeBinary.mockResolvedValue(BRIDGE_PATH);
});

// ── Tests ────────────────────────────────────────────────────────────

describe("installer.install argv forwarding", () => {
  it("forwards --agent-name when kiro_options present", async () => {
    fakeBridgeSpawn();

    const installer = createBridgeInstaller();
    const req = makeKiroRequest({
      kiro_options: { agent_name: "my-agent", set_default: false },
    });
    const result = await installer.install(req, () => {});

    expect(result.success).toBe(true);
    const args = lastSpawnArgs();
    // --agent-name forwarded with the chosen value
    const agentIdx = args.indexOf("--agent-name");
    expect(agentIdx).toBeGreaterThan(-1);
    expect(args[agentIdx + 1]).toBe("my-agent");
    // set_default is false → flag must NOT be present
    expect(args).not.toContain("--set-default");
  });

  it("forwards --set-default when set_default is true", async () => {
    fakeBridgeSpawn();

    const installer = createBridgeInstaller();
    const req = makeKiroRequest({
      kiro_options: { agent_name: "x", set_default: true },
    });
    const result = await installer.install(req, () => {});

    expect(result.success).toBe(true);
    const args = lastSpawnArgs();
    const agentIdx = args.indexOf("--agent-name");
    expect(agentIdx).toBeGreaterThan(-1);
    expect(args[agentIdx + 1]).toBe("x");
    expect(args).toContain("--set-default");
  });

  it("omits kiro flags when kiro_options is null", async () => {
    fakeBridgeSpawn();

    const installer = createBridgeInstaller();
    const req = makeKiroRequest({ kiro_options: null });
    const result = await installer.install(req, () => {});

    expect(result.success).toBe(true);
    const args = lastSpawnArgs();
    expect(args).not.toContain("--agent-name");
    expect(args).not.toContain("--set-default");
  });

  it("omits kiro flags for non-kiro harness even when kiro_options is set", async () => {
    fakeBridgeSpawn();

    const installer = createBridgeInstaller();
    // Defensive: a codex request that somehow carries kiro_options.
    const req = makeCodexRequest({
      kiro_options: { agent_name: "my-agent", set_default: true },
    });
    const result = await installer.install(req, () => {});

    expect(result.success).toBe(true);
    const args = lastSpawnArgs();
    expect(args).not.toContain("--agent-name");
    expect(args).not.toContain("--set-default");
  });
});
