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
// V1 SDK call verified against @opencode-ai/sdk:
//   client.session.messages({ path: { id } }) -> { info, parts }[]
// Some SDK versions wrap the response in { data: ... }; the snapshot helper
// unwraps both shapes.
// V2 support: see README Limitations.
//
// Forwarded payload contract (do not change without updating the Python
// reconciler):
//   { type, sessionID, messages, childSessions: {info,messages,parentCallID,...}[] }

import { spawn } from "node:child_process"
import { homedir, platform } from "node:os"
import { join } from "node:path"
import { setTimeout as delay } from "node:timers/promises"

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
    child.stdin?.on("error", () => {})
    child.stdin?.write(JSON.stringify(payload))
    child.stdin?.end()
    child.unref()
  } catch {
    /* fail-soft: tracing must never break the host */
  }
}

// Both entrypoints feed the same validated snapshot/child traversal. Only the
// host API and message representation differ; Python remains the reconciler.
function snapshots(session: { get: (id: string) => Promise<any>, messages: (id: string, root?: boolean) => Promise<any> }, signal?: AbortSignal) {

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
        if (signal?.aborted) return children
        const state = part?.state
        const childSessionID = state?.metadata?.sessionID ?? state?.metadata?.sessionId
        if (part?.type !== "tool" || !["task", "subagent"].includes(part?.tool) || !childSessionID || seen.has(childSessionID)) {
          continue
        }
        seen.add(childSessionID)
        try {
          const [sessionRes, messagesRes] = await Promise.all([
            session.get(childSessionID),
            session.messages(childSessionID),
          ])
          if (signal?.aborted) return children
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

  return async function snapshot(sessionID: string, kind: "reconcile" | "close"): Promise<void> {
    if (!sessionID || signal?.aborted) return
    try {
      const sessionInfoRes = await session.get(sessionID)
      const sessionInfo = successfulSession(sessionInfoRes, sessionID)
      if (!sessionInfo || sessionInfo.parentID) return
      if (signal?.aborted) return
      const res = await session.messages(sessionID, true)
      const messages = successfulMessages(res, sessionID)
      if (!messages || signal?.aborted) return
      const childSessions = await fetchChildSessions(messages)
      const type = kind
      if (signal?.aborted) return
      forward({ type, sessionID, messages, childSessions })
    } catch {
      /* fail-soft */
    }
  }
}

const ArizeTracing = async (ctx: any) => {
  const snapshot = snapshots({
    get: (id) => ctx.client.session.get({ path: { id } }),
    messages: (id) => ctx.client.session.messages({ path: { id } }),
  })

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

function v2SnapshotMessages(messages: any[], sessionID: string): any[] {
  if (!Array.isArray(messages)) throw new Error("Invalid session messages")
  let parentID: string | undefined
  return messages.filter((message) => message?.type === "user" || message?.type === "assistant").map((message) => {
    if (!message.id || !message.time) {
      throw new Error("Invalid session message")
    }
    if (message.type === "user") parentID = message.id
    const info = {
      id: message.id,
      role: message.type,
      sessionID,
      parentID: message.type === "assistant" ? parentID : undefined,
      time: message.time,
      modelID: message.model?.id,
      providerID: message.model?.providerID,
      agent: message.agent,
      tokens: message.tokens,
      cost: message.cost,
      error: message.error,
      finish: message.finish,
    }
    const content = message.type === "user" ? [{ type: "text", text: message.text }] : message.content
    if (!Array.isArray(content)) throw new Error("Invalid message content")
    const parts = content.map((part: any, index: number) => {
      if (!part || typeof part !== "object") {
        throw new Error("Invalid message content")
      }
      const base = { id: `${message.id}-${index}`, messageID: message.id, sessionID }
      if (part.type !== "tool") return { ...part, ...base }
      if (!part.id || !part.name || !part.state || typeof part.state !== "object") {
        throw new Error("Invalid tool state")
      }
      return {
        ...base,
        type: "tool",
        callID: part.id,
        tool: part.name,
        state: {
          status: part.state.status,
          input: part.state.input,
          metadata: part.state.metadata,
          error: part.state.error?.message,
          output: Array.isArray(part.state.content)
            ? part.state.content.map((item: any) => typeof item === "string" ? item : (item.text ?? "")).join("\n")
            : undefined,
          time: { start: part.time?.ran ?? part.time?.created, end: part.time?.completed },
        },
      }
    })
    return { info, parts }
  })
}

// Same dual entrypoint shape as the migration guide (Plugin.define is an
// identity helper). No runtime SDK import: the installer ships one .ts file.
// V1 object entrypoints require OpenCode >=1.3.4 (README).
export default {
  id: "arize-tracing",
  server: ArizeTracing,
  setup(ctx: any) {
    const controller = new AbortController()
    // Compaction can drop a turn's user message from context; keep the latest one.
    const lastUser = new Map<string, any>()
    const snapshot = snapshots({
      get: (sessionID) => ctx.session.get({ sessionID }),
      messages: async (sessionID, root) => {
        const context = await ctx.session.context({ sessionID })
        if (!root || !Array.isArray(context)) return v2SnapshotMessages(context, sessionID)
        const first = context.find((message: any) => message?.type === "user" || message?.type === "assistant")
        const remembered = lastUser.get(sessionID)
        const latest = context.findLast((message: any) => message?.type === "user")
        if (latest) lastUser.set(sessionID, latest)
        return v2SnapshotMessages(first?.type === "assistant" && remembered ? [remembered, ...context] : context, sessionID)
      },
    }, controller.signal)
    void (async () => {
      while (!controller.signal.aborted) {
        try {
          for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
            if (controller.signal.aborted) return
            const sessionID = event.data?.sessionID
            if (!sessionID) continue
            if (event.type === "session.step.ended" || event.type === "session.step.failed") {
              await snapshot(sessionID, "reconcile")
            } else if (
              event.type === "session.execution.succeeded" ||
              event.type === "session.execution.failed" ||
              (event.type === "session.execution.interrupted" && event.data.reason !== "shutdown")
            ) {
              await snapshot(sessionID, "close")
              lastUser.delete(sessionID)
            }
          }
        } catch {
          /* fail-soft: a disconnected stream must not break the host */
        }
        if (controller.signal.aborted) return
        await delay(5000, undefined, { signal: controller.signal, ref: false })
      }
    })().catch(() => { /* cleanup aborts the reconnect delay */ })
    return () => {
      controller.abort()
      lastUser.clear()
    }
  },
}
