/**
 * Teardown logic for removing all Arize tracing configuration and the bridge venv.
 *
 * Provides an opt-in command that fully removes Arize tracing harness configs
 * and the bridge venv. Does NOT wire into vscode:uninstall — teardown only
 * happens when the user explicitly invokes the command.
 */

import * as fs from "fs";
import * as path from "path";
import * as os from "os";
import * as bridge from "./bridge";
import { HARNESS_KEYS } from "./types";
import type { HarnessKey, StatusPayload } from "./types";

// ── Public interfaces ────────────────────────────────────────────────

export interface TeardownResult {
  /** True iff every harness was uninstalled or skipped, and the venv was removed. */
  ok: boolean;
  /** Per-harness result; entries appear in HARNESS_KEYS order. */
  harnesses: Array<{
    harness: HarnessKey;
    /** "skipped" when the harness was not configured to begin with. */
    state: "uninstalled" | "skipped" | "failed";
    error?: string;
  }>;
  /** True iff ~/.arize/harness/venv was removed (or did not exist). */
  venvRemoved: boolean;
  /** Populated when venv removal failed. */
  venvError?: string;
}

export interface TeardownOptions {
  onLog?: (level: "info" | "error", message: string) => void;
  signal?: AbortSignal;
  /** Default true. When false, skips the venv rm step (harness uninstalls only). */
  removeVenv?: boolean;
}

// ── Implementation ───────────────────────────────────────────────────

export async function teardownAll(opts: TeardownOptions): Promise<TeardownResult> {
  const harnesses: TeardownResult["harnesses"] = [];
  let status: StatusPayload | null = null;
  let codexBufferRunning = false;

  // Step 1: Try to get status from the bridge.
  try {
    status = await bridge.getStatus({ signal: opts.signal });
  } catch {
    // Bridge missing or errored — mark everything skipped, proceed to venv removal.
    opts.onLog?.("info", "Bridge not reachable; skipping harness uninstalls.");
    for (const key of HARNESS_KEYS) {
      harnesses.push({ harness: key, state: "skipped" });
    }
  }

  // Step 2: If we got status, process each harness.
  if (status !== null) {
    // Build a lookup for configured state.
    const statusMap = new Map(
      status.harnesses?.map((h) => [h.name, h]) ?? [],
    );

    // Check if codex buffer is running before we start uninstalls.
    if (status.codex_buffer?.state === "running") {
      codexBufferRunning = true;
    }

    for (const key of HARNESS_KEYS) {
      if (opts.signal?.aborted) {
        // Record remaining as skipped on cancellation.
        harnesses.push({ harness: key, state: "skipped" });
        continue;
      }

      const item = statusMap.get(key);
      if (!item || !item.configured) {
        harnesses.push({ harness: key, state: "skipped" });
        continue;
      }

      try {
        opts.onLog?.("info", `Uninstalling ${key}…`);
        const result = await bridge.uninstall(key, { signal: opts.signal });
        if (result.success) {
          harnesses.push({ harness: key, state: "uninstalled" });
          opts.onLog?.("info", `Uninstalled ${key}.`);
        } else {
          harnesses.push({
            harness: key,
            state: "failed",
            error: result.error ?? "Unknown error",
          });
          opts.onLog?.("error", `Failed to uninstall ${key}: ${result.error ?? "unknown"}`);
        }
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        harnesses.push({ harness: key, state: "failed", error: message });
        opts.onLog?.("error", `Failed to uninstall ${key}: ${message}`);
      }
    }

    // Step 3: Courtesy codex buffer stop.
    if (codexBufferRunning) {
      try {
        opts.onLog?.("info", "Stopping Codex buffer…");
        await bridge.codexBufferStop({ signal: opts.signal });
      } catch {
        // Ignore — courtesy stop.
      }
    }
  }

  // Step 4: Venv removal.
  const venvDir = path.join(os.homedir(), ".arize", "harness", "venv");
  let venvRemoved: boolean;
  let venvError: string | undefined;

  if (opts.removeVenv !== false && !opts.signal?.aborted) {
    const existed = fs.existsSync(venvDir);
    if (!existed) {
      venvRemoved = true;
      opts.onLog?.("info", "Venv directory does not exist; nothing to remove.");
    } else {
      try {
        opts.onLog?.("info", "Removing venv…");
        await fs.promises.rm(venvDir, {
          recursive: true,
          force: true,
          maxRetries: 5,
          retryDelay: 200,
        });
        venvRemoved = true;
        opts.onLog?.("info", "Venv removed.");
      } catch (err) {
        venvRemoved = false;
        venvError = err instanceof Error ? err.message : String(err);
        opts.onLog?.("error", `Failed to remove venv: ${venvError}`);
      }
    }
  } else if (opts.removeVenv === false) {
    // removeVenv === false: report prior on-disk state without removing.
    venvRemoved = !fs.existsSync(venvDir);
  } else {
    // Signal was aborted: skip venv removal, report prior on-disk state.
    venvRemoved = !fs.existsSync(venvDir);
    opts.onLog?.("info", "Cancelled; skipping venv removal.");
  }

  // Step 5: Compute ok.
  const ok = harnesses.every((h) => h.state !== "failed") && venvRemoved;

  return { ok, harnesses, venvRemoved, venvError };
}
