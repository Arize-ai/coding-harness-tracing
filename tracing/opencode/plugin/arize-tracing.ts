// Arize opencode tracing plugin (shim).
//
// This file ships in the repo and is copied into the user's opencode global
// plugin dir (~/.config/opencode/plugin/) by the installer. opencode loads it
// in-process inside its Bun runtime. The shim is a DUMB BRIDGE: it contains
// no tracing logic. On a small whitelist of lifecycle events it pulls the
// authoritative session snapshot via the injected opencode SDK client and
// spawns the Python entry point `arize-hook-opencode` (detached,
// fire-and-forget) with that snapshot piped to stdin. ALL parsing, span
// building, and dedup happens in the Python reconciler.
//
// SDK call verified against @opencode-ai/sdk:
//   client.session.messages({ path: { id } }) -> { info, parts }[]
// Some SDK versions wrap the response in { data: ... }; the snapshot helper
// unwraps both shapes.
//
// Forwarded payload contract (do not change without updating the Python
// reconciler):
//   { type: "reconcile" | "close", sessionID: string, messages: {info,parts}[] }

import { spawn } from "node:child_process"
import { homedir, platform } from "node:os"
import { join } from "node:path"

function binaryPath(): string {
  const base = join(homedir(), ".arize", "harness", "venv")
  return platform() === "win32"
    ? join(base, "Scripts", "arize-hook-opencode.exe")
    : join(base, "bin", "arize-hook-opencode")
}

function forward(payload: unknown): void {
  try {
    const child = spawn(binaryPath(), [], {
      stdio: ["pipe", "ignore", "ignore"],
      detached: true,
    })
    child.on("error", () => {})
    child.stdin?.write(JSON.stringify(payload))
    child.stdin?.end()
    child.unref()
  } catch {
    /* fail-soft: tracing must never break the host */
  }
}

export const ArizeTracing = async (ctx: any) => {
  const { client } = ctx

  async function snapshot(sessionID: string, kind: "reconcile" | "close"): Promise<void> {
    if (!sessionID) return
    try {
      const res = await client.session.messages({ path: { id: sessionID } })
      const messages = (res as any)?.data ?? res
      forward({ type: kind, sessionID, messages })
    } catch {
      /* fail-soft */
    }
  }

  return {
    event: async ({ event }: any) => {
      try {
        if (event?.type === "message.updated") {
          const info = event.properties?.info
          if (info?.role === "assistant" && info?.time?.completed) {
            await snapshot(info.sessionID, "reconcile")
          }
        } else if (event?.type === "session.idle") {
          await snapshot(event.properties?.sessionID, "close")
        }
      } catch {
        /* fail-soft */
      }
    },
  }
}
