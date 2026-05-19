/**
 * @jest-environment jsdom
 */

const fs = require("fs");
const path = require("path");

const HARNESS_KEYS = ["claude-code", "codex", "cursor", "copilot", "gemini", "kiro"];

let postMessageCalls;

// Track message listeners so we can clean up between tests.
// Each eval of wizard.js adds a new "message" listener; without cleanup,
// duplicate listeners accumulate and cause inflated DOM mutations.
let _messageListeners = [];
const _origAddEventListener = window.addEventListener.bind(window);

function setupWizard() {
  // Remove any message listeners left by a prior test
  _messageListeners.forEach((fn) => window.removeEventListener("message", fn));
  _messageListeners = [];

  // Intercept addEventListener to track message handlers
  window.addEventListener = function (type, fn, opts) {
    if (type === "message") _messageListeners.push(fn);
    return _origAddEventListener(type, fn, opts);
  };

  // Reset DOM
  document.body.innerHTML = '<div id="wizard-root"></div>';
  postMessageCalls = [];

  // Provide acquireVsCodeApi global
  global.acquireVsCodeApi = () => ({
    postMessage: (msg) => postMessageCalls.push(msg),
  });

  // Load wizard.js by evaluating it in the current context
  const scriptPath = path.join(__dirname, "..", "media", "wizard.js");
  const scriptContent = fs.readFileSync(scriptPath, "utf-8");

  // The script uses DOMContentLoaded; since readyState is already "complete"
  // in jsdom, the init() branch that runs immediately will fire.
  eval(scriptContent);
}

function dispatchMessage(data) {
  const event = new MessageEvent("message", { data });
  window.dispatchEvent(event);
}

function clickElement(el) {
  el.dispatchEvent(new MouseEvent("click", { bubbles: true }));
}

function getHarnessCards() {
  return document.querySelectorAll(".harness-card");
}

function getNextButton() {
  const buttons = document.querySelectorAll(".btn-primary");
  for (const btn of buttons) {
    if (btn.textContent === "Next") return btn;
  }
  return null;
}

function getInstallButton() {
  return document.getElementById("install-btn");
}

// ---- Tests ----

describe("Wizard UI", () => {
  beforeEach(() => {
    setupWizard();
    // "ready" should be the first postMessage
    expect(postMessageCalls).toEqual([{ type: "ready" }]);
    postMessageCalls.length = 0;
  });

  test("all five harness cards render in step 1, in documented order", () => {
    const cards = getHarnessCards();
    expect(cards.length).toBe(HARNESS_KEYS.length);
    const keys = Array.from(cards).map((c) => c.getAttribute("data-harness"));
    expect(keys).toEqual(HARNESS_KEYS);
  });

  test("selecting a card enables Next", () => {
    const nextBtn = getNextButton();
    expect(nextBtn).not.toBeNull();
    expect(nextBtn.disabled).toBe(true);

    const card = getHarnessCards()[0]; // claude-code
    clickElement(card);

    const nextBtnAfter = getNextButton();
    expect(nextBtnAfter.disabled).toBe(false);
  });

  test("step 2 backend toggle: arize shows space_id field, phoenix hides it", () => {
    // Select harness and go to step 2
    clickElement(getHarnessCards()[0]);
    clickElement(getNextButton());

    // Default is arize — space_id should be visible
    const spaceIdField = document.getElementById("field-space_id");
    expect(spaceIdField).not.toBeNull();

    // Switch to phoenix
    const phoenixBtn = document.querySelector('[data-backend="phoenix"]');
    clickElement(phoenixBtn);

    const spaceIdFieldAfter = document.getElementById("field-space_id");
    expect(spaceIdFieldAfter).toBeNull();
  });

  test("step 3 logging toggles default to on", () => {
    // Navigate to step 3
    clickElement(getHarnessCards()[0]);
    clickElement(getNextButton());

    // Fill required fields for arize to enable Next
    setInputValue("field-endpoint", "otlp.arize.com:443");
    setInputValue("field-api_key", "test-key");
    setInputValue("field-space_id", "test-space");
    clickElement(getNextButton());

    // Check logging toggles
    const prompts = document.getElementById("field-log_prompts");
    const toolDetails = document.getElementById("field-log_tool_details");
    const toolContent = document.getElementById("field-log_tool_content");

    expect(prompts).not.toBeNull();
    expect(prompts.checked).toBe(true);
    expect(toolDetails.checked).toBe(true);
    expect(toolContent.checked).toBe(true);
  });

  test("clicking Install emits postMessage with type install and correct request shape", () => {
    // Navigate through all steps
    clickElement(getHarnessCards()[1]); // codex
    clickElement(getNextButton());

    setInputValue("field-endpoint", "otlp.arize.com:443");
    setInputValue("field-api_key", "my-key");
    setInputValue("field-space_id", "my-space");
    clickElement(getNextButton());

    setInputValue("field-project_name", "my-project");
    clickElement(getNextButton());

    // Now on step 4 — click Install
    const installBtn = getInstallButton();
    expect(installBtn).not.toBeNull();
    clickElement(installBtn);

    const installMsg = postMessageCalls.find((m) => m.type === "install");
    expect(installMsg).toBeDefined();
    expect(installMsg.request).toBeDefined();

    const req = installMsg.request;
    // Assert all required keys are present
    expect(req.harness).toBe("codex");
    expect(req.backend).toBeDefined();
    expect(req.backend.target).toBe("arize");
    expect(req.backend.endpoint).toBe("otlp.arize.com:443");
    expect(req.backend.api_key).toBe("my-key");
    expect(req.backend.space_id).toBe("my-space");
    expect(req.project_name).toBe("my-project");
    expect(req).toHaveProperty("user_id");
    expect(req).toHaveProperty("with_skills");
    expect(req).toHaveProperty("logging");
    expect(req.logging).toHaveProperty("prompts");
    expect(req.logging).toHaveProperty("tool_details");
    expect(req.logging).toHaveProperty("tool_content");
  });

  test("receiving log message appends child to #wizard-log with matching class", () => {
    // Navigate to step 4
    clickElement(getHarnessCards()[0]);
    clickElement(getNextButton());

    setInputValue("field-endpoint", "otlp.arize.com:443");
    setInputValue("field-api_key", "k");
    setInputValue("field-space_id", "s");
    clickElement(getNextButton());
    clickElement(getNextButton());

    // Click install to show log area
    clickElement(getInstallButton());

    // Send log messages
    dispatchMessage({ type: "log", level: "info", message: "Starting install..." });
    dispatchMessage({ type: "log", level: "error", message: "Something went wrong" });

    const logEl = document.getElementById("wizard-log");
    expect(logEl).not.toBeNull();

    const children = logEl.querySelectorAll(".log");
    expect(children.length).toBe(2);
    expect(children[0].classList.contains("log-info")).toBe(true);
    expect(children[0].textContent).toBe("Starting install...");
    expect(children[1].classList.contains("log-error")).toBe(true);
    expect(children[1].textContent).toBe("Something went wrong");
  });

  test("receiving result with success shows success state and Close button", () => {
    // Navigate to step 4
    clickElement(getHarnessCards()[0]);
    clickElement(getNextButton());

    setInputValue("field-endpoint", "otlp.arize.com:443");
    setInputValue("field-api_key", "k");
    setInputValue("field-space_id", "s");
    clickElement(getNextButton());
    clickElement(getNextButton());

    clickElement(getInstallButton());

    // Send result
    dispatchMessage({
      type: "result",
      payload: { success: true, error: null, harness: "claude-code", logs: [] },
    });

    // Success banner
    const banner = document.querySelector(".result-banner.success");
    expect(banner).not.toBeNull();

    // Close button
    const closeBtn = Array.from(document.querySelectorAll(".btn-primary")).find(
      (b) => b.textContent === "Close"
    );
    expect(closeBtn).not.toBeNull();

    // Install button should be gone
    expect(getInstallButton()).toBeNull();
  });

  test("receiving result with failure shows failure banner", () => {
    clickElement(getHarnessCards()[0]);
    clickElement(getNextButton());

    setInputValue("field-endpoint", "otlp.arize.com:443");
    setInputValue("field-api_key", "k");
    setInputValue("field-space_id", "s");
    clickElement(getNextButton());
    clickElement(getNextButton());

    clickElement(getInstallButton());

    dispatchMessage({
      type: "result",
      payload: { success: false, error: "timeout", harness: "claude-code", logs: [] },
    });

    const banner = document.querySelector(".result-banner.failure");
    expect(banner).not.toBeNull();
    expect(banner.textContent).toContain("timeout");
  });

  test("cancel button emits cancel message", () => {
    // Navigate to step 4
    clickElement(getHarnessCards()[0]);
    clickElement(getNextButton());

    setInputValue("field-endpoint", "otlp.arize.com:443");
    setInputValue("field-api_key", "k");
    setInputValue("field-space_id", "s");
    clickElement(getNextButton());
    clickElement(getNextButton());

    const cancelBtn = Array.from(document.querySelectorAll(".btn-secondary")).find(
      (b) => b.textContent === "Cancel"
    );
    expect(cancelBtn).not.toBeNull();
    clickElement(cancelBtn);

    const cancelMsg = postMessageCalls.find((m) => m.type === "cancel");
    expect(cancelMsg).toBeDefined();
  });

  test("prefill message pre-selects harness and fills fields", () => {
    dispatchMessage({
      type: "prefill",
      harness: "cursor",
      request: {
        project_name: "my-proj",
        user_id: "user-42",
        backend: { target: "phoenix", endpoint: "http://localhost:9999", api_key: "pk" },
        logging: { prompts: false, tool_details: true, tool_content: false },
      },
    });

    // Harness should be pre-selected
    const selected = document.querySelector(".harness-card.selected");
    expect(selected).not.toBeNull();
    expect(selected.getAttribute("data-harness")).toBe("cursor");

    // Navigate to step 2 — endpoint should be prefilled
    clickElement(getNextButton());
    const endpointField = document.getElementById("field-endpoint");
    expect(endpointField.value).toBe("http://localhost:9999");

    // Backend should be phoenix — no space_id
    expect(document.getElementById("field-space_id")).toBeNull();
  });

  test("uninstall button appears only when prefill provides a harness", () => {
    // Without prefill — no uninstall button on step 4
    clickElement(getHarnessCards()[2]); // cursor
    clickElement(getNextButton());

    // Switch to phoenix so no space_id needed
    clickElement(document.querySelector('[data-backend="phoenix"]'));
    setInputValue("field-endpoint", "http://localhost:6006");
    clickElement(getNextButton());
    clickElement(getNextButton());

    let uninstallBtn = Array.from(document.querySelectorAll(".btn-danger")).find(
      (b) => b.textContent === "Uninstall"
    );
    expect(uninstallBtn).toBeUndefined();
  });

  test("uninstall button emits uninstall message when prefilled", () => {
    dispatchMessage({ type: "prefill", harness: "copilot" });

    // Navigate through to step 4
    clickElement(getNextButton()); // to step 2

    // Switch to phoenix for simpler validation
    clickElement(document.querySelector('[data-backend="phoenix"]'));
    setInputValue("field-endpoint", "http://localhost:6006");
    clickElement(getNextButton()); // to step 3
    clickElement(getNextButton()); // to step 4

    const uninstallBtn = Array.from(document.querySelectorAll(".btn-danger")).find(
      (b) => b.textContent === "Uninstall"
    );
    expect(uninstallBtn).not.toBeUndefined();
    clickElement(uninstallBtn);

    const uninstallMsg = postMessageCalls.find((m) => m.type === "uninstall");
    expect(uninstallMsg).toBeDefined();
    expect(uninstallMsg.harness).toBe("copilot");
  });

  test("step 2 requires api_key and space_id for arize backend", () => {
    clickElement(getHarnessCards()[0]);
    clickElement(getNextButton());

    // Only fill endpoint — Next should be disabled
    setInputValue("field-endpoint", "otlp.arize.com:443");
    let nextBtn = getNextButton();
    expect(nextBtn.disabled).toBe(true);

    // Fill api_key but not space_id — still disabled
    setInputValue("field-api_key", "key");
    nextBtn = getNextButton();
    expect(nextBtn.disabled).toBe(true);

    // Fill space_id — now Next should be enabled
    setInputValue("field-space_id", "space");
    nextBtn = getNextButton();
    expect(nextBtn.disabled).toBe(false);
  });

  test("phoenix backend makes api_key optional", () => {
    clickElement(getHarnessCards()[0]);
    clickElement(getNextButton());

    clickElement(document.querySelector('[data-backend="phoenix"]'));
    setInputValue("field-endpoint", "http://localhost:6006");
    // Don't fill api_key — should still allow Next
    const nextBtn = getNextButton();
    expect(nextBtn.disabled).toBe(false);
  });

  test("step indicator shows correct active/completed dots", () => {
    // Step 0 — first dot active
    let dots = document.querySelectorAll(".wizard-step-dot");
    expect(dots[0].classList.contains("active")).toBe(true);
    expect(dots[1].classList.contains("active")).toBe(false);

    // Go to step 1
    clickElement(getHarnessCards()[0]);
    clickElement(getNextButton());

    dots = document.querySelectorAll(".wizard-step-dot");
    expect(dots[0].classList.contains("completed")).toBe(true);
    expect(dots[1].classList.contains("active")).toBe(true);
  });

  test("log lines are preserved after result re-render", () => {
    // Navigate to step 4 and install
    clickElement(getHarnessCards()[0]);
    clickElement(getNextButton());

    setInputValue("field-endpoint", "otlp.arize.com:443");
    setInputValue("field-api_key", "k");
    setInputValue("field-space_id", "s");
    clickElement(getNextButton());
    clickElement(getNextButton());
    clickElement(getInstallButton());

    // Send log lines
    dispatchMessage({ type: "log", level: "info", message: "Step 1 done" });
    dispatchMessage({ type: "log", level: "error", message: "Step 2 failed" });

    // Send result — triggers re-render
    dispatchMessage({
      type: "result",
      payload: { success: false, error: "Step 2 failed", harness: "claude-code", logs: [] },
    });

    // Log lines should still be present in the new DOM
    const logEl = document.getElementById("wizard-log");
    const children = logEl.querySelectorAll(".log");
    expect(children.length).toBe(2);
    expect(children[0].textContent).toBe("Step 1 done");
    expect(children[1].textContent).toBe("Step 2 failed");
  });

  test("step 3 with_skills defaults to off", () => {
    clickElement(getHarnessCards()[0]);
    clickElement(getNextButton());

    setInputValue("field-endpoint", "otlp.arize.com:443");
    setInputValue("field-api_key", "k");
    setInputValue("field-space_id", "s");
    clickElement(getNextButton());

    const withSkills = document.getElementById("field-with_skills");
    expect(withSkills).not.toBeNull();
    expect(withSkills.checked).toBe(false);
  });

  test("ready message is sent on initialization", () => {
    // beforeEach already checks this, but let's be explicit
    // postMessageCalls was cleared in beforeEach after checking ready
    // The original check in beforeEach confirms ready was sent
    // Re-setup to test from scratch
    setupWizard();
    expect(postMessageCalls[0]).toEqual({ type: "ready" });
  });

  test("step 3 shows workspace folder field only for copilot harness", () => {
    // Non-copilot: no workspace folder field
    clickElement(getHarnessCards()[0]); // claude-code
    clickElement(getNextButton());
    setInputValue("field-endpoint", "otlp.arize.com:443");
    setInputValue("field-api_key", "k");
    setInputValue("field-space_id", "s");
    clickElement(getNextButton());

    expect(document.getElementById("field-copilot_repo_path")).toBeNull();

    // Go back to step 1 and pick copilot
    setupWizard();
    postMessageCalls.length = 0;

    const copilotCard = Array.from(getHarnessCards()).find(
      (c) => c.getAttribute("data-harness") === "copilot"
    );
    clickElement(copilotCard);
    clickElement(getNextButton());
    setInputValue("field-endpoint", "otlp.arize.com:443");
    setInputValue("field-api_key", "k");
    setInputValue("field-space_id", "s");
    clickElement(getNextButton());

    expect(document.getElementById("field-copilot_repo_path")).not.toBeNull();
  });

  test("copilot repo path defaults to workspace_folder from prefill", () => {
    dispatchMessage({
      type: "prefill",
      harness: "copilot",
      workspace_folder: "/my/workspace",
    });

    // Navigate through to step 3
    clickElement(getNextButton()); // step 2
    clickElement(document.querySelector('[data-backend="phoenix"]'));
    setInputValue("field-endpoint", "http://localhost:6006");
    clickElement(getNextButton()); // step 3

    const field = document.getElementById("field-copilot_repo_path");
    expect(field).not.toBeNull();
    expect(field.value).toBe("/my/workspace");
  });

  test("selecting copilot card after prefill workspace_folder defaults the path", () => {
    // Prefill only carries workspace_folder, no harness
    dispatchMessage({ type: "prefill", workspace_folder: "/ws-from-prefill" });

    // User then selects copilot manually
    const copilotCard = Array.from(getHarnessCards()).find(
      (c) => c.getAttribute("data-harness") === "copilot"
    );
    clickElement(copilotCard);
    clickElement(getNextButton());

    clickElement(document.querySelector('[data-backend="phoenix"]'));
    setInputValue("field-endpoint", "http://localhost:6006");
    clickElement(getNextButton());

    const field = document.getElementById("field-copilot_repo_path");
    expect(field.value).toBe("/ws-from-prefill");
  });

  test("clicking Browse posts pickFolder with current value", () => {
    dispatchMessage({
      type: "prefill",
      harness: "copilot",
      workspace_folder: "/ws",
    });

    clickElement(getNextButton());
    clickElement(document.querySelector('[data-backend="phoenix"]'));
    setInputValue("field-endpoint", "http://localhost:6006");
    clickElement(getNextButton());

    postMessageCalls.length = 0;

    // Find the Browse button — it sits in the same .form-group as the
    // copilot_repo_path input.
    const field = document.getElementById("field-copilot_repo_path");
    const formGroup = field.closest(".form-group");
    const browseBtn = Array.from(formGroup.querySelectorAll("button")).find(
      (b) => b.textContent.indexOf("Browse") !== -1
    );
    expect(browseBtn).toBeDefined();
    clickElement(browseBtn);

    const pickMsg = postMessageCalls.find((m) => m.type === "pickFolder");
    expect(pickMsg).toBeDefined();
    expect(pickMsg.current).toBe("/ws");
  });

  test("folderPicked message updates the workspace folder input", () => {
    dispatchMessage({
      type: "prefill",
      harness: "copilot",
      workspace_folder: "/ws",
    });

    clickElement(getNextButton());
    clickElement(document.querySelector('[data-backend="phoenix"]'));
    setInputValue("field-endpoint", "http://localhost:6006");
    clickElement(getNextButton());

    dispatchMessage({ type: "folderPicked", path: "/picked/path" });

    const field = document.getElementById("field-copilot_repo_path");
    expect(field.value).toBe("/picked/path");
  });

  test("folderPicked with null path does not change input value", () => {
    dispatchMessage({
      type: "prefill",
      harness: "copilot",
      workspace_folder: "/ws",
    });

    clickElement(getNextButton());
    clickElement(document.querySelector('[data-backend="phoenix"]'));
    setInputValue("field-endpoint", "http://localhost:6006");
    clickElement(getNextButton());

    const before = document.getElementById("field-copilot_repo_path").value;
    dispatchMessage({ type: "folderPicked", path: null });

    const field = document.getElementById("field-copilot_repo_path");
    expect(field.value).toBe(before);
  });

  test("install request includes repo_path for copilot and null otherwise", () => {
    // Copilot path: install request carries the user-entered repo_path
    dispatchMessage({
      type: "prefill",
      harness: "copilot",
      workspace_folder: "/ws",
    });

    clickElement(getNextButton());
    clickElement(document.querySelector('[data-backend="phoenix"]'));
    setInputValue("field-endpoint", "http://localhost:6006");
    clickElement(getNextButton());

    setInputValue("field-copilot_repo_path", "/some/repo");
    clickElement(getNextButton());

    clickElement(getInstallButton());

    let installMsg = postMessageCalls.find((m) => m.type === "install");
    expect(installMsg).toBeDefined();
    expect(installMsg.request.repo_path).toBe("/some/repo");

    // Non-copilot: repo_path should be null
    setupWizard();
    postMessageCalls.length = 0;

    clickElement(getHarnessCards()[0]); // claude-code
    clickElement(getNextButton());
    setInputValue("field-endpoint", "otlp.arize.com:443");
    setInputValue("field-api_key", "k");
    setInputValue("field-space_id", "s");
    clickElement(getNextButton());
    clickElement(getNextButton());
    clickElement(getInstallButton());

    installMsg = postMessageCalls.find((m) => m.type === "install");
    expect(installMsg).toBeDefined();
    expect(installMsg.request.repo_path).toBeNull();
  });
});

// ---- Helpers ----

function setInputValue(id, value) {
  const input = document.getElementById(id);
  if (!input) throw new Error("Input #" + id + " not found");
  input.value = value;
  input.dispatchEvent(new Event("input", { bubbles: true }));
}
