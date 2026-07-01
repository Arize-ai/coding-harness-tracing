// Arize omp tracing hook (shim).
//
// This file ships in the repo and is copied into the user's omp extensions
// dir (~/.omp/extensions/) by the installer, then registered by absolute path
// in the "extensions" array of ~/.omp/agent/settings.json (omp does NOT
// auto-discover an extensions dir). omp loads it in-process inside its own Bun
// runtime. The shim is a DUMB BRIDGE: it contains no tracing logic. On a small
// whitelist of once-fired lifecycle events it spawns the Python entry point
// `arize-hook-omp` (detached, fire-and-forget) with the event payload piped to
// stdin. ALL parsing, span building, and token math happens in the Python
// handler (tracing/omp/hooks/handlers.py).
//
// Verified against omp's HookAPI / examples (https://omp.sh/docs/hooks and
// packages/coding-agent/examples/hooks/*.ts, e.g. auto-commit-on-exit.ts):
//   - a hook default-exports a factory `function (pi: HookAPI)`;
//   - handlers register via `pi.on(eventName, (event, ctx) => ...)`;
//   - `session_shutdown` fires with an empty event, so the session id is read
//     from the hook context via ctx.sessionManager.getSessionId(), not the event.
// `import type` is erasable, so Bun runs this file directly with no npm deps.
//
// Forwarded payload contract (do not change top-level type/sessionId without
// updating the Python handler):
//   { type, sessionId, ...eventFields }

import { spawn } from "node:child_process";
import { homedir, platform } from "node:os";
import { join } from "node:path";
import type { HookAPI, HookContext } from "@oh-my-pi/pi-coding-agent";

function binaryPath(): string {
  const base = join(homedir(), ".arize", "harness", "venv");
  return platform() === "win32"
    ? join(base, "Scripts", "arize-hook-omp.exe")
    : join(base, "bin", "arize-hook-omp");
}

function forward(payload: unknown): void {
  try {
    const child = spawn(binaryPath(), [], {
      stdio: ["pipe", "ignore", "ignore"],
      detached: true,
    });
    child.on("error", () => {});
    child.stdin?.write(JSON.stringify(payload));
    child.stdin?.end();
    child.unref();
  } catch {
    /* fail-soft: tracing must never break the host */
  }
}

// Resolve the omp session id from the hook context. session_shutdown's event
// payload is empty, so every payload is stamped with the id read from ctx.
// ReadonlySessionManager exposes getSessionId() (a method) — verified against
// packages/coding-agent/src/session/session-manager.ts.
function sessionIdOf(ctx: HookContext): string {
  try {
    return ctx.sessionManager.getSessionId() || "";
  } catch {
    return "";
  }
}

export default function (pi: HookAPI): void {
  pi.on("before_agent_start", async (event, ctx) => {
    try {
      forward({ type: "before_agent_start", sessionId: sessionIdOf(ctx), prompt: event.prompt });
    } catch {
      /* fail-soft */
    }
  });

  pi.on("turn_end", async (event, ctx) => {
    try {
      forward({
        type: "turn_end",
        sessionId: sessionIdOf(ctx),
        turnIndex: event.turnIndex,
        message: event.message,
        toolResults: event.toolResults,
      });
    } catch {
      /* fail-soft */
    }
  });

  pi.on("agent_end", async (event, ctx) => {
    try {
      forward({ type: "agent_end", sessionId: sessionIdOf(ctx), messages: event.messages });
    } catch {
      /* fail-soft */
    }
  });

  pi.on("session_shutdown", async (_event, ctx) => {
    try {
      forward({ type: "session_shutdown", sessionId: sessionIdOf(ctx) });
    } catch {
      /* fail-soft */
    }
  });
}
