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
//   { type, sessionID, messages, childSessions: {info,messages,parentCallID,...}[] }

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

  function successfulSession(response: any, expectedID: string): any | undefined {
    if (response?.error) return undefined
    const data = response?.data ?? response
    return data?.id === expectedID ? data : undefined
  }

  function successfulMessages(response: any, expectedID: string): any[] | undefined {
    if (response?.error) return undefined
    const data = response?.data ?? response
    if (!Array.isArray(data)) return undefined
    for (const message of data) {
      if (!message || typeof message !== "object" || Array.isArray(message)) return undefined
      const info = message.info
      const parts = message.parts
      if (!info || typeof info !== "object" || Array.isArray(info) || !Array.isArray(parts)) return undefined
      if (!info.id || info.sessionID !== expectedID) return undefined
      for (const part of parts) {
        if (!part || typeof part !== "object" || Array.isArray(part)) return undefined
        if (part.sessionID !== expectedID) return undefined
      }
    }
    return data
  }

  async function fetchChildSessions(messages: any[], seen = new Set<string>()): Promise<any[]> {
    const children: any[] = []
    for (const message of messages ?? []) {
      for (const part of message?.parts ?? []) {
        const state = part?.state
        const childSessionID = state?.metadata?.sessionId
        if (part?.type !== "tool" || part?.tool !== "task" || !childSessionID || seen.has(childSessionID)) {
          continue
        }
        seen.add(childSessionID)
        try {
          const [sessionRes, messagesRes] = await Promise.all([
            client.session.get({ path: { id: childSessionID } }),
            client.session.messages({ path: { id: childSessionID } }),
          ])
          const info = successfulSession(sessionRes, childSessionID)
          const childMessages = successfulMessages(messagesRes, childSessionID)
          if (!info || !childMessages || !info.parentID || info.parentID !== part?.sessionID) continue
          children.push({
            sessionID: childSessionID,
            parentSessionID: info.parentID,
            parentCallID: part?.callID ?? "",
            info,
            messages: childMessages,
          })
          children.push(...await fetchChildSessions(childMessages, seen))
        } catch {
          /* fail-soft: preserve the root snapshot if a child vanished */
        }
      }
    }
    return children
  }

  async function snapshot(sessionID: string, kind: "reconcile" | "close"): Promise<void> {
    if (!sessionID) return
    try {
      const sessionInfoRes = await client.session.get({ path: { id: sessionID } })
      const sessionInfo = successfulSession(sessionInfoRes, sessionID)
      if (!sessionInfo || sessionInfo.parentID) return
      const res = await client.session.messages({ path: { id: sessionID } })
      const messages = successfulMessages(res, sessionID)
      if (!messages) return
      const childSessions = await fetchChildSessions(messages)
      const type = kind
      forward({ type, sessionID, messages, childSessions })
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
