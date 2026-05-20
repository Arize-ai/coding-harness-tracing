/**
 * Bootstrap module: ensures the arize-vscode-bridge binary exists on disk
 * by creating a venv and pip-installing the bundled wheel if needed.
 */

import { spawn, ChildProcess } from "child_process";
import { existsSync, readFileSync } from "fs";
import * as fs from "fs";
import { join } from "path";
import { homedir, platform } from "os";
import { findPython, findBridgeBinary } from "./python";

const IS_WIN = platform() === "win32";

// ── Public types ─────────────────────────────────────────────────────

export interface BootstrapResult {
  ok: boolean;
  /** Absolute path to the bridge binary when ok=true. */
  bridgePath?: string;
  /** Stable machine-readable code. */
  error?: EnsureBridgeError;
  /** Human-readable detail to render in the sidebar. */
  errorMessage?: string;
}

export type EnsureBridgeError =
  | "python_not_found"
  | "venv_create_failed"
  | "wheel_missing"
  | "pip_install_failed"
  | "ssl_fix_failed"
  | "binary_still_missing";

export interface EnsureBridgeOptions {
  /** Streams every spawned process's stdout/stderr. */
  onLog?: (level: "info" | "error", message: string) => void;
  /** Aborts the in-flight bootstrap. Propagates SIGTERM to children. */
  signal?: AbortSignal;
  /** Path containing python/wheel.json. Pass ctx.extensionPath. */
  extensionPath: string;
}

// ── macOS certifi fix ───────────────────────────────────────────────

interface MacOSCertifiFixOptions {
  venvDir: string;
  onLog?: (level: "info" | "error", message: string) => void;
  signal?: AbortSignal;
}

export type MacOSCertifiFixResult = { ok: true } | { ok: false; reason: string };

/** Contents written to sitecustomize.py — exported so tests can byte-compare. */
export const SITECUSTOMIZE_PY = `# Arize Harness Tracing: point Python's SSL stack at certifi's CA bundle on macOS.
# This runs automatically at interpreter startup, before any hook code.
import os as _os
try:
    import certifi as _certifi
    _bundle = _certifi.where()
    _os.environ.setdefault("SSL_CERT_FILE", _bundle)
    _os.environ.setdefault("REQUESTS_CA_BUNDLE", _bundle)
except ImportError:
    pass
`;

/**
 * Ensure Python's SSL stack uses certifi's CA bundle on macOS.
 * Ports _fix_macos_ssl_certs from install.sh:147-176.
 */
export async function applyMacOSCertifiFix(
  opts: MacOSCertifiFixOptions,
): Promise<MacOSCertifiFixResult> {
  const { venvDir, onLog, signal } = opts;
  const venvPip = join(venvDir, "bin", "pip");
  const venvPython = join(venvDir, "bin", "python");

  // Step 1: Install certifi
  try {
    const pipResult = await runProcess(venvPip, ["install", "--quiet", "certifi"], onLog, signal);
    if (pipResult.code !== 0) {
      return { ok: false, reason: `certifi install failed: ${pipResult.stderr}` };
    }
  } catch (err: unknown) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw err;
    }
    return { ok: false, reason: `certifi install failed: ${String(err)}` };
  }

  // Step 2: Get certifi bundle path
  let bundlePath: string;
  try {
    const certResult = await runProcessWithStdout(
      venvPython, ["-c", "import certifi; print(certifi.where())"], onLog, signal,
    );
    if (certResult.code !== 0) {
      return { ok: false, reason: "certifi.where() lookup failed" };
    }
    const firstLine = certResult.stdout.split("\n").map(l => l.trim()).find(l => l.length > 0);
    if (!firstLine) {
      return { ok: false, reason: "certifi.where() lookup failed" };
    }
    bundlePath = firstLine;
  } catch (err: unknown) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw err;
    }
    return { ok: false, reason: "certifi.where() lookup failed" };
  }

  // Step 3: Get site-packages dir
  let sitePackagesDir: string;
  try {
    const siteResult = await runProcessWithStdout(
      venvPython, ["-c", "import site; print(site.getsitepackages()[0])"], onLog, signal,
    );
    if (siteResult.code !== 0) {
      return { ok: false, reason: "site-packages lookup failed" };
    }
    const firstLine = siteResult.stdout.split("\n").map(l => l.trim()).find(l => l.length > 0);
    if (!firstLine) {
      return { ok: false, reason: "site-packages lookup failed" };
    }
    sitePackagesDir = firstLine;
  } catch (err: unknown) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw err;
    }
    return { ok: false, reason: "site-packages lookup failed" };
  }

  // Step 4: Write sitecustomize.py
  try {
    await fs.promises.writeFile(join(sitePackagesDir, "sitecustomize.py"), SITECUSTOMIZE_PY);
  } catch (err: unknown) {
    return { ok: false, reason: `Failed to write sitecustomize.py: ${String(err)}` };
  }

  return { ok: true };
}

// ── Concurrency state ────────────────────────────────────────────────

let _inflight: Promise<BootstrapResult> | null = null;

/** Reset concurrency state between tests. */
export function _resetForTesting(): void {
  _inflight = null;
}

// ── Internal helpers ─────────────────────────────────────────────────

interface WheelJson {
  filename: string;
  version: string;
}

/**
 * Spawn a process and collect its stderr. Resolves with exit code and
 * trimmed stderr. Streams output through onLog. Honors AbortSignal.
 */
function runProcess(
  cmd: string,
  args: string[],
  onLog?: (level: "info" | "error", message: string) => void,
  signal?: AbortSignal,
): Promise<{ code: number; stderr: string }> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException("The operation was aborted.", "AbortError"));
      return;
    }

    const child: ChildProcess = spawn(cmd, args, { stdio: ["ignore", "pipe", "pipe"] });

    let stderr = "";

    child.stdout?.on("data", (chunk: Buffer) => {
      const text = chunk.toString();
      onLog?.("info", text);
    });

    child.stderr?.on("data", (chunk: Buffer) => {
      const text = chunk.toString();
      stderr += text;
      onLog?.("error", text);
    });

    const onAbort = () => {
      child.kill("SIGTERM");
      reject(new DOMException("The operation was aborted.", "AbortError"));
    };

    signal?.addEventListener("abort", onAbort, { once: true });

    child.on("error", (err) => {
      signal?.removeEventListener("abort", onAbort);
      reject(err);
    });

    child.on("close", (code) => {
      signal?.removeEventListener("abort", onAbort);
      resolve({ code: code ?? 1, stderr: stderr.trim() });
    });
  });
}

/**
 * Like runProcess but also collects stdout. Used by applyMacOSCertifiFix
 * to capture certifi.where() and site.getsitepackages() output.
 */
function runProcessWithStdout(
  cmd: string,
  args: string[],
  onLog?: (level: "info" | "error", message: string) => void,
  signal?: AbortSignal,
): Promise<{ code: number; stdout: string; stderr: string }> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException("The operation was aborted.", "AbortError"));
      return;
    }

    const child: ChildProcess = spawn(cmd, args, { stdio: ["ignore", "pipe", "pipe"] });

    let stdout = "";
    let stderr = "";

    child.stdout?.on("data", (chunk: Buffer) => {
      const text = chunk.toString();
      stdout += text;
      onLog?.("info", text);
    });

    child.stderr?.on("data", (chunk: Buffer) => {
      const text = chunk.toString();
      stderr += text;
      onLog?.("error", text);
    });

    const onAbort = () => {
      child.kill("SIGTERM");
      reject(new DOMException("The operation was aborted.", "AbortError"));
    };

    signal?.addEventListener("abort", onAbort, { once: true });

    child.on("error", (err) => {
      signal?.removeEventListener("abort", onAbort);
      reject(err);
    });

    child.on("close", (code) => {
      signal?.removeEventListener("abort", onAbort);
      resolve({ code: code ?? 1, stdout: stdout.trim(), stderr: stderr.trim() });
    });
  });
}

// ── Main entry point ─────────────────────────────────────────────────

/**
 * Ensure the bridge binary exists on disk. Idempotent and safe to
 * call concurrently — a single in-flight bootstrap is shared across
 * callers within one process.
 */
export function ensureBridge(opts: EnsureBridgeOptions): Promise<BootstrapResult> {
  if (_inflight) {
    return _inflight;
  }

  const promise = doEnsureBridge(opts).finally(() => {
    _inflight = null;
  });
  _inflight = promise;
  return promise;
}

async function doEnsureBridge(opts: EnsureBridgeOptions): Promise<BootstrapResult> {
  const { onLog, signal, extensionPath } = opts;

  // Step 1: Already installed?
  const existing = await findBridgeBinary();
  if (existing) {
    return { ok: true, bridgePath: existing };
  }

  // Step 2: Find system Python
  const systemPython = await findPython();
  if (!systemPython) {
    return { ok: false, error: "python_not_found", errorMessage: "Python ≥ 3.9 not found on PATH." };
  }

  // Step 3: Create venv if absent
  const venvDir = join(homedir(), ".arize", "harness", "venv");
  if (!existsSync(venvDir)) {
    try {
      const result = await runProcess(systemPython, ["-m", "venv", venvDir], onLog, signal);
      if (result.code !== 0) {
        return { ok: false, error: "venv_create_failed", errorMessage: result.stderr || "venv creation failed." };
      }
    } catch (err: unknown) {
      if (err instanceof DOMException && err.name === "AbortError") {
        throw err;
      }
      return { ok: false, error: "venv_create_failed", errorMessage: String(err) };
    }
  }

  // Step 4: Read wheel.json
  const wheelJsonPath = join(extensionPath, "python", "wheel.json");
  let wheelJson: WheelJson;
  try {
    const raw = readFileSync(wheelJsonPath, "utf-8");
    wheelJson = JSON.parse(raw);
    if (!wheelJson.filename) {
      return { ok: false, error: "wheel_missing", errorMessage: "Bundled bridge wheel is missing." };
    }
  } catch {
    return { ok: false, error: "wheel_missing", errorMessage: "Bundled bridge wheel is missing." };
  }

  const wheelPath = join(extensionPath, "python", wheelJson.filename);
  if (!existsSync(wheelPath)) {
    return { ok: false, error: "wheel_missing", errorMessage: "Bundled bridge wheel is missing." };
  }

  // Step 5: Check pip in venv
  const venvPip = IS_WIN
    ? join(venvDir, "Scripts", "pip.exe")
    : join(venvDir, "bin", "pip");
  if (!existsSync(venvPip)) {
    return { ok: false, error: "venv_create_failed", errorMessage: `Pip not found in venv at ${venvPip}.` };
  }

  // Step 6: pip install the wheel
  try {
    // --force-reinstall: the bundled wheel keeps the same version across rebuilds, so
    // pip would otherwise treat an existing 0.1.0 install as "already satisfied" and
    // skip refreshing entry-point scripts (e.g. a newly-added arize-vscode-bridge).
    const result = await runProcess(
      venvPip,
      ["install", "--quiet", "--force-reinstall", "--no-deps", wheelPath],
      onLog,
      signal,
    );
    if (result.code !== 0) {
      return { ok: false, error: "pip_install_failed", errorMessage: result.stderr || "pip install failed." };
    }
  } catch (err: unknown) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw err;
    }
    return { ok: false, error: "pip_install_failed", errorMessage: String(err) };
  }

  // Step 7: macOS SSL cert fix
  if (process.platform === "darwin") {
    const certResult = await applyMacOSCertifiFix({ venvDir, onLog, signal });
    if (!certResult.ok) {
      return { ok: false, error: "ssl_fix_failed", errorMessage: certResult.reason };
    }
  }

  // Step 8: Verify bridge binary now exists
  const bridgePath = await findBridgeBinary();
  if (!bridgePath) {
    return { ok: false, error: "binary_still_missing", errorMessage: "Install completed but arize-vscode-bridge was not found." };
  }

  return { ok: true, bridgePath };
}
