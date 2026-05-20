/**
 * Python and bridge-binary discovery utilities.
 */

import { execFile } from "child_process";
import { existsSync } from "fs";
import { join } from "path";
import { homedir, platform } from "os";

const IS_WIN = platform() === "win32";
const VENV_DIR = join(homedir(), ".arize", "harness", "venv");
const VENV_BIN_DIR = IS_WIN ? join(VENV_DIR, "Scripts") : join(VENV_DIR, "bin");
const BRIDGE_NAME = IS_WIN ? "arize-vscode-bridge.exe" : "arize-vscode-bridge";
const PYTHON_NAMES = IS_WIN
  ? ["python3.exe", "python.exe"]
  : ["python3", "python"];

/** Minimum Python version required. */
const MIN_PYTHON: [number, number] = [3, 9];

/**
 * Run a command and return its trimmed stdout, or null on failure.
 */
function run(cmd: string, args: string[]): Promise<string | null> {
  return new Promise((resolve) => {
    execFile(cmd, args, { timeout: 5000 }, (err, stdout) => {
      if (err) {
        resolve(null);
      } else {
        resolve(stdout.trim());
      }
    });
  });
}

/**
 * Check whether the given python binary meets the minimum version requirement.
 */
async function checkVersion(pythonPath: string): Promise<boolean> {
  const out = await run(pythonPath, [
    "-c",
    "import sys; print(sys.version_info.major, sys.version_info.minor)",
  ]);
  if (!out) return false;
  const parts = out.split(" ").map(Number);
  if (parts.length < 2 || isNaN(parts[0]) || isNaN(parts[1])) return false;
  return (
    parts[0] > MIN_PYTHON[0] ||
    (parts[0] === MIN_PYTHON[0] && parts[1] >= MIN_PYTHON[1])
  );
}

/**
 * Locate a Python ≥ 3.9 interpreter.
 *
 * Search order:
 * 1. The venv python inside ~/.arize/harness/venv
 * 2. python3 / python on PATH
 *
 * Returns an absolute path or null.
 */
export async function findPython(): Promise<string | null> {
  // Check venv first
  const venvPython = join(VENV_BIN_DIR, IS_WIN ? "python.exe" : "python");
  if (existsSync(venvPython)) {
    if (await checkVersion(venvPython)) return venvPython;
  }

  // Fall back to PATH
  for (const name of PYTHON_NAMES) {
    const whichCmd = IS_WIN ? "where" : "which";
    const absPath = await run(whichCmd, [name]);
    if (absPath && existsSync(absPath)) {
      if (await checkVersion(absPath)) return absPath;
    }
  }

  return null;
}

/**
 * Locate the `arize-vscode-bridge` binary.
 *
 * Search order:
 * 1. ~/.arize/harness/venv/bin/arize-vscode-bridge (POSIX)
 *    or ~/.arize/harness/venv/Scripts/arize-vscode-bridge.exe (Windows)
 * 2. which / where on PATH
 *
 * Returns an absolute path or null.
 */
export async function findBridgeBinary(): Promise<string | null> {
  // Check venv first
  const venvBridge = join(VENV_BIN_DIR, BRIDGE_NAME);
  if (existsSync(venvBridge)) return venvBridge;

  // Fall back to PATH
  const whichCmd = IS_WIN ? "where" : "which";
  const absPath = await run(whichCmd, [BRIDGE_NAME]);
  if (absPath && existsSync(absPath)) return absPath;

  return null;
}

/**
 * Check whether the ~/.arize/harness/venv directory exists.
 */
export function checkVenvExists(): boolean {
  return existsSync(VENV_DIR);
}
