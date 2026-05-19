/**
 * TypeScript client for the arize-vscode-bridge CLI.
 *
 * Spawns the bridge binary, consumes its NDJSON stdout stream, dispatches
 * log events to the caller, and resolves with the final result payload.
 */

import { spawn } from "child_process";
import { findBridgeBinary } from "./python";
import type {
  HarnessKey,
  InstallRequest,
  StatusPayload,
  OperationResult,
  CodexBufferPayload,
} from "./types";

/** Options shared by every bridge call. */
export interface RunOptions {
  onLog?: (level: "info" | "error", message: string) => void;
  signal?: AbortSignal;
}

// ── internal runner ────────────────────────────────────────────────────

type LogEvent = { event: "log"; level: "info" | "error"; message: string };
type ResultEvent = { event: "result"; payload: unknown };
type BridgeEvent = LogEvent | ResultEvent;

function isLogEvent(obj: unknown): obj is LogEvent {
  return (
    typeof obj === "object" &&
    obj !== null &&
    (obj as Record<string, unknown>).event === "log"
  );
}

function isResultEvent(obj: unknown): obj is ResultEvent {
  return (
    typeof obj === "object" &&
    obj !== null &&
    (obj as Record<string, unknown>).event === "result"
  );
}

/**
 * Spawn the bridge binary with the given argv and return the result payload.
 */
async function runBridge<T>(argv: string[], opts?: RunOptions): Promise<T> {
  let binary: string | null;
  try {
    binary = await findBridgeBinary();
  } catch {
    binary = null;
  }
  if (!binary) {
    throw new Error("bridge: binary not found");
  }

  if (opts?.signal?.aborted) {
    throw new Error("bridge: aborted");
  }

  const bin = binary; // capture for closure narrowing

  return new Promise<T>((resolve, reject) => {
    let settled = false;
    const settle = <A extends unknown[]>(fn: (...args: A) => void, ...args: A) => {
      if (settled) return;
      settled = true;
      cleanup();
      fn(...args);
    };

    const child = spawn(bin, argv, { stdio: ["ignore", "pipe", "pipe"] });

    let resultPayload: T | undefined;
    let stderrBuf = "";
    let stdoutBuf = "";

    const onAbort = () => {
      child.kill("SIGTERM");
      settle(reject, new Error("bridge: aborted"));
    };

    const cleanup = () => {
      if (opts?.signal) {
        opts.signal.removeEventListener("abort", onAbort);
      }
    };

    if (opts?.signal) {
      opts.signal.addEventListener("abort", onAbort, { once: true });
    }

    child.stdout.on("data", (chunk: Buffer) => {
      stdoutBuf += chunk.toString();
      let nlIndex: number;
      while ((nlIndex = stdoutBuf.indexOf("\n")) !== -1) {
        const line = stdoutBuf.slice(0, nlIndex);
        stdoutBuf = stdoutBuf.slice(nlIndex + 1);
        if (!line) continue;

        let parsed: BridgeEvent;
        try {
          parsed = JSON.parse(line) as BridgeEvent;
        } catch {
          opts?.onLog?.("error", line);
          continue;
        }

        if (isLogEvent(parsed)) {
          opts?.onLog?.(parsed.level, parsed.message);
        } else if (isResultEvent(parsed)) {
          resultPayload = parsed.payload as T;
        } else {
          opts?.onLog?.("error", line);
        }
      }
    });

    child.stderr.on("data", (chunk: Buffer) => {
      stderrBuf += chunk.toString();
    });

    child.on("close", (code: number | null) => {
      if (code === 2) {
        return settle(reject, new Error(`bridge: argv error: ${stderrBuf.trim() || "unknown"}`));
      }

      if (resultPayload === undefined) {
        return settle(reject, new Error("bridge: no result emitted"));
      }

      settle(resolve, resultPayload);
    });

    child.on("error", (err: Error) => {
      settle(reject, new Error(`bridge: spawn error: ${err.message}`));
    });
  });
}

// ── public API ─────────────────────────────────────────────────────────

export async function getStatus(opts?: RunOptions): Promise<StatusPayload> {
  return runBridge<StatusPayload>(["status"], opts);
}

export async function install(
  req: InstallRequest,
  opts?: RunOptions
): Promise<OperationResult> {
  const argv: string[] = [
    "install",
    "--harness",
    req.harness,
    "--target",
    req.backend.target,
    "--endpoint",
    req.backend.endpoint,
    "--api-key",
    req.backend.api_key,
    "--project-name",
    req.project_name,
  ];

  if (req.backend.space_id) {
    argv.push("--space-id", req.backend.space_id);
  }
  if (req.user_id) {
    argv.push("--user-id", req.user_id);
  }
  if (req.with_skills) {
    argv.push("--with-skills");
  }
  if (req.logging) {
    argv.push("--log-prompts", String(req.logging.prompts));
    argv.push("--log-tool-details", String(req.logging.tool_details));
    argv.push("--log-tool-content", String(req.logging.tool_content));
  }
  if (req.repo_path) {
    argv.push("--repo-path", req.repo_path);
  }
  if (req.harness === "kiro" && req.kiro_options) {
    argv.push("--agent-name", req.kiro_options.agent_name);
    if (req.kiro_options.set_default) {
      argv.push("--set-default");
    }
  }

  return runBridge<OperationResult>(argv, opts);
}

export async function uninstall(
  harness: HarnessKey,
  opts?: RunOptions
): Promise<OperationResult> {
  return runBridge<OperationResult>(["uninstall", "--harness", harness], opts);
}

export async function setUserId(
  userId: string,
  opts?: RunOptions
): Promise<OperationResult> {
  return runBridge<OperationResult>(["set-user-id", "--user-id", userId], opts);
}

export async function codexBufferStatus(
  opts?: RunOptions
): Promise<CodexBufferPayload> {
  return runBridge<CodexBufferPayload>(["codex-buffer-status"], opts);
}

export async function codexBufferStart(
  opts?: RunOptions
): Promise<CodexBufferPayload> {
  return runBridge<CodexBufferPayload>(["codex-buffer-start"], opts);
}

export async function codexBufferStop(
  opts?: RunOptions
): Promise<CodexBufferPayload> {
  return runBridge<CodexBufferPayload>(["codex-buffer-stop"], opts);
}
