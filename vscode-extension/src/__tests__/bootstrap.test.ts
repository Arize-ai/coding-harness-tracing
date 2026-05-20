/**
 * Tests for bootstrap.ts (ensureBridge).
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

const mockExistsSync = jest.fn();
const mockReadFileSync = jest.fn();
const mockWriteFile = jest.fn();
jest.mock("fs", () => ({
  existsSync: mockExistsSync,
  readFileSync: mockReadFileSync,
  promises: {
    writeFile: mockWriteFile,
  },
}));

import { EventEmitter } from "events";
import { join } from "path";
import { findPython, findBridgeBinary } from "../python";
import { ensureBridge, BootstrapResult, EnsureBridgeOptions, _resetForTesting, SITECUSTOMIZE_PY } from "../bootstrap";

const mockFindPython = findPython as jest.MockedFunction<typeof findPython>;
const mockFindBridgeBinary = findBridgeBinary as jest.MockedFunction<typeof findBridgeBinary>;

// ── Helpers ──────────────────────────────────────────────────────────

/** Create a fake ChildProcess that completes with a given code, stderr, and optional stdout. */
function fakeSpawn(exitCode: number, stderr = "", stdout = "") {
  const child = new EventEmitter() as any;
  child.stdout = new EventEmitter();
  child.stderr = new EventEmitter();
  child.kill = jest.fn();
  child.stdin = null;

  mockSpawn.mockReturnValueOnce(child);

  // Schedule the events asynchronously so callers can wire up listeners.
  setImmediate(() => {
    if (stdout) {
      child.stdout.emit("data", Buffer.from(stdout));
    }
    if (stderr) {
      child.stderr.emit("data", Buffer.from(stderr));
    }
    child.emit("close", exitCode);
  });

  return child;
}

function defaultOpts(overrides: Partial<EnsureBridgeOptions> = {}): EnsureBridgeOptions {
  return {
    extensionPath: "/ext",
    ...overrides,
  };
}

const WHEEL_JSON = JSON.stringify({ filename: "arize_harness_tracing-0.1.0-py3-none-any.whl", version: "0.1.0" });

// ── Reset mocks between tests ────────────────────────────────────────

const ORIGINAL_PLATFORM = process.platform;

beforeEach(() => {
  // resetAllMocks (vs clearAllMocks) drains *Once queues so a test that fails
  // mid-flow can't leak unconsumed mocks into the next test.
  jest.resetAllMocks();
  _resetForTesting();
  // Default to linux so the macOS SSL fix step (bootstrap.ts step 7) is skipped
  // in tests that don't opt in. The inner SSL describe overrides to "darwin".
  Object.defineProperty(process, "platform", { value: "linux" });
  mockFindBridgeBinary.mockResolvedValue(null);
  mockFindPython.mockResolvedValue(null);
  mockExistsSync.mockReturnValue(false);
  mockReadFileSync.mockImplementation(() => {
    throw new Error("ENOENT");
  });
  mockWriteFile.mockResolvedValue(undefined);
});

afterEach(() => {
  Object.defineProperty(process, "platform", { value: ORIGINAL_PLATFORM });
});

// ── Tests ────────────────────────────────────────────────────────────

describe("ensureBridge", () => {
  it("returns ok when findBridgeBinary resolves on first call", async () => {
    mockFindBridgeBinary.mockResolvedValueOnce("/usr/bin/arize-vscode-bridge");

    const result = await ensureBridge(defaultOpts());

    expect(result).toEqual({ ok: true, bridgePath: "/usr/bin/arize-vscode-bridge" });
    expect(mockSpawn).not.toHaveBeenCalled();
    expect(mockFindPython).not.toHaveBeenCalled();
  });

  it("returns python_not_found when findPython returns null", async () => {
    mockFindPython.mockResolvedValueOnce(null);

    const result = await ensureBridge(defaultOpts());

    expect(result).toEqual({
      ok: false,
      error: "python_not_found",
      errorMessage: "Python ≥ 3.9 not found on PATH.",
    });
    expect(mockSpawn).not.toHaveBeenCalled();
  });

  it("creates venv when venvDir does not exist", async () => {
    mockFindPython.mockResolvedValueOnce("/usr/bin/python3");
    // venvDir does not exist -> first existsSync returns false
    // pip exists -> second existsSync returns true
    // wheel.json readable
    mockExistsSync.mockImplementation((p: string) => {
      if (typeof p === "string" && p.includes("pip")) return true;
      if (typeof p === "string" && p.includes(".whl")) return true;
      return false;
    });
    mockReadFileSync.mockReturnValueOnce(WHEEL_JSON);

    // venv creation succeeds
    fakeSpawn(0);
    // pip install succeeds
    fakeSpawn(0);

    // After install, bridge is found
    mockFindBridgeBinary.mockResolvedValueOnce(null).mockResolvedValueOnce("/home/user/.arize/harness/venv/bin/arize-vscode-bridge");

    const result = await ensureBridge(defaultOpts());

    expect(result.ok).toBe(true);
    // First spawn call is venv creation
    expect(mockSpawn).toHaveBeenCalledTimes(2);
    const venvCall = mockSpawn.mock.calls[0];
    expect(venvCall[0]).toBe("/usr/bin/python3");
    expect(venvCall[1]).toEqual(expect.arrayContaining(["-m", "venv"]));
  });

  it("returns wheel_missing when wheel.json is absent", async () => {
    mockFindPython.mockResolvedValueOnce("/usr/bin/python3");
    // venv already exists
    mockExistsSync.mockImplementation((p: string) => {
      if (typeof p === "string" && p.includes("venv")) return true;
      if (typeof p === "string" && p.includes("pip")) return true;
      return false;
    });
    // readFileSync throws (file missing)
    mockReadFileSync.mockImplementation(() => {
      throw new Error("ENOENT");
    });

    const result = await ensureBridge(defaultOpts());

    expect(result).toEqual({
      ok: false,
      error: "wheel_missing",
      errorMessage: "Bundled bridge wheel is missing.",
    });
  });

  it("returns wheel_missing when wheel file is absent on disk", async () => {
    mockFindPython.mockResolvedValueOnce("/usr/bin/python3");
    mockExistsSync.mockImplementation((p: string) => {
      if (typeof p === "string" && p.includes("venv")) return true;
      if (typeof p === "string" && p.includes("pip")) return true;
      // wheel file does not exist
      if (typeof p === "string" && p.includes(".whl")) return false;
      return false;
    });
    mockReadFileSync.mockReturnValueOnce(WHEEL_JSON);

    const result = await ensureBridge(defaultOpts());

    expect(result).toEqual({
      ok: false,
      error: "wheel_missing",
      errorMessage: "Bundled bridge wheel is missing.",
    });
  });

  it("returns pip_install_failed when pip exits non-zero", async () => {
    mockFindPython.mockResolvedValueOnce("/usr/bin/python3");
    mockExistsSync.mockImplementation((p: string) => {
      if (typeof p === "string" && p.includes("venv")) return true;
      if (typeof p === "string" && p.includes("pip")) return true;
      if (typeof p === "string" && p.includes(".whl")) return true;
      return false;
    });
    mockReadFileSync.mockReturnValueOnce(WHEEL_JSON);

    // pip install fails
    fakeSpawn(1, "  Could not find wheel  \n");

    const result = await ensureBridge(defaultOpts());

    expect(result).toEqual({
      ok: false,
      error: "pip_install_failed",
      errorMessage: "Could not find wheel",
    });
  });

  it("re-calls findBridgeBinary after successful install and propagates bridgePath", async () => {
    mockFindPython.mockResolvedValueOnce("/usr/bin/python3");
    mockExistsSync.mockImplementation((p: string) => {
      if (typeof p === "string" && p.includes("venv")) return true;
      if (typeof p === "string" && p.includes("pip")) return true;
      if (typeof p === "string" && p.includes(".whl")) return true;
      return false;
    });
    mockReadFileSync.mockReturnValueOnce(WHEEL_JSON);

    // pip install succeeds
    fakeSpawn(0);

    // First call (step 1) returns null; second call (step 8) returns path
    mockFindBridgeBinary
      .mockResolvedValueOnce(null)
      .mockResolvedValueOnce("/home/user/.arize/harness/venv/bin/arize-vscode-bridge");

    const result = await ensureBridge(defaultOpts());

    expect(result).toEqual({ ok: true, bridgePath: "/home/user/.arize/harness/venv/bin/arize-vscode-bridge" });
    expect(mockFindBridgeBinary).toHaveBeenCalledTimes(2);
  });

  it("returns binary_still_missing when bridge not found after install", async () => {
    mockFindPython.mockResolvedValueOnce("/usr/bin/python3");
    mockExistsSync.mockImplementation((p: string) => {
      if (typeof p === "string" && p.includes("venv")) return true;
      if (typeof p === "string" && p.includes("pip")) return true;
      if (typeof p === "string" && p.includes(".whl")) return true;
      return false;
    });
    mockReadFileSync.mockReturnValueOnce(WHEEL_JSON);

    // pip install succeeds
    fakeSpawn(0);

    // Bridge still not found after install
    mockFindBridgeBinary.mockResolvedValue(null);

    const result = await ensureBridge(defaultOpts());

    expect(result).toEqual({
      ok: false,
      error: "binary_still_missing",
      errorMessage: "Install completed but arize-vscode-bridge was not found.",
    });
  });

  it("concurrent calls share one underlying invocation", async () => {
    mockFindPython.mockResolvedValue("/usr/bin/python3");
    mockExistsSync.mockImplementation((p: string) => {
      if (typeof p === "string" && p.includes("venv")) return true;
      if (typeof p === "string" && p.includes("pip")) return true;
      if (typeof p === "string" && p.includes(".whl")) return true;
      return false;
    });
    mockReadFileSync.mockReturnValue(WHEEL_JSON);

    // pip install succeeds
    fakeSpawn(0);

    mockFindBridgeBinary
      .mockResolvedValueOnce(null)
      .mockResolvedValueOnce("/bridge");

    const [r1, r2] = await Promise.all([
      ensureBridge(defaultOpts()),
      ensureBridge(defaultOpts()),
    ]);

    expect(r1).toEqual(r2);
    // findPython called only once (shared invocation)
    expect(mockFindPython).toHaveBeenCalledTimes(1);
  });

  it("abort signal kills child process and throws AbortError", async () => {
    mockFindPython.mockResolvedValueOnce("/usr/bin/python3");
    // venvDir doesn't exist, so venv creation spawn is triggered
    mockExistsSync.mockReturnValue(false);

    const ac = new AbortController();

    // Create a child that does not auto-close — we'll abort it
    const child = new EventEmitter() as any;
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    child.kill = jest.fn();
    child.stdin = null;
    mockSpawn.mockReturnValueOnce(child);

    const promise = ensureBridge(defaultOpts({ signal: ac.signal }));

    // Give event loop time to spawn
    await new Promise((r) => setImmediate(r));

    ac.abort();

    await expect(promise).rejects.toThrow("aborted");
    expect(child.kill).toHaveBeenCalledWith("SIGTERM");
  });

  it("returns venv_create_failed when pip is missing in venv", async () => {
    mockFindPython.mockResolvedValueOnce("/usr/bin/python3");
    mockExistsSync.mockImplementation((p: string) => {
      // venv directory exists
      if (typeof p === "string" && p.endsWith("venv")) return true;
      // pip does NOT exist
      if (typeof p === "string" && p.includes("pip")) return false;
      if (typeof p === "string" && p.includes(".whl")) return true;
      return false;
    });
    mockReadFileSync.mockReturnValueOnce(WHEEL_JSON);

    const result = await ensureBridge(defaultOpts());

    expect(result.ok).toBe(false);
    expect(result.error).toBe("venv_create_failed");
    expect(result.errorMessage).toContain("Pip not found");
  });

  it("returns venv_create_failed when venv spawn exits non-zero", async () => {
    mockFindPython.mockResolvedValueOnce("/usr/bin/python3");
    // venvDir doesn't exist
    mockExistsSync.mockReturnValue(false);

    fakeSpawn(1, "Error: ensurepip not available");

    const result = await ensureBridge(defaultOpts());

    expect(result).toEqual({
      ok: false,
      error: "venv_create_failed",
      errorMessage: "Error: ensurepip not available",
    });
  });

  it("streams onLog callbacks for spawned processes", async () => {
    mockFindPython.mockResolvedValueOnce("/usr/bin/python3");
    mockExistsSync.mockImplementation((p: string) => {
      if (typeof p === "string" && p.includes("venv")) return true;
      if (typeof p === "string" && p.includes("pip")) return true;
      if (typeof p === "string" && p.includes(".whl")) return true;
      return false;
    });
    mockReadFileSync.mockReturnValueOnce(WHEEL_JSON);

    // pip install succeeds but emits output
    const child = new EventEmitter() as any;
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    child.kill = jest.fn();
    child.stdin = null;
    mockSpawn.mockReturnValueOnce(child);

    mockFindBridgeBinary
      .mockResolvedValueOnce(null)
      .mockResolvedValueOnce("/bridge");

    const logs: Array<{ level: string; message: string }> = [];

    const promise = ensureBridge(
      defaultOpts({
        onLog: (level, message) => logs.push({ level, message }),
      }),
    );

    await new Promise((r) => setImmediate(r));
    child.stdout.emit("data", Buffer.from("Installing..."));
    child.stderr.emit("data", Buffer.from("WARNING: something"));
    child.emit("close", 0);

    const result = await promise;
    expect(result.ok).toBe(true);
    expect(logs).toEqual(
      expect.arrayContaining([
        { level: "info", message: "Installing..." },
        { level: "error", message: "WARNING: something" },
      ]),
    );
  });

  it("returns venv_create_failed when spawn emits an error event (e.g. ENOENT)", async () => {
    mockFindPython.mockResolvedValueOnce("/usr/bin/python3");
    // venvDir doesn't exist, triggers venv creation
    mockExistsSync.mockReturnValue(false);

    const child = new EventEmitter() as any;
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    child.kill = jest.fn();
    child.stdin = null;
    mockSpawn.mockReturnValueOnce(child);

    const promise = ensureBridge(defaultOpts());

    await new Promise((r) => setImmediate(r));
    child.emit("error", new Error("spawn ENOENT"));

    const result = await promise;
    expect(result).toEqual({
      ok: false,
      error: "venv_create_failed",
      errorMessage: "Error: spawn ENOENT",
    });
  });

  it("returns pip_install_failed when pip spawn emits an error event", async () => {
    mockFindPython.mockResolvedValueOnce("/usr/bin/python3");
    mockExistsSync.mockImplementation((p: string) => {
      if (typeof p === "string" && p.includes("venv")) return true;
      if (typeof p === "string" && p.includes("pip")) return true;
      if (typeof p === "string" && p.includes(".whl")) return true;
      return false;
    });
    mockReadFileSync.mockReturnValueOnce(WHEEL_JSON);

    const child = new EventEmitter() as any;
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    child.kill = jest.fn();
    child.stdin = null;
    mockSpawn.mockReturnValueOnce(child);

    const promise = ensureBridge(defaultOpts());

    await new Promise((r) => setImmediate(r));
    child.emit("error", new Error("spawn EPERM"));

    const result = await promise;
    expect(result).toEqual({
      ok: false,
      error: "pip_install_failed",
      errorMessage: "Error: spawn EPERM",
    });
  });

  // ── macOS SSL certifi fix tests ─────────────────────────────────────

  describe("macOS SSL certifi fix", () => {
    const ORIGINAL_PLATFORM = process.platform;

    beforeEach(() => {
      Object.defineProperty(process, "platform", { value: "darwin" });
    });

    afterEach(() => {
      Object.defineProperty(process, "platform", { value: ORIGINAL_PLATFORM });
    });

    /** Set up mocks so ensureBridge reaches step 7 (SSL fix). */
    function setupToReachSSLFix() {
      mockFindPython.mockResolvedValueOnce("/usr/bin/python3");
      mockExistsSync.mockImplementation((p: string) => {
        if (typeof p === "string" && p.includes("venv")) return true;
        if (typeof p === "string" && p.includes("pip")) return true;
        if (typeof p === "string" && p.includes(".whl")) return true;
        return false;
      });
      mockReadFileSync.mockReturnValueOnce(WHEEL_JSON);
    }

    it("installs certifi, looks up paths, and writes sitecustomize.py on darwin", async () => {
      setupToReachSSLFix();

      // Step 6: pip install wheel succeeds
      fakeSpawn(0);
      // Step 7a: pip install certifi succeeds
      fakeSpawn(0);
      // Step 7b: certifi.where() returns bundle path
      fakeSpawn(0, "", "/path/to/certifi/cacert.pem\n");
      // Step 7c: site.getsitepackages() returns site-packages dir
      fakeSpawn(0, "", "/path/to/site-packages\n");

      mockFindBridgeBinary
        .mockResolvedValueOnce(null)
        .mockResolvedValueOnce("/home/user/.arize/harness/venv/bin/arize-vscode-bridge");

      const result = await ensureBridge(defaultOpts());

      expect(result.ok).toBe(true);

      // Verify spawn calls: wheel install, certifi install, certifi.where(), site.getsitepackages()
      expect(mockSpawn).toHaveBeenCalledTimes(4);

      // certifi install
      const certifiInstallCall = mockSpawn.mock.calls[1];
      expect(certifiInstallCall[1]).toEqual(["install", "--quiet", "certifi"]);

      // certifi.where()
      const certifiWhereCall = mockSpawn.mock.calls[2];
      expect(certifiWhereCall[1]).toEqual(["-c", "import certifi; print(certifi.where())"]);

      // site.getsitepackages()
      const sitePackagesCall = mockSpawn.mock.calls[3];
      expect(sitePackagesCall[1]).toEqual(["-c", "import site; print(site.getsitepackages()[0])"]);

      // writeFile called with correct path and exact content. Use path.join
      // so the expected separators match the host OS — the production code
      // uses path.join, which produces backslashes on Windows even though the
      // test forces process.platform = "darwin".
      expect(mockWriteFile).toHaveBeenCalledTimes(1);
      expect(mockWriteFile).toHaveBeenCalledWith(
        join("/path/to/site-packages", "sitecustomize.py"),
        SITECUSTOMIZE_PY,
      );
    });

    it("returns ssl_fix_failed when certifi install exits non-zero", async () => {
      setupToReachSSLFix();

      // Step 6: pip install wheel succeeds
      fakeSpawn(0);
      // Step 7a: pip install certifi fails
      fakeSpawn(1, "  No matching distribution found  \n");

      mockFindBridgeBinary.mockResolvedValueOnce(null);

      const result = await ensureBridge(defaultOpts());

      expect(result.ok).toBe(false);
      expect(result.error).toBe("ssl_fix_failed");
      expect(result.errorMessage).toContain("certifi install failed");
      expect(result.errorMessage).toContain("No matching distribution found");
      expect(mockWriteFile).not.toHaveBeenCalled();
    });

    it("skips SSL fix on non-darwin platforms", async () => {
      Object.defineProperty(process, "platform", { value: "linux" });

      setupToReachSSLFix();

      // Step 6: pip install wheel succeeds (only spawn needed)
      fakeSpawn(0);

      mockFindBridgeBinary
        .mockResolvedValueOnce(null)
        .mockResolvedValueOnce("/home/user/.arize/harness/venv/bin/arize-vscode-bridge");

      const result = await ensureBridge(defaultOpts());

      expect(result.ok).toBe(true);
      // Only one spawn: the wheel install. No certifi spawns.
      expect(mockSpawn).toHaveBeenCalledTimes(1);
      expect(mockWriteFile).not.toHaveBeenCalled();
    });
  });
});
