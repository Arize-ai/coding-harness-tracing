/**
 * Tests for src/bridge.ts and src/python.ts
 *
 * Mocks child_process.spawn and fs/os helpers to exercise the NDJSON
 * framing logic, error handling, and abort support without a real bridge.
 */

const { EventEmitter } = require("events");
const { Readable } = require("stream");
const path = require("path");
const os = require("os");

// ── helpers to build a fake child process ──────────────────────────────

function fakeChild() {
  const child = new EventEmitter();
  child.stdout = new Readable({ read() {} });
  child.stderr = new Readable({ read() {} });
  child.kill = jest.fn();
  child.stdin = null;
  return child;
}

function pushLines(stream, ...lines) {
  for (const l of lines) {
    stream.push(l + "\n");
  }
}

// ── mock child_process.spawn ───────────────────────────────────────────

let nextChild;
jest.mock("child_process", () => ({
  ...jest.requireActual("child_process"),
  spawn: jest.fn(() => nextChild),
  execFile: jest.fn((_cmd, _args, _opts, cb) => {
    // Default: command not found
    cb(new Error("not found"), "", "");
  }),
}));

// ── mock fs.existsSync (for findBridgeBinary / findPython) ────────────

let existsSyncResults = {};
jest.mock("fs", () => {
  const actual = jest.requireActual("fs");
  return {
    ...actual,
    existsSync: jest.fn((p) => {
      if (typeof existsSyncResults === "function") return existsSyncResults(p);
      return !!existsSyncResults[p];
    }),
  };
});

// ── import modules under test AFTER mocks are in place ─────────────────

const { findPython, findBridgeBinary, checkVenvExists } = require("../src/python");
const {
  getStatus,
  install,
  uninstall,
  codexBufferStatus,
  codexBufferStart,
  codexBufferStop,
} = require("../src/bridge");
const cp = require("child_process");
const fs = require("fs");

// ── setup / teardown ───────────────────────────────────────────────────

beforeEach(() => {
  jest.clearAllMocks();
  existsSyncResults = {};
  nextChild = undefined;
});

/**
 * Yield ticks until cp.spawn has been invoked. runBridge awaits
 * findBridgeBinary() before spawning, so tests must wait for that microtask
 * to resolve before pushing data into the fake child's streams — otherwise
 * 'close' events fire before listeners are attached and the promise hangs.
 */
async function awaitSpawn() {
  for (let i = 0; i < 50 && cp.spawn.mock.calls.length === 0; i++) {
    await new Promise((r) => setImmediate(r));
  }
}

// ═══════════════════════════════════════════════════════════════════════
// bridge.ts tests
// ═══════════════════════════════════════════════════════════════════════

describe("bridge client", () => {
  const IS_WIN = os.platform() === "win32";
  const VENV_BIN = IS_WIN
    ? path.join(os.homedir(), ".arize", "harness", "venv", "Scripts")
    : path.join(os.homedir(), ".arize", "harness", "venv", "bin");
  const BRIDGE_PATH = path.join(
    VENV_BIN,
    IS_WIN ? "arize-vscode-bridge.exe" : "arize-vscode-bridge"
  );

  /** Make findBridgeBinary resolve to BRIDGE_PATH. */
  function stubBridgeExists() {
    existsSyncResults[BRIDGE_PATH] = true;
  }

  // ── getStatus ────────────────────────────────────────────────────────

  test("getStatus: single result event resolves with payload", async () => {
    stubBridgeExists();
    const child = fakeChild();
    nextChild = child;

    const p = getStatus();
    await awaitSpawn();

    const payload = {
      success: true,
      error: null,
      user_id: "u1",
      harnesses: [],
      logging: null,
      codex_buffer: null,
    };
    pushLines(child.stdout, JSON.stringify({ event: "result", payload }));
    child.stdout.push(null);
    child.emit("close", 0);

    await expect(p).resolves.toEqual(payload);
    expect(cp.spawn).toHaveBeenCalledWith(
      BRIDGE_PATH,
      ["status"],
      expect.objectContaining({ stdio: ["ignore", "pipe", "pipe"] })
    );
  });

  // ── install with log events ──────────────────────────────────────────

  test("install: log events delivered in order, then result", async () => {
    stubBridgeExists();
    const child = fakeChild();
    nextChild = child;

    const logs = [];
    const onLog = (level, message) => logs.push({ level, message });

    const p = install(
      {
        harness: "claude-code",
        backend: {
          target: "arize",
          endpoint: "https://otlp.arize.com/v1",
          api_key: "key123",
          space_id: "sp1",
        },
        project_name: "proj",
        user_id: "uid",
        with_skills: true,
        logging: { prompts: true, tool_details: false, tool_content: true },
      },
      { onLog }
    );
    await awaitSpawn();

    pushLines(
      child.stdout,
      JSON.stringify({ event: "log", level: "info", message: "step 1" }),
      JSON.stringify({ event: "log", level: "info", message: "step 2" }),
      JSON.stringify({
        event: "result",
        payload: { success: true, error: null, harness: "claude-code", logs: ["done"] },
      })
    );
    child.stdout.push(null);
    child.emit("close", 0);

    const result = await p;
    expect(result.success).toBe(true);
    expect(logs).toEqual([
      { level: "info", message: "step 1" },
      { level: "info", message: "step 2" },
    ]);

    // Verify argv includes all flags
    const argv = cp.spawn.mock.calls[0][1];
    expect(argv).toContain("--harness");
    expect(argv).toContain("claude-code");
    expect(argv).toContain("--space-id");
    expect(argv).toContain("sp1");
    expect(argv).toContain("--user-id");
    expect(argv).toContain("uid");
    expect(argv).toContain("--with-skills");
    expect(argv).toContain("--log-prompts");
    expect(argv).toContain("true");
    expect(argv).toContain("--log-tool-content");
  });

  // ── install passes --repo-path ───────────────────────────────────────

  test("install passes --repo-path when set", async () => {
    stubBridgeExists();
    const child = fakeChild();
    nextChild = child;

    const p = install({
      harness: "copilot",
      backend: {
        target: "arize",
        endpoint: "https://otlp.arize.com/v1",
        api_key: "key123",
        space_id: "sp1",
      },
      project_name: "proj",
      user_id: null,
      with_skills: false,
      logging: null,
      kiro_options: null,
      repo_path: "/repo/a",
    });
    await awaitSpawn();

    pushLines(
      child.stdout,
      JSON.stringify({
        event: "result",
        payload: { success: true, error: null, harness: "copilot", logs: [] },
      })
    );
    child.stdout.push(null);
    child.emit("close", 0);

    await p;

    const argv = cp.spawn.mock.calls[0][1];
    const flagIdx = argv.indexOf("--repo-path");
    expect(flagIdx).toBeGreaterThan(-1);
    expect(argv[flagIdx + 1]).toBe("/repo/a");
  });

  test("install omits --repo-path when null/undefined", async () => {
    stubBridgeExists();
    const child = fakeChild();
    nextChild = child;

    const p = install({
      harness: "copilot",
      backend: {
        target: "arize",
        endpoint: "https://otlp.arize.com/v1",
        api_key: "key123",
        space_id: "sp1",
      },
      project_name: "proj",
      user_id: null,
      with_skills: false,
      logging: null,
      kiro_options: null,
      repo_path: null,
    });
    await awaitSpawn();

    pushLines(
      child.stdout,
      JSON.stringify({
        event: "result",
        payload: { success: true, error: null, harness: "copilot", logs: [] },
      })
    );
    child.stdout.push(null);
    child.emit("close", 0);

    await p;

    const argv = cp.spawn.mock.calls[0][1];
    expect(argv).not.toContain("--repo-path");
  });

  // ── uninstall with success=false ─────────────────────────────────────

  test("uninstall: result with success=false resolves (does not throw)", async () => {
    stubBridgeExists();
    const child = fakeChild();
    nextChild = child;

    const p = uninstall("cursor");
    await awaitSpawn();

    pushLines(
      child.stdout,
      JSON.stringify({
        event: "result",
        payload: { success: false, error: "not configured", harness: "cursor", logs: [] },
      })
    );
    child.stdout.push(null);
    child.emit("close", 1);

    const result = await p;
    expect(result.success).toBe(false);
    expect(result.error).toBe("not configured");
  });

  // ── bad JSON line ────────────────────────────────────────────────────

  test("bad JSON line forwarded to onLog as error, does not crash", async () => {
    stubBridgeExists();
    const child = fakeChild();
    nextChild = child;

    const logs = [];
    const onLog = (level, message) => logs.push({ level, message });

    const p = codexBufferStatus({ onLog });
    await awaitSpawn();

    pushLines(
      child.stdout,
      "this is not json",
      JSON.stringify({
        event: "result",
        payload: { success: true, error: null, state: "running", host: "127.0.0.1", port: 9999, pid: 42 },
      })
    );
    child.stdout.push(null);
    child.emit("close", 0);

    const result = await p;
    expect(result.state).toBe("running");
    expect(logs).toEqual([{ level: "error", message: "this is not json" }]);
  });

  // ── exit code 2 ──────────────────────────────────────────────────────

  test("exit code 2 rejects with stderr text", async () => {
    stubBridgeExists();
    const child = fakeChild();
    nextChild = child;

    const p = getStatus();
    await awaitSpawn();

    child.stderr.push("unrecognized arguments: --bad");
    child.stdout.push(null);
    child.emit("close", 2);

    await expect(p).rejects.toThrow("bridge: argv error: unrecognized arguments: --bad");
  });

  // ── no result emitted ────────────────────────────────────────────────

  test("no result event before close rejects", async () => {
    stubBridgeExists();
    const child = fakeChild();
    nextChild = child;

    const p = getStatus();
    await awaitSpawn();

    pushLines(
      child.stdout,
      JSON.stringify({ event: "log", level: "info", message: "hello" })
    );
    child.stdout.push(null);
    child.emit("close", 0);

    await expect(p).rejects.toThrow("bridge: no result emitted");
  });

  // ── AbortSignal ──────────────────────────────────────────────────────

  test("AbortSignal triggers SIGTERM and rejects", async () => {
    stubBridgeExists();
    const child = fakeChild();
    nextChild = child;

    const ac = new AbortController();
    const p = getStatus({ signal: ac.signal });

    // Let the spawn happen, then abort
    await new Promise((r) => setImmediate(r));
    ac.abort();

    await expect(p).rejects.toThrow("bridge: aborted");
    expect(child.kill).toHaveBeenCalledWith("SIGTERM");
  });

  // ── bridge binary not found ──────────────────────────────────────────

  test("rejects when bridge binary is not found", async () => {
    // existsSyncResults is empty → findBridgeBinary returns null
    // Also mock execFile to fail for which/where fallback
    const p = getStatus();
    await expect(p).rejects.toThrow("bridge: binary not found");
  });

  // ── codexBufferStart / codexBufferStop ───────────────────────────────

  test("codexBufferStart passes correct argv", async () => {
    stubBridgeExists();
    const child = fakeChild();
    nextChild = child;

    const p = codexBufferStart();
    await awaitSpawn();
    pushLines(
      child.stdout,
      JSON.stringify({
        event: "result",
        payload: { success: true, error: null, state: "running", host: "127.0.0.1", port: 9999, pid: 100 },
      })
    );
    child.stdout.push(null);
    child.emit("close", 0);

    await p;
    expect(cp.spawn.mock.calls[0][1]).toEqual(["codex-buffer-start"]);
  });

  test("codexBufferStop passes correct argv", async () => {
    stubBridgeExists();
    const child = fakeChild();
    nextChild = child;

    const p = codexBufferStop();
    await awaitSpawn();
    pushLines(
      child.stdout,
      JSON.stringify({
        event: "result",
        payload: { success: true, error: null, state: "stopped", host: null, port: null, pid: null },
      })
    );
    child.stdout.push(null);
    child.emit("close", 0);

    await p;
    expect(cp.spawn.mock.calls[0][1]).toEqual(["codex-buffer-stop"]);
  });
});

// ═══════════════════════════════════════════════════════════════════════
// python.ts tests
// ═══════════════════════════════════════════════════════════════════════

describe("python discovery", () => {
  const IS_WIN = os.platform() === "win32";
  const VENV_DIR = path.join(os.homedir(), ".arize", "harness", "venv");
  const VENV_BIN = IS_WIN
    ? path.join(VENV_DIR, "Scripts")
    : path.join(VENV_DIR, "bin");
  const VENV_PYTHON = path.join(VENV_BIN, IS_WIN ? "python.exe" : "python");
  const BRIDGE_PATH = path.join(
    VENV_BIN,
    IS_WIN ? "arize-vscode-bridge.exe" : "arize-vscode-bridge"
  );

  test("findPython: returns venv python when it exists and is >=3.9", async () => {
    existsSyncResults[VENV_PYTHON] = true;

    // Mock execFile to return version info for the venv python
    cp.execFile.mockImplementation((cmd, args, opts, cb) => {
      if (cmd === VENV_PYTHON) {
        cb(null, "3 12", "");
      } else {
        cb(new Error("not found"), "", "");
      }
    });

    const result = await findPython();
    expect(result).toBe(VENV_PYTHON);
  });

  test("findPython: falls back to PATH when venv missing", async () => {
    // No venv python
    const whichCmd = IS_WIN ? "where" : "which";
    const pythonName = IS_WIN ? "python3.exe" : "python3";
    const pathPython = IS_WIN
      ? "C:\\Python\\python3.exe"
      : "/usr/bin/python3";

    cp.execFile.mockImplementation((cmd, args, opts, cb) => {
      if (cmd === whichCmd && args[0] === pythonName) {
        cb(null, pathPython, "");
      } else if (cmd === pathPython) {
        cb(null, "3 11", "");
      } else {
        cb(new Error("not found"), "", "");
      }
    });

    existsSyncResults[pathPython] = true;

    const result = await findPython();
    expect(result).toBe(pathPython);
  });

  test("findPython: returns null when no python found", async () => {
    cp.execFile.mockImplementation((_cmd, _args, _opts, cb) => {
      cb(new Error("not found"), "", "");
    });

    const result = await findPython();
    expect(result).toBeNull();
  });

  test("findBridgeBinary: returns venv path when it exists", async () => {
    existsSyncResults[BRIDGE_PATH] = true;

    const result = await findBridgeBinary();
    expect(result).toBe(BRIDGE_PATH);
  });

  test("findBridgeBinary: falls back to PATH when venv missing", async () => {
    // Venv bridge does NOT exist — existsSyncResults is empty for BRIDGE_PATH
    const whichCmd = IS_WIN ? "where" : "which";
    const pathBridge = IS_WIN
      ? "C:\\Tools\\arize-vscode-bridge.exe"
      : "/usr/local/bin/arize-vscode-bridge";

    cp.execFile.mockImplementation((cmd, args, opts, cb) => {
      const bridgeName = IS_WIN ? "arize-vscode-bridge.exe" : "arize-vscode-bridge";
      if (cmd === whichCmd && args[0] === bridgeName) {
        cb(null, pathBridge, "");
      } else {
        cb(new Error("not found"), "", "");
      }
    });

    existsSyncResults[pathBridge] = true;

    const result = await findBridgeBinary();
    expect(result).toBe(pathBridge);
  });

  test("findBridgeBinary: returns null when not found anywhere", async () => {
    cp.execFile.mockImplementation((_cmd, _args, _opts, cb) => {
      cb(new Error("not found"), "", "");
    });

    const result = await findBridgeBinary();
    expect(result).toBeNull();
  });

  test("checkVenvExists: returns true when venv dir exists", () => {
    existsSyncResults[VENV_DIR] = true;
    expect(checkVenvExists()).toBe(true);
  });

  test("checkVenvExists: returns false when venv dir missing", () => {
    expect(checkVenvExists()).toBe(false);
  });
});
