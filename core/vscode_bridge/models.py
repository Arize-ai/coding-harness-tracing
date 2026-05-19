"""
Payload models for the VS Code bridge.

Every dict shape returned or accepted by the bridge is defined here as a
builder function.  Builders fill defaults and validate required fields so
callers never need to construct raw dicts by hand.

Shapes
------

Backend = {
    "target": str,                 # "arize" | "phoenix"
    "endpoint": str,
    "api_key": str,                # may be empty for phoenix no-auth
    "space_id": str | None,        # arize only; None on phoenix
}

HarnessStatusItem = {
    "name": str,                   # one of HARNESS_KEYS
    "configured": bool,
    "project_name": str | None,
    "backend": Backend | None,
    "scope": str | None,           # currently always None; reserved
    "kiro_options": {"agent_name": str, "set_default": bool} | None,
    "repo_paths": list[str] | None,  # copilot only; absolute repo paths
}

StatusPayload = {
    "success": bool,
    "error": str | None,
    "user_id": str | None,
    "harnesses": list[HarnessStatusItem],
    "logging": {"prompts": bool, "tool_details": bool, "tool_content": bool} | None,
    "codex_buffer": CodexBufferPayload | None,
}

InstallRequest = {
    "harness": str,
    "backend": Backend,
    "project_name": str,
    "user_id": str | None,
    "with_skills": bool,
    "logging": {"prompts": bool, "tool_details": bool, "tool_content": bool} | None,
    "kiro_options": {"agent_name": str, "set_default": bool} | None,
    "repo_path": str | None,       # copilot only; absolute path to target repo
}

OperationResult = {
    "success": bool,
    "error": str | None,
    "harness": str | None,
    "logs": list[str],
}

CodexBufferPayload = {
    "success": bool,
    "error": str | None,
    "state": str,                  # "running" | "stopped" | "stale" | "unknown"
    "host": str | None,
    "port": int | None,
    "pid": int | None,
}
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# ---- constants ----

HARNESS_KEYS = ("claude-code", "codex", "cursor", "copilot", "gemini", "kiro")

_VALID_TARGETS = ("arize", "phoenix")
_VALID_CODEX_STATES = ("running", "stopped", "stale", "unknown")

# ---- internal helpers ----


def _require_str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _validate_harness(harness: str) -> str:
    _require_str(harness, "harness")
    if harness not in HARNESS_KEYS:
        raise ValueError(f"unknown harness {harness!r}; expected one of {HARNESS_KEYS}")
    return harness


def _validate_target(target: str) -> str:
    _require_str(target, "target")
    if target not in _VALID_TARGETS:
        raise ValueError(f"unknown target {target!r}; expected one of {_VALID_TARGETS}")
    return target


def _normalize_logging(
    logging: Optional[Dict[str, bool]],
) -> Optional[Dict[str, bool]]:
    """Return a validated logging dict or None."""
    if logging is None:
        return None
    return {
        "prompts": bool(logging.get("prompts", True)),
        "tool_details": bool(logging.get("tool_details", True)),
        "tool_content": bool(logging.get("tool_content", True)),
    }


# ---- builders ----


def build_backend(
    target: str,
    endpoint: str,
    api_key: str = "",
    space_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a Backend dict.

    For target ``"arize"``, *space_id* is required.
    For target ``"phoenix"``, *space_id* must be ``None``.
    """
    _validate_target(target)
    _require_str(endpoint, "endpoint")

    if target == "arize" and not space_id:
        raise ValueError("space_id is required when target is 'arize'")
    if target == "phoenix" and space_id is not None:
        raise ValueError("space_id must be None when target is 'phoenix'")

    return {
        "target": target,
        "endpoint": endpoint,
        "api_key": api_key if api_key is not None else "",
        "space_id": space_id,
    }


def build_kiro_options(
    agent_name: str,
    set_default: bool = False,
) -> Dict[str, Any]:
    """Build a KiroOptions dict.

    Shape:
      { "agent_name": str, "set_default": bool }

    agent_name must be a non-empty string. Allowed character set is enforced
    by tracing.kiro.install at write-time, NOT here — this builder only checks
    non-emptiness so we don't double-validate.
    """
    _require_str(agent_name, "agent_name")
    return {
        "agent_name": agent_name,
        "set_default": bool(set_default),
    }


def build_harness_status_item(
    name: str,
    configured: bool = False,
    project_name: Optional[str] = None,
    backend: Optional[Dict[str, Any]] = None,
    scope: Optional[str] = None,
    kiro_options: Optional[Dict[str, Any]] = None,
    repo_paths: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Build a HarnessStatusItem dict."""
    _validate_harness(name)
    if kiro_options is not None and name != "kiro":
        raise ValueError("kiro_options only valid when harness is 'kiro'")
    if kiro_options is not None:
        kiro_options = build_kiro_options(
            kiro_options.get("agent_name", ""),
            kiro_options.get("set_default", False),
        )
    if repo_paths is not None and name != "copilot":
        raise ValueError("repo_paths only valid when harness is 'copilot'")
    if repo_paths is not None:
        if not isinstance(repo_paths, list):
            raise ValueError("repo_paths must be a list of non-empty strings")
        for item in repo_paths:
            if not isinstance(item, str) or not item:
                raise ValueError("repo_paths must be a list of non-empty strings")
    return {
        "name": name,
        "configured": bool(configured),
        "project_name": project_name,
        "backend": backend,
        "scope": scope,
        "kiro_options": kiro_options,
        "repo_paths": repo_paths,
    }


def build_status(
    success: bool,
    error: Optional[str] = None,
    user_id: Optional[str] = None,
    harnesses: Optional[List[Dict[str, Any]]] = None,
    logging: Optional[Dict[str, bool]] = None,
    codex_buffer: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a StatusPayload dict.

    *harnesses* defaults to all harnesses in :data:`HARNESS_KEYS` unconfigured
    when omitted.
    """
    if harnesses is None:
        harnesses = [build_harness_status_item(name=k) for k in HARNESS_KEYS]
    return {
        "success": bool(success),
        "error": error,
        "user_id": user_id,
        "harnesses": harnesses,
        "logging": _normalize_logging(logging),
        "codex_buffer": codex_buffer,
    }


def build_install_request(
    harness: str,
    backend: Dict[str, Any],
    project_name: str,
    user_id: Optional[str] = None,
    with_skills: bool = False,
    logging: Optional[Dict[str, bool]] = None,
    kiro_options: Optional[Dict[str, Any]] = None,
    repo_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Build an InstallRequest dict."""
    _validate_harness(harness)
    _require_str(project_name, "project_name")
    if kiro_options is not None and harness != "kiro":
        raise ValueError("kiro_options only valid when harness is 'kiro'")
    if kiro_options is not None:
        kiro_options = build_kiro_options(
            kiro_options.get("agent_name", ""),
            kiro_options.get("set_default", False),
        )
    if repo_path is not None and harness != "copilot":
        raise ValueError("repo_path only valid when harness is 'copilot'")
    if repo_path is not None:
        _require_str(repo_path, "repo_path")
    return {
        "harness": harness,
        "backend": backend,
        "project_name": project_name,
        "user_id": user_id,
        "with_skills": bool(with_skills),
        "logging": _normalize_logging(logging),
        "kiro_options": kiro_options,
        "repo_path": repo_path,
    }


def build_operation_result(
    success: bool,
    error: Optional[str] = None,
    harness: Optional[str] = None,
    logs: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Build an OperationResult dict."""
    if harness is not None:
        _validate_harness(harness)
    return {
        "success": bool(success),
        "error": error,
        "harness": harness,
        "logs": logs if logs is not None else [],
    }


def build_codex_buffer(
    success: bool,
    error: Optional[str] = None,
    state: str = "unknown",
    host: Optional[str] = None,
    port: Optional[int] = None,
    pid: Optional[int] = None,
) -> Dict[str, Any]:
    """Build a CodexBufferPayload dict."""
    if state not in _VALID_CODEX_STATES:
        raise ValueError(f"unknown codex buffer state {state!r}; " f"expected one of {_VALID_CODEX_STATES}")
    return {
        "success": bool(success),
        "error": error,
        "state": state,
        "host": host,
        "port": port if port is None else int(port),
        "pid": pid if pid is None else int(pid),
    }
