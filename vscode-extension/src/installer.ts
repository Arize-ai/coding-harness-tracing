/**
 * Thin wizard-facing abstraction over src/bridge.ts.
 * Exists so wizard.ts can be tested without spawning real processes.
 */

import * as bridge from "./bridge";
import type {
  HarnessKey,
  InstallRequest,
  StatusPayload,
  OperationResult,
} from "./types";

export interface InstallerBridge {
  install(
    req: InstallRequest,
    onLog: (level: "info" | "error", msg: string) => void,
    signal?: AbortSignal,
  ): Promise<OperationResult>;

  uninstall(
    harness: HarnessKey,
    onLog: (level: "info" | "error", msg: string) => void,
    signal?: AbortSignal,
  ): Promise<OperationResult>;

  setUserId(userId: string): Promise<OperationResult>;

  loadStatus(): Promise<StatusPayload>;
}

export function createBridgeInstaller(): InstallerBridge {
  return {
    async install(req, onLog, signal) {
      try {
        return await bridge.install(req, { onLog, signal });
      } catch (err) {
        return {
          success: false,
          error: "install_failed",
          harness: req.harness,
          logs: [String(err)],
        };
      }
    },

    async uninstall(harness, onLog, signal) {
      try {
        return await bridge.uninstall(harness, { onLog, signal });
      } catch (err) {
        return {
          success: false,
          error: "uninstall_failed",
          harness,
          logs: [String(err)],
        };
      }
    },

    async setUserId(userId) {
      try {
        return await bridge.setUserId(userId);
      } catch (err) {
        return {
          success: false,
          error: "set_user_id_failed",
          harness: null,
          logs: [String(err)],
        };
      }
    },

    async loadStatus() {
      return bridge.getStatus();
    },
  };
}
