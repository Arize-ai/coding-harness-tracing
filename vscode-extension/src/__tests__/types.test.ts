/**
 * Tests for types.ts — sanity checks for the harness key list and the
 * KiroOptions shape introduced for the Kiro harness.
 */

import { HARNESS_KEYS } from "../types";
import type { HarnessStatusItem, InstallRequest, KiroOptions } from "../types";

describe("HARNESS_KEYS", () => {
  it("contains kiro", () => {
    expect(HARNESS_KEYS).toContain("kiro");
  });

  it("has 6 entries", () => {
    expect(HARNESS_KEYS.length).toBe(6);
  });
});

describe("KiroOptions", () => {
  it("compiles with the expected shape", () => {
    const opts: KiroOptions = { agent_name: "arize-traced", set_default: false };
    expect(opts.agent_name).toBe("arize-traced");
    expect(opts.set_default).toBe(false);
  });
});

describe("HarnessStatusItem.repo_paths", () => {
  it("accepts a list of repo paths", () => {
    const item: HarnessStatusItem = {
      name: "copilot",
      configured: true,
      project_name: "demo",
      backend: null,
      scope: null,
      kiro_options: null,
      repo_paths: ["/a", "/b"],
    };
    expect(item.repo_paths).toEqual(["/a", "/b"]);
  });

  it("accepts null", () => {
    const item: HarnessStatusItem = {
      name: "claude-code",
      configured: false,
      project_name: null,
      backend: null,
      scope: null,
      kiro_options: null,
      repo_paths: null,
    };
    expect(item.repo_paths).toBeNull();
  });
});

describe("InstallRequest.repo_path", () => {
  it("accepts a string path", () => {
    const req: InstallRequest = {
      harness: "copilot",
      backend: {
        target: "arize",
        endpoint: "https://example.com",
        api_key: "k",
        space_id: null,
      },
      project_name: "demo",
      user_id: null,
      with_skills: false,
      logging: null,
      kiro_options: null,
      repo_path: "/workspace/repo",
    };
    expect(req.repo_path).toBe("/workspace/repo");
  });

  it("accepts null", () => {
    const req: InstallRequest = {
      harness: "claude-code",
      backend: {
        target: "phoenix",
        endpoint: "https://example.com",
        api_key: "k",
        space_id: null,
      },
      project_name: "demo",
      user_id: null,
      with_skills: false,
      logging: null,
      kiro_options: null,
      repo_path: null,
    };
    expect(req.repo_path).toBeNull();
  });
});
