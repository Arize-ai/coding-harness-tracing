/**
 * TypeScript interfaces mirroring core/vscode_bridge/models.py.
 */

/** Supported harness identifiers. */
export type HarnessKey = "claude-code" | "codex" | "cursor" | "copilot" | "gemini" | "kiro";

/** All supported harness keys, in canonical order. */
export const HARNESS_KEYS: readonly HarnessKey[] = [
  "claude-code",
  "codex",
  "cursor",
  "copilot",
  "gemini",
  "kiro",
] as const;

/** Kiro-specific install/configuration options. */
export interface KiroOptions {
  agent_name: string;
  set_default: boolean;
}

/** Tracing backend configuration. */
export interface Backend {
  target: "arize" | "phoenix";
  endpoint: string;
  api_key: string;
  space_id: string | null;
}

/** Logging flags. */
export interface LoggingFlags {
  prompts: boolean;
  tool_details: boolean;
  tool_content: boolean;
}

/** Status of a single configured harness. */
export interface HarnessStatusItem {
  name: HarnessKey;
  configured: boolean;
  project_name: string | null;
  backend: Backend | null;
  scope: string | null;
  kiro_options: KiroOptions | null;
  repo_paths: string[] | null;
}

/** Full status payload returned by the bridge. */
export interface StatusPayload {
  success: boolean;
  error: string | null;
  user_id: string | null;
  harnesses: HarnessStatusItem[];
  logging: LoggingFlags | null;
  codex_buffer: CodexBufferPayload | null;
}

/** Request to install/configure a harness. */
export interface InstallRequest {
  harness: HarnessKey;
  backend: Backend;
  project_name: string;
  user_id: string | null;
  with_skills: boolean;
  logging: LoggingFlags | null;
  kiro_options: KiroOptions | null;
  repo_path: string | null;
}

/** Result of an install, reconfigure, or uninstall operation. */
export interface OperationResult {
  success: boolean;
  error: string | null;
  harness: string | null;
  logs: string[];
}

/** Codex buffer state payload. */
export interface CodexBufferPayload {
  success: boolean;
  error: string | null;
  state: "running" | "stopped" | "stale" | "unknown";
  host: string | null;
  port: number | null;
  pid: number | null;
}
