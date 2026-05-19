/**
 * Arize Agent Kit — Wizard Webview Script
 *
 * Message contract
 * ================
 *
 * Webview → extension host (postMessage):
 *
 *   { type: "ready" }
 *     Sent once on DOMContentLoaded.
 *
 *   { type: "install", request: InstallRequest }
 *     User clicked "Install" on the review step.
 *     InstallRequest = {
 *       harness:      string,            // one of the five harness keys
 *       backend:      Backend,
 *       project_name: string,
 *       user_id:      string | null,
 *       with_skills:  boolean,
 *       logging:      { prompts: boolean, tool_details: boolean, tool_content: boolean } | null,
 *     }
 *     Backend = {
 *       target:   "arize" | "phoenix",
 *       endpoint: string,
 *       api_key:  string,
 *       space_id: string | null,
 *     }
 *
 *   { type: "uninstall", harness: HarnessKey }
 *     User clicked "Uninstall" (available when reconfiguring an existing harness).
 *
 *   { type: "cancel" }
 *     User clicked Cancel; extension host closes the panel.
 *
 * Extension host → webview (message event):
 *
 *   { type: "prefill", harness?: HarnessKey, request?: Partial<InstallRequest> }
 *     Sent in response to "ready". May pre-select a harness and/or fill fields.
 *
 *   { type: "log", level: "info" | "error", message: string }
 *     Streamed installer log line. Appended to #wizard-log.
 *
 *   { type: "result", payload: OperationResult }
 *     Final install/uninstall outcome.
 *     OperationResult = { success: boolean, error: string | null, harness: string | null, logs: string[] }
 */

(function () {
  "use strict";

  // Acquire VS Code API if available (webview context), otherwise stub.
  var vscode;
  try {
    // eslint-disable-next-line no-undef
    vscode = acquireVsCodeApi();
  } catch (_e) {
    vscode = { postMessage: function () {} };
  }

  // ---- Constants ----

  var HARNESS_KEYS = ["claude-code", "codex", "cursor", "copilot", "gemini", "kiro"];
  var HARNESS_LABELS = {
    "claude-code": "Claude Code",
    codex: "Codex",
    cursor: "Cursor",
    copilot: "Copilot",
    gemini: "Gemini",
    kiro: "Kiro",
  };
  var KIRO_DEFAULT_AGENT_NAME = "arize-traced";
  var TOTAL_STEPS = 4;

  var ARIZE_DEFAULT_ENDPOINT = "otlp.arize.com:443";
  var PHOENIX_DEFAULT_ENDPOINT = "http://localhost:6006";

  // ---- State ----

  var state = {
    step: 0,
    harness: null,
    backendTarget: "arize",
    endpoint: "",
    apiKey: "",
    spaceId: "",
    projectName: "",
    userId: "",
    withSkills: false,
    logPrompts: true,
    logToolDetails: true,
    logToolContent: true,
    kiroAgentName: KIRO_DEFAULT_AGENT_NAME,
    kiroSetDefault: false,
    workspaceFolder: "",
    copilotRepoPath: "",
    installing: false,
    resultPayload: null,
    prefillHarness: null,
  };

  // ---- Helpers ----

  function h(tag, attrs, children) {
    var el = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        if (k === "className") {
          el.className = attrs[k];
        } else if (k.indexOf("on") === 0) {
          el.addEventListener(k.slice(2).toLowerCase(), attrs[k]);
        } else if (k === "htmlFor") {
          el.setAttribute("for", attrs[k]);
        } else {
          el.setAttribute(k, attrs[k]);
        }
      });
    }
    if (children != null) {
      if (!Array.isArray(children)) children = [children];
      children.forEach(function (c) {
        if (c == null) return;
        if (typeof c === "string" || typeof c === "number") {
          el.appendChild(document.createTextNode(String(c)));
        } else {
          el.appendChild(c);
        }
      });
    }
    return el;
  }

  // ---- Render ----

  function render() {
    var root = document.getElementById("wizard-root");
    if (!root) return;
    root.innerHTML = "";

    // Step indicator
    var dots = [];
    for (var i = 0; i < TOTAL_STEPS; i++) {
      var cls = "wizard-step-dot";
      if (i < state.step) cls += " completed";
      if (i === state.step) cls += " active";
      dots.push(h("div", { className: cls }));
    }
    root.appendChild(h("div", { className: "wizard-steps-indicator" }, dots));

    // Render active step
    var stepEl;
    switch (state.step) {
      case 0:
        stepEl = renderStep1();
        break;
      case 1:
        stepEl = renderStep2();
        break;
      case 2:
        stepEl = renderStep3();
        break;
      case 3:
        stepEl = renderStep4();
        break;
    }
    if (stepEl) {
      stepEl.classList.add("wizard-step", "visible");
      root.appendChild(stepEl);
    }
  }

  // ---- Step 1: Harness ----

  function renderStep1() {
    var container = h("div", null, [
      h("h2", null, "Select Harness"),
      h("p", { className: "subtitle" }, "Choose the AI coding agent to configure."),
    ]);

    var cards = h("div", { className: "harness-cards" });
    HARNESS_KEYS.forEach(function (key) {
      var cls = "harness-card";
      if (state.harness === key) cls += " selected";
      var card = h("div", { className: cls, "data-harness": key, onClick: function () {
        state.harness = key;
        if (!state.projectName) state.projectName = key;
        if (state.harness === "copilot" && !state.copilotRepoPath) {
          state.copilotRepoPath = state.workspaceFolder;
        }
        render();
      } }, [
        h("span", { className: "card-label" }, HARNESS_LABELS[key]),
      ]);
      cards.appendChild(card);
    });
    container.appendChild(cards);

    // Nav
    container.appendChild(renderNav(null, state.harness != null));
    return container;
  }

  // ---- Step 2: Backend ----

  function renderStep2() {
    var container = h("div", null, [
      h("h2", null, "Configure Backend"),
      h("p", { className: "subtitle" }, "Where should traces be sent?"),
    ]);

    // Toggle
    var toggle = h("div", { className: "backend-toggle" });
    ["arize", "phoenix"].forEach(function (t) {
      var cls = "backend-btn";
      if (state.backendTarget === t) cls += " selected";
      toggle.appendChild(
        h("button", {
          type: "button",
          className: cls,
          "data-backend": t,
          onClick: function () {
            state.backendTarget = t;
            // Auto-fill endpoint with the new target's default unless the
            // user has set a value that isn't either default (i.e. a
            // genuinely custom endpoint they typed).
            if (!state.endpoint || state.endpoint === ARIZE_DEFAULT_ENDPOINT || state.endpoint === PHOENIX_DEFAULT_ENDPOINT) {
              state.endpoint = t === "arize" ? ARIZE_DEFAULT_ENDPOINT : PHOENIX_DEFAULT_ENDPOINT;
            }
            render();
          },
        }, t.charAt(0).toUpperCase() + t.slice(1))
      );
    });
    container.appendChild(toggle);

    // Endpoint
    container.appendChild(
      formGroup("Endpoint", "text", "endpoint", state.endpoint || defaultEndpoint(), function (v) {
        state.endpoint = v;
      })
    );

    // API Key
    container.appendChild(
      formGroup("API Key", "password", "api_key", state.apiKey, function (v) {
        state.apiKey = v;
      }, state.backendTarget === "phoenix" ? "(optional)" : null)
    );

    // Space ID (arize only)
    if (state.backendTarget === "arize") {
      container.appendChild(
        formGroup("Space ID", "text", "space_id", state.spaceId, function (v) {
          state.spaceId = v;
        })
      );
    }

    var canNext = step2Valid();
    container.appendChild(renderNav(true, canNext));
    return container;
  }

  function defaultEndpoint() {
    return state.backendTarget === "arize" ? ARIZE_DEFAULT_ENDPOINT : PHOENIX_DEFAULT_ENDPOINT;
  }

  function step2Valid() {
    var ep = state.endpoint || defaultEndpoint();
    if (!ep) return false;
    if (state.backendTarget === "arize") {
      if (!state.apiKey) return false;
      if (!state.spaceId) return false;
    }
    return true;
  }

  // ---- Step 3: Options ----

  function renderStep3() {
    var container = h("div", null, [
      h("h2", null, "Options"),
      h("p", { className: "subtitle" }, "Configure project settings and logging."),
    ]);

    container.appendChild(
      formGroup("Project Name", "text", "project_name", state.projectName, function (v) {
        state.projectName = v;
      })
    );

    container.appendChild(
      formGroup("User ID", "text", "user_id", state.userId, function (v) {
        state.userId = v;
      }, "(optional)")
    );

    if (state.harness === "copilot") {
      container.appendChild(copilotRepoPathRow());
    }

    if (state.harness === "kiro") {
      container.appendChild(
        formGroup(
          "Kiro Agent Name",
          "text",
          "kiro_agent_name",
          state.kiroAgentName,
          function (v) {
            state.kiroAgentName = v;
          },
          "Default: " + KIRO_DEFAULT_AGENT_NAME + " — name of the ~/.kiro/agents/<name>.json file to install hooks into"
        )
      );
      container.appendChild(
        checkboxRow(
          "kiro_set_default",
          "Set as Kiro's default agent",
          state.kiroSetDefault,
          function (v) {
            state.kiroSetDefault = v;
          }
        )
      );
    }

    container.appendChild(
      checkboxRow("with_skills", "Enable skills", state.withSkills, function (v) {
        state.withSkills = v;
      })
    );

    var loggingHeader = h("div", { className: "form-group" }, [
      h("label", null, "Logging"),
    ]);
    container.appendChild(loggingHeader);

    container.appendChild(
      checkboxRow("log_prompts", "Log prompts", state.logPrompts, function (v) {
        state.logPrompts = v;
      })
    );
    container.appendChild(
      checkboxRow("log_tool_details", "Log tool details", state.logToolDetails, function (v) {
        state.logToolDetails = v;
      })
    );
    container.appendChild(
      checkboxRow("log_tool_content", "Log tool content", state.logToolContent, function (v) {
        state.logToolContent = v;
      })
    );

    container.appendChild(renderNav(true, true));
    return container;
  }

  // ---- Step 4: Review & Install ----

  function renderStep4() {
    var container = h("div", null, [
      h("h2", null, "Review & Install"),
      h("p", { className: "subtitle" }, "Confirm your configuration."),
    ]);

    var table = h("table", { className: "review-table" });
    var rows = [
      ["Harness", HARNESS_LABELS[state.harness] || state.harness],
      ["Backend", state.backendTarget],
      ["Endpoint", state.endpoint || defaultEndpoint()],
      ["Project Name", state.projectName],
      ["User ID", state.userId || "—"],
      ["With Skills", state.withSkills ? "Yes" : "No"],
      ["Log Prompts", state.logPrompts ? "Yes" : "No"],
      ["Log Tool Details", state.logToolDetails ? "Yes" : "No"],
      ["Log Tool Content", state.logToolContent ? "Yes" : "No"],
    ];
    if (state.harness === "kiro") {
      rows.push(["Kiro Agent Name", state.kiroAgentName || KIRO_DEFAULT_AGENT_NAME]);
      rows.push(["Set as Default Agent", state.kiroSetDefault ? "Yes" : "No"]);
    }
    rows.forEach(function (r) {
      table.appendChild(
        h("tr", null, [h("td", null, r[0]), h("td", null, r[1])])
      );
    });
    container.appendChild(table);

    // Action buttons
    var nav = h("div", { className: "wizard-nav" });

    if (!state.installing && !state.resultPayload) {
      nav.appendChild(
        h("button", { className: "btn btn-secondary", type: "button", onClick: function () {
          vscode.postMessage({ type: "cancel" });
        } }, "Cancel")
      );

      // Uninstall (only for prefilled / reconfigure flow)
      if (state.prefillHarness) {
        nav.appendChild(
          h("button", { className: "btn btn-danger", type: "button", onClick: function () {
            vscode.postMessage({ type: "uninstall", harness: state.harness });
          } }, "Uninstall")
        );
      }

      nav.appendChild(
        h("button", { className: "btn btn-secondary", type: "button", onClick: function () {
          state.step = 2;
          render();
        } }, "Back")
      );

      nav.appendChild(
        h("button", { id: "install-btn", className: "btn btn-primary", type: "button", onClick: doInstall }, "Install")
      );
    }

    if (state.resultPayload) {
      nav.appendChild(
        h("button", { className: "btn btn-primary", type: "button", onClick: function () {
          vscode.postMessage({ type: "cancel" });
        } }, "Close")
      );
    }

    container.appendChild(nav);

    // Log area
    container.appendChild(h("pre", { id: "wizard-log", className: state.installing || state.resultPayload ? "visible" : "" }));

    // Result banner
    if (state.resultPayload) {
      var ok = state.resultPayload.success;
      container.appendChild(
        h("div", { className: "result-banner " + (ok ? "success" : "failure") },
          ok ? "Installation successful!" : ("Installation failed: " + (state.resultPayload.error || "Unknown error")))
      );
    }

    return container;
  }

  function doInstall() {
    state.installing = true;
    render();

    var request = {
      harness: state.harness,
      backend: {
        target: state.backendTarget,
        endpoint: state.endpoint || defaultEndpoint(),
        api_key: state.apiKey,
        space_id: state.backendTarget === "arize" ? state.spaceId : null,
      },
      project_name: state.projectName,
      user_id: state.userId || null,
      with_skills: state.withSkills,
      logging: {
        prompts: state.logPrompts,
        tool_details: state.logToolDetails,
        tool_content: state.logToolContent,
      },
      kiro_options: null,
      repo_path: state.harness === "copilot" ? (state.copilotRepoPath || null) : null,
    };
    if (state.harness === "kiro") {
      request.kiro_options = {
        agent_name: state.kiroAgentName || KIRO_DEFAULT_AGENT_NAME,
        set_default: state.kiroSetDefault,
      };
    }

    vscode.postMessage({ type: "install", request: request });
  }

  // ---- Shared form helpers ----

  function formGroup(label, inputType, name, value, onChange, hint) {
    var input = h("input", {
      type: inputType,
      name: name,
      id: "field-" + name,
      value: value || "",
      onInput: function (e) {
        onChange(e.target.value);
        revalidate();
      },
    });
    var children = [
      h("label", { htmlFor: "field-" + name }, label),
      input,
    ];
    if (hint) {
      children.push(h("div", { className: "hint" }, hint));
    }
    return h("div", { className: "form-group" }, children);
  }

  function copilotRepoPathRow() {
    var input = h("input", {
      type: "text",
      name: "copilot_repo_path",
      id: "field-copilot_repo_path",
      value: state.copilotRepoPath || "",
      onInput: function (e) {
        state.copilotRepoPath = e.target.value;
      },
    });
    var browseBtn = h("button", {
      type: "button",
      className: "btn btn-secondary",
      onClick: function () {
        vscode.postMessage({
          type: "pickFolder",
          current: state.copilotRepoPath || state.workspaceFolder,
        });
      },
    }, "Browse...");
    var inputRow = h("div", { className: "input-row" }, [input, browseBtn]);
    return h("div", { className: "form-group" }, [
      h("label", { htmlFor: "field-copilot_repo_path" }, "Workspace folder"),
      inputRow,
      h("div", { className: "hint" }, "Repo where Copilot Chat will read .github/hooks/hooks.json. Defaults to your VS Code workspace."),
    ]);
  }

  function checkboxRow(name, label, checked, onChange) {
    var cb = h("input", {
      type: "checkbox",
      id: "field-" + name,
      name: name,
    });
    if (checked) cb.checked = true;
    cb.addEventListener("change", function (e) {
      onChange(e.target.checked);
    });
    return h("div", { className: "toggle-row" }, [
      cb,
      h("label", { htmlFor: "field-" + name }, label),
    ]);
  }

  function revalidate() {
    var nextBtn = document.querySelector(".wizard-nav .btn-primary");
    if (!nextBtn || nextBtn.textContent !== "Next") return;
    var valid = false;
    if (state.step === 1) valid = step2Valid();
    else valid = true;
    nextBtn.disabled = !valid;
  }

  function renderNav(showBack, canNext) {
    var nav = h("div", { className: "wizard-nav" });
    if (showBack) {
      nav.appendChild(
        h("button", { className: "btn btn-secondary", type: "button", onClick: function () {
          state.step = Math.max(0, state.step - 1);
          render();
        } }, "Back")
      );
    }
    var nextBtn = h("button", {
      className: "btn btn-primary",
      type: "button",
      onClick: function () {
        state.step = Math.min(TOTAL_STEPS - 1, state.step + 1);
        render();
      },
    }, "Next");
    if (!canNext) nextBtn.disabled = true;
    nav.appendChild(nextBtn);
    return nav;
  }

  // ---- Incoming messages ----

  function handleMessage(event) {
    var msg = event.data;
    if (!msg || !msg.type) return;

    switch (msg.type) {
      case "prefill":
        if (typeof msg.workspace_folder === "string" && msg.workspace_folder) {
          state.workspaceFolder = msg.workspace_folder;
        }
        if (msg.harness && HARNESS_KEYS.indexOf(msg.harness) !== -1) {
          state.harness = msg.harness;
          state.prefillHarness = msg.harness;
          if (!state.projectName) state.projectName = msg.harness;
        }
        if (state.harness === "copilot" && !state.copilotRepoPath) {
          state.copilotRepoPath = state.workspaceFolder;
        }
        if (msg.request) {
          var r = msg.request;
          if (r.project_name) state.projectName = r.project_name;
          if (r.user_id) state.userId = r.user_id;
          if (r.with_skills != null) state.withSkills = r.with_skills;
          if (r.backend) {
            if (r.backend.target) state.backendTarget = r.backend.target;
            if (r.backend.endpoint) state.endpoint = r.backend.endpoint;
            if (r.backend.api_key) state.apiKey = r.backend.api_key;
            if (r.backend.space_id) state.spaceId = r.backend.space_id;
          }
          if (r.logging) {
            if (r.logging.prompts != null) state.logPrompts = r.logging.prompts;
            if (r.logging.tool_details != null) state.logToolDetails = r.logging.tool_details;
            if (r.logging.tool_content != null) state.logToolContent = r.logging.tool_content;
          }
          if (r.kiro_options && r.kiro_options.agent_name) {
            state.kiroAgentName = r.kiro_options.agent_name;
          }
          // set_default always renders as unchecked on prefill — do NOT read it from r.kiro_options.set_default
        }
        render();
        break;

      case "folderPicked":
        if (msg.path != null) {
          state.copilotRepoPath = msg.path;
          render();
        }
        break;

      case "log":
        appendLog(msg.level || "info", msg.message || "");
        break;

      case "result":
        state.installing = false;
        state.resultPayload = msg.payload || { success: false, error: "No payload" };
        render();
        // Re-append buffered log lines to the new DOM
        var logEl = document.getElementById("wizard-log");
        if (logEl) {
          logBuffer.forEach(function (entry) {
            var line = document.createElement("div");
            line.className = "log log-" + entry.level;
            line.textContent = entry.message;
            logEl.appendChild(line);
          });
        }
        break;
    }
  }

  var logBuffer = [];

  function appendLog(level, message) {
    logBuffer.push({ level: level, message: message });
    var logEl = document.getElementById("wizard-log");
    if (!logEl) return;
    logEl.classList.add("visible");
    var line = document.createElement("div");
    line.className = "log log-" + level;
    line.textContent = message;
    logEl.appendChild(line);
    logEl.scrollTop = logEl.scrollHeight;
  }

  // ---- Init ----

  function init() {
    render();
    window.addEventListener("message", handleMessage);
    vscode.postMessage({ type: "ready" });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
