/**
 * Derives a normalised view-state from raw bridge status data.
 *
 * Pure function — consumed by both the status bar and sidebar state.
 */

import type { StatusPayload, CodexBufferPayload } from "./types";
import { HARNESS_KEYS } from "./types";

// ── Derived state enum ────────────────────────────────────────────────

export enum DerivedState {
  PythonMissing = "pythonMissing",
  BridgeMissing = "bridgeMissing",
  BridgeError = "bridgeError",
  NoHarnesses = "noHarnesses",
  Configured = "configured",
}

// ── Derived status interface ──────────────────────────────────────────

export interface DerivedStatus {
  state: DerivedState;
  configuredCount: number;
  totalCount: number;
  codexBuffer: CodexBufferPayload | null;
  errorMessage: string | null;
}

// ── Input interface ───────────────────────────────────────────────────

export interface DeriveStatusInput {
  pythonFound: boolean;
  bridgeFound: boolean;
  status: StatusPayload | null;
  codexBuffer: CodexBufferPayload | null;
  bridgeError: string | null;
}

// ── Pure derivation function ──────────────────────────────────────────

export function deriveStatus(input: DeriveStatusInput): DerivedStatus {
  const totalCount = HARNESS_KEYS.length;
  const codexBuffer = input.codexBuffer;

  // Rule 1: Python not found
  if (!input.pythonFound) {
    return {
      state: DerivedState.PythonMissing,
      configuredCount: 0,
      totalCount,
      codexBuffer,
      errorMessage: null,
    };
  }

  // Rule 2: Bridge not found
  if (!input.bridgeFound) {
    return {
      state: DerivedState.BridgeMissing,
      configuredCount: 0,
      totalCount,
      codexBuffer,
      errorMessage: null,
    };
  }

  // Rule 3: Bridge threw an exception
  if (input.bridgeError !== null) {
    return {
      state: DerivedState.BridgeError,
      configuredCount: 0,
      totalCount,
      codexBuffer,
      errorMessage: input.bridgeError,
    };
  }

  // Rule 4: Status null or success === false
  if (input.status === null || input.status.success === false) {
    return {
      state: DerivedState.BridgeError,
      configuredCount: 0,
      totalCount,
      codexBuffer,
      errorMessage: input.status?.error ?? "unknown_error",
    };
  }

  // Rule 5 & 6: Count configured harnesses
  const configuredCount = input.status.harnesses.filter(
    (h) => h.configured,
  ).length;

  if (configuredCount === 0) {
    return {
      state: DerivedState.NoHarnesses,
      configuredCount: 0,
      totalCount,
      codexBuffer,
      errorMessage: null,
    };
  }

  // Rule 6: At least one configured
  return {
    state: DerivedState.Configured,
    configuredCount,
    totalCount,
    codexBuffer,
    errorMessage: null,
  };
}
