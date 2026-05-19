"""Dispatch non-interactive install/uninstall for a single harness."""

from __future__ import annotations

import contextlib
import io
import traceback
from typing import Any, Dict, List, Optional

from core.vscode_bridge.models import _VALID_TARGETS, HARNESS_KEYS, build_operation_result

# Maps harness key to its install module path.
_HARNESS_MODULES = {
    "claude-code": "tracing.claude_code.install",
    "codex": "tracing.codex.install",
    "cursor": "tracing.cursor.install",
    "copilot": "tracing.copilot.install",
    "gemini": "tracing.gemini.install",
    "kiro": "tracing.kiro.install",
}


def _capture_output(fn: Any, *args: Any, **kwargs: Any) -> List[str]:
    """Call *fn* and return captured stdout+stderr lines."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        fn(*args, **kwargs)
    return [line for line in buf.getvalue().splitlines() if line]


def _import_installer(harness: str) -> Any:
    """Lazily import the installer module for *harness*."""
    import importlib

    return importlib.import_module(_HARNESS_MODULES[harness])


def _validate_backend(backend: Dict[str, Any]) -> Optional[str]:
    """Return an error string if *backend* is invalid, else None."""
    target = backend.get("target", "")
    if target not in _VALID_TARGETS:
        return "missing_credentials"
    if target == "arize" and not backend.get("space_id"):
        return "missing_credentials"
    return None


def install(request: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatch a non-interactive install for one harness.

    Parameters
    ----------
    request:
        An ``InstallRequest`` dict (see ``core.vscode_bridge.models``).

    Returns
    -------
    dict
        An ``OperationResult`` dict.
    """
    harness = request.get("harness", "")
    if harness not in HARNESS_KEYS:
        return build_operation_result(success=False, error="unknown_harness", harness=None, logs=[])

    backend = request.get("backend", {})
    backend_err = _validate_backend(backend)
    if backend_err:
        return build_operation_result(success=False, error=backend_err, harness=harness, logs=[])

    # Build credentials dict: backend minus 'target'.
    credentials = {k: v for k, v in backend.items() if k != "target"}

    # Build logging block if provided.
    logging_block = request.get("logging")

    extra_kwargs: Dict[str, Any] = {}
    if harness == "kiro" and request.get("kiro_options"):
        extra_kwargs["agent_name"] = request["kiro_options"]["agent_name"]
        extra_kwargs["set_default"] = request["kiro_options"]["set_default"]
    if harness == "copilot" and request.get("repo_path"):
        extra_kwargs["repo_path"] = request["repo_path"]

    try:
        mod = _import_installer(harness)
        logs = _capture_output(
            mod.install_noninteractive,
            target=backend["target"],
            credentials=credentials,
            project_name=request["project_name"],
            user_id=request.get("user_id") or "",
            with_skills=request.get("with_skills", False),
            logging_block=logging_block,
            **extra_kwargs,
        )
    except Exception:
        tb = traceback.format_exc()
        return build_operation_result(
            success=False,
            error="install_failed",
            harness=harness,
            logs=[tb],
        )

    return build_operation_result(success=True, harness=harness, logs=logs)


def set_user_id(user_id: str) -> Dict[str, Any]:
    """Set or clear the top-level ``user_id`` in ``config.yaml``.

    Passing an empty string deletes the key. Returns an ``OperationResult``;
    ``harness`` is ``None`` because this is a top-level config change.
    """
    try:
        from core.config import delete_value, load_config, save_config, set_value

        config = load_config()
        if user_id:
            set_value(config, "user_id", user_id)
        else:
            delete_value(config, "user_id")
        save_config(config)
    except Exception:
        tb = traceback.format_exc()
        return build_operation_result(
            success=False,
            error="set_user_id_failed",
            harness=None,
            logs=[tb],
        )

    return build_operation_result(success=True, harness=None, logs=[])


def uninstall(harness: str) -> Dict[str, Any]:
    """Dispatch a non-interactive uninstall for one harness.

    Parameters
    ----------
    harness:
        One of ``HARNESS_KEYS``.

    Returns
    -------
    dict
        An ``OperationResult`` dict.
    """
    if harness not in HARNESS_KEYS:
        return build_operation_result(success=False, error="unknown_harness", harness=None, logs=[])

    try:
        mod = _import_installer(harness)
        logs = _capture_output(mod.uninstall_noninteractive)
    except Exception:
        tb = traceback.format_exc()
        return build_operation_result(
            success=False,
            error="install_failed",
            harness=harness,
            logs=[tb],
        )

    return build_operation_result(success=True, harness=harness, logs=logs)
