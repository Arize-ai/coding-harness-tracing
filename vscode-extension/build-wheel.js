/**
 * Build the arize-harness-tracing wheel and place it under vscode-extension/python/.
 *
 * Exports main() returning Promise<{ wheelPath: string, version: string }>.
 * Also runs main() when invoked directly: node build-wheel.js
 */

const { spawnSync, spawn } = require("child_process");
const fs = require("fs");
const fsp = require("fs/promises");
const path = require("path");
const os = require("os");

const REPO_ROOT = path.resolve(__dirname, "..");
const OUT_DIR = path.join(__dirname, "python");

// ---------------------------------------------------------------------------
// Python discovery
// ---------------------------------------------------------------------------

/**
 * Check whether a python binary satisfies >= 3.9.
 * @param {string} cmd
 * @param {string[]} args  Extra args prepended (e.g. ["-3"] for `py -3`).
 * @returns {string|null}  The resolved command string or null.
 */
function checkPython(cmd, args = []) {
  const result = spawnSync(
    cmd,
    [...args, "-c", "import sys; assert sys.version_info >= (3, 9)"],
    { windowsHide: true, stdio: "pipe", timeout: 10000 }
  );
  return result.status === 0 ? cmd : null;
}

/**
 * Check whether `<cmd> <prefix...> -m pip --version` succeeds.
 * uv-managed venvs typically omit pip, so probe before relying on it.
 */
function hasPip(cmd, prefix = []) {
  const result = spawnSync(
    cmd,
    [...prefix, "-m", "pip", "--version"],
    { windowsHide: true, stdio: "pipe", timeout: 10000 }
  );
  return result.status === 0;
}

/**
 * Whether `uv` is on PATH.
 */
function hasUv() {
  const result = spawnSync(
    "uv",
    ["--version"],
    { windowsHide: true, stdio: "pipe", timeout: 10000 }
  );
  return result.status === 0;
}

/**
 * Find a Python >= 3.9 interpreter.
 * @returns {{ cmd: string, prefix: string[] }}
 */
function findPython() {
  const isWin = process.platform === "win32";
  const tried = [];

  // Prefer a virtualenv (active env, or a .venv at the repo root) — otherwise
  // pip install can hit a Homebrew/system Python that PEP 668 marks
  // externally-managed.
  const venvCandidates = [];
  if (process.env.VIRTUAL_ENV) {
    venvCandidates.push(process.env.VIRTUAL_ENV);
  }
  venvCandidates.push(path.join(REPO_ROOT, ".venv"));
  venvCandidates.push(path.join(REPO_ROOT, "venv"));
  for (const venvDir of venvCandidates) {
    const venvPython = path.join(
      venvDir,
      isWin ? "Scripts" : "bin",
      isWin ? "python.exe" : "python",
    );
    tried.push(venvPython);
    if (fs.existsSync(venvPython) && checkPython(venvPython)) {
      return { cmd: venvPython, prefix: [] };
    }
  }

  if (isWin) {
    // Windows: py -3, python3.exe, python.exe
    tried.push("py -3");
    if (checkPython("py", ["-3"])) return { cmd: "py", prefix: ["-3"] };

    for (const name of ["python3", "python"]) {
      tried.push(name);
      if (checkPython(name)) return { cmd: name, prefix: [] };
    }
  } else {
    // POSIX candidates matching install.sh precedence
    const candidates = [
      "python3",
      "python",
      "/usr/bin/python3",
      "/usr/local/bin/python3",
      path.join(os.homedir(), ".local", "bin", "python3"),
    ];
    if (process.platform === "darwin") {
      candidates.push("/opt/homebrew/bin/python3");
    }

    for (const c of candidates) {
      tried.push(c);
      if (checkPython(c)) return { cmd: c, prefix: [] };
    }
  }

  throw new Error(
    `No Python >= 3.9 found. Tried: ${tried.join(", ")}`
  );
}

// ---------------------------------------------------------------------------
// Subprocess helper
// ---------------------------------------------------------------------------

/**
 * Spawn a command inheriting stdio. Rejects on non-zero exit.
 */
function runInherited(cmd, args) {
  return new Promise((resolve, reject) => {
    const child = spawn(cmd, args, {
      stdio: "inherit",
      windowsHide: true,
    });
    child.on("error", reject);
    child.on("close", (code) => {
      if (code !== 0) {
        reject(new Error(`${cmd} ${args.join(" ")} exited with code ${code}`));
      } else {
        resolve();
      }
    });
  });
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

async function main() {
  // 1. Clean output directory
  await fsp.rm(OUT_DIR, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
  await fsp.mkdir(OUT_DIR, { recursive: true });

  // 2. Find python
  const { cmd, prefix } = findPython();

  // 3. Build wheel. Prefer pip + python -m build when pip is available; otherwise
  // fall back to `uv build`, which handles uv-managed venvs that ship without pip.
  if (hasPip(cmd, prefix)) {
    await runInherited(cmd, [...prefix, "-m", "pip", "install", "--quiet", "--upgrade", "build"]);
    await runInherited(cmd, [...prefix, "-m", "build", "--wheel", "--outdir", OUT_DIR, REPO_ROOT]);
  } else if (hasUv()) {
    await runInherited("uv", ["build", "--wheel", "--out-dir", OUT_DIR, REPO_ROOT]);
  } else {
    throw new Error(
      `Python at ${cmd} has no pip module, and uv is not on PATH. ` +
      `Install pip into the venv (python -m ensurepip) or install uv.`
    );
  }

  // 5. Verify exactly one .whl
  const files = (await fsp.readdir(OUT_DIR)).filter((f) => f.endsWith(".whl"));
  if (files.length === 0) {
    throw new Error("Build produced no wheel files in " + OUT_DIR);
  }
  if (files.length > 1) {
    throw new Error("Build produced multiple wheel files: " + files.join(", "));
  }

  const wheelFile = files[0];
  const wheelPath = path.join(OUT_DIR, wheelFile);

  // 6. Extract version from pyproject.toml
  const toml = await fsp.readFile(path.join(REPO_ROOT, "pyproject.toml"), "utf-8");
  const versionMatch = toml.match(/^version\s*=\s*"([^"]+)"/m);
  if (!versionMatch) {
    throw new Error("Could not extract version from pyproject.toml");
  }
  const version = versionMatch[1];

  // 7. Write wheel.json manifest
  await fsp.writeFile(
    path.join(OUT_DIR, "wheel.json"),
    JSON.stringify({ filename: wheelFile, version }, null, 2) + "\n"
  );

  console.log(`Wheel built: ${wheelFile} (v${version})`);
  return { wheelPath, version };
}

module.exports = { main };

if (require.main === module) {
  main().catch((err) => {
    console.error(err.message);
    process.exit(1);
  });
}
