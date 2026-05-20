/**
 * Tests for build-wheel.js
 *
 * Mocks child_process, fs, and fs/promises so no Python is required.
 */

const path = require("path");

// ── Track spawn / spawnSync calls ────────────────────────────────────────
// Variables prefixed with "mock" so jest.mock factories can reference them.

let mockSpawnSyncImpl;
let mockSpawnImpl;

jest.mock("child_process", () => ({
  spawnSync: jest.fn((...args) => {
    return mockSpawnSyncImpl(...args);
  }),
  spawn: jest.fn((...args) => {
    return mockSpawnImpl(...args);
  }),
}));

// ── Mock fs/promises ─────────────────────────────────────────────────────

let mockReaddirImpl;
let mockReadFileImpl;
let mockWriteFileImpl;

jest.mock("fs/promises", () => ({
  rm: jest.fn(() => Promise.resolve()),
  mkdir: jest.fn(() => Promise.resolve()),
  readdir: jest.fn((...args) => mockReaddirImpl(...args)),
  readFile: jest.fn((...args) => mockReadFileImpl(...args)),
  writeFile: jest.fn((...args) => mockWriteFileImpl(...args)),
}));

jest.mock("fs", () => ({
  ...jest.requireActual("fs"),
  existsSync: jest.fn(() => true),
}));

// ── Import module under test AFTER mocks ─────────────────────────────────

const { main } = require("../build-wheel");

// ── Helpers ──────────────────────────────────────────────────────────────

const savedPlatform = process.platform;
let spawnCallLog;
let writeFileCalls;

function setPlatform(value) {
  Object.defineProperty(process, "platform", { value, configurable: true });
}

function makeDefaultSpawnImpl() {
  const { EventEmitter } = require("events");
  return (...args) => {
    spawnCallLog.push({ type: "spawn", args });
    const child = new EventEmitter();
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    process.nextTick(() => child.emit("close", 0));
    return child;
  };
}

// ── Test suite ───────────────────────────────────────────────────────────

beforeEach(() => {
  spawnCallLog = [];
  writeFileCalls = [];

  mockSpawnSyncImpl = (cmd, args) => {
    spawnCallLog.push({ type: "spawnSync", args: [cmd, args] });
    if (cmd === "python3" || cmd === "py") return { status: 0 };
    return { status: 1 };
  };

  mockSpawnImpl = makeDefaultSpawnImpl();

  mockReaddirImpl = () =>
    Promise.resolve(["arize_harness_tracing-0.1.0-py3-none-any.whl"]);

  mockReadFileImpl = () =>
    Promise.resolve(
      '[project]\nname = "arize-harness-tracing"\nversion = "0.1.0"\n'
    );

  mockWriteFileImpl = (...args) => {
    writeFileCalls.push(args);
    return Promise.resolve();
  };

  setPlatform(savedPlatform);
});

afterEach(() => {
  setPlatform(savedPlatform);
  jest.clearAllMocks();
});

describe("build-wheel main()", () => {
  test("succeeds when build produces exactly one .whl", async () => {
    const result = await main();

    expect(result.version).toBe("0.1.0");
    expect(result.wheelPath).toContain(
      "arize_harness_tracing-0.1.0-py3-none-any.whl"
    );

    // wheel.json was written
    expect(writeFileCalls.length).toBe(1);
    const [filePath, content] = writeFileCalls[0];
    expect(filePath).toContain("wheel.json");
    const parsed = JSON.parse(content);
    expect(parsed.filename).toBe(
      "arize_harness_tracing-0.1.0-py3-none-any.whl"
    );
    expect(parsed.version).toBe("0.1.0");
  });

  test("rejects when no .whl files exist after build", async () => {
    mockReaddirImpl = () => Promise.resolve([]);
    await expect(main()).rejects.toThrow(/no wheel/i);
  });

  test("rejects when multiple .whl files exist after build", async () => {
    mockReaddirImpl = () =>
      Promise.resolve(["a-0.1.0-py3-none-any.whl", "b-0.2.0-py3-none-any.whl"]);
    await expect(main()).rejects.toThrow(/multiple/i);
  });

  test("rejects when no qualifying python is found", async () => {
    mockSpawnSyncImpl = (cmd, args) => {
      spawnCallLog.push({ type: "spawnSync", args: [cmd, args] });
      return { status: 1 };
    };
    await expect(main()).rejects.toThrow(/No Python/);
  });

  test("Windows discovery tries py -3 before other system candidates", async () => {
    setPlatform("win32");

    // Only py -3 succeeds; venv python paths don't.
    mockSpawnSyncImpl = (cmd, args) => {
      spawnCallLog.push({ type: "spawnSync", args: [cmd, args] });
      if (cmd === "py" && args && args[0] === "-3") return { status: 0 };
      return { status: 1 };
    };

    await main();

    const syncCalls = spawnCallLog.filter((c) => c.type === "spawnSync");
    const pyIdx = syncCalls.findIndex(
      (c) => c.args[0] === "py" && c.args[1][0] === "-3",
    );
    expect(pyIdx).toBeGreaterThanOrEqual(0);
    // Among the non-venv candidates, py -3 comes first.
    const nonVenvCalls = syncCalls.filter(
      (c) => !c.args[0].includes("venv"),
    );
    expect(nonVenvCalls[0].args[0]).toBe("py");
    expect(nonVenvCalls[0].args[1][0]).toBe("-3");
  });
});
