"""CLI entry point for the VS Code bridge.

Parses argv, dispatches to the appropriate bridge module, and emits
NDJSON on stdout.  See the module docstring for the full argv contract.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Sequence

from core.vscode_bridge.models import HARNESS_KEYS


def _emit_log(level: str, message: str) -> None:
    """Write a single log event to stdout as NDJSON."""
    print(json.dumps({"event": "log", "level": level, "message": message}), flush=True)


def _emit_result(payload: Dict[str, Any]) -> None:
    """Write the final result event to stdout as NDJSON."""
    print(json.dumps({"event": "result", "payload": payload}), flush=True)


def _drain_logs_and_emit(result: Dict[str, Any]) -> int:
    """Emit log events from result['logs'], then emit the result event.

    Returns the process exit code (0 for success, 1 for failure).
    """
    logs: List[str] = result.get("logs", [])
    for line in logs:
        level = "error" if result.get("success") is False else "info"
        _emit_log(level, line)
    _emit_result(result)
    return 0 if result.get("success") else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arize-vscode-bridge",
        description="Python bridge for the Arize VS Code extension",
    )
    sub = parser.add_subparsers(dest="command")

    # status
    sub.add_parser("status", help="Show current harness configuration status")

    # install
    install_p = sub.add_parser("install", help="Install tracing for a harness")
    install_p.add_argument("--harness", required=True, choices=HARNESS_KEYS, help="Harness to install")
    install_p.add_argument("--target", required=True, choices=("arize", "phoenix"), help="Backend target")
    install_p.add_argument("--endpoint", required=True, help="Backend endpoint URL")
    install_p.add_argument("--api-key", required=True, help="API key (empty string for phoenix no-auth)")
    install_p.add_argument("--space-id", default=None, help="Space ID (required for arize)")
    install_p.add_argument("--project-name", required=True, help="Project name")
    install_p.add_argument("--user-id", default=None, help="User ID")
    install_p.add_argument("--with-skills", action="store_true", default=False, help="Enable skills")
    install_p.add_argument("--log-prompts", choices=("true", "false"), default=None, help="Log prompts")
    install_p.add_argument("--log-tool-details", choices=("true", "false"), default=None, help="Log tool details")
    install_p.add_argument("--log-tool-content", choices=("true", "false"), default=None, help="Log tool content")
    install_p.add_argument(
        "--agent-name",
        default=None,
        help="Kiro: agent name to install hooks into (default arize-traced)",
    )
    install_p.add_argument(
        "--set-default",
        action="store_true",
        default=False,
        help="Kiro: set the chosen agent as Kiro's default after install",
    )
    install_p.add_argument(
        "--repo-path",
        default=None,
        help="Copilot only: absolute path to the repo where hooks should be installed. Default: cwd.",
    )

    # uninstall
    uninstall_p = sub.add_parser("uninstall", help="Uninstall tracing for a harness")
    uninstall_p.add_argument("--harness", required=True, choices=HARNESS_KEYS, help="Harness to uninstall")

    # set-user-id (top-level user_id in config.yaml; empty string clears)
    set_user_p = sub.add_parser("set-user-id", help="Set or clear the top-level user_id in config.yaml")
    set_user_p.add_argument("--user-id", default="", help="New user_id; empty string clears the field")

    # codex buffer commands
    sub.add_parser("codex-buffer-status", help="Show Codex buffer status")
    sub.add_parser("codex-buffer-start", help="Start Codex buffer")
    sub.add_parser("codex-buffer-stop", help="Stop Codex buffer")

    return parser


def _parse_bool_flag(value: str) -> bool:
    return value == "true"


def _build_logging_block(args: argparse.Namespace) -> Any:
    """Build the logging dict from parsed flags, or None if none were set."""
    prompts = args.log_prompts
    details = args.log_tool_details
    content = args.log_tool_content
    if prompts is None and details is None and content is None:
        return None
    return {
        "prompts": _parse_bool_flag(prompts) if prompts is not None else True,
        "tool_details": _parse_bool_flag(details) if details is not None else True,
        "tool_content": _parse_bool_flag(content) if content is not None else True,
    }


def _run_status() -> int:
    from core.vscode_bridge.status import load_status

    result = load_status()
    _emit_result(result)
    return 0 if result.get("success") else 1


def _run_install(args: argparse.Namespace) -> int:
    from core.vscode_bridge.install import install
    from core.vscode_bridge.models import build_backend, build_install_request

    try:
        backend = build_backend(
            target=args.target,
            endpoint=args.endpoint,
            api_key=args.api_key,
            space_id=args.space_id,
        )
    except ValueError as exc:
        from core.vscode_bridge.models import build_operation_result

        result = build_operation_result(
            success=False,
            error="missing_credentials",
            harness=args.harness,
            logs=[str(exc)],
        )
        return _drain_logs_and_emit(result)

    kiro_options = None
    if args.harness == "kiro" and (args.agent_name or args.set_default):
        from core.vscode_bridge.models import build_kiro_options

        kiro_options = build_kiro_options(
            agent_name=args.agent_name or "arize-traced",
            set_default=args.set_default,
        )

    try:
        request = build_install_request(
            harness=args.harness,
            backend=backend,
            project_name=args.project_name,
            user_id=args.user_id,
            with_skills=args.with_skills,
            logging=_build_logging_block(args),
            kiro_options=kiro_options,
            repo_path=args.repo_path,
        )
    except ValueError as exc:
        from core.vscode_bridge.models import build_operation_result

        result = build_operation_result(
            success=False,
            error="invalid_request",
            harness=args.harness,
            logs=[str(exc)],
        )
        return _drain_logs_and_emit(result)

    result = install(request)
    return _drain_logs_and_emit(result)


def _run_uninstall(args: argparse.Namespace) -> int:
    from core.vscode_bridge.install import uninstall

    result = uninstall(args.harness)
    return _drain_logs_and_emit(result)


def _run_set_user_id(args: argparse.Namespace) -> int:
    from core.vscode_bridge.install import set_user_id

    result = set_user_id(args.user_id)
    return _drain_logs_and_emit(result)


def _run_codex_buffer_status() -> int:
    from core.vscode_bridge.codex import buffer_status

    result = buffer_status()
    _emit_result(result)
    return 0 if result.get("success") else 1


def _run_codex_buffer_start() -> int:
    from core.vscode_bridge.codex import buffer_start

    result = buffer_start()
    _emit_result(result)
    return 0 if result.get("success") else 1


def _run_codex_buffer_stop() -> int:
    from core.vscode_bridge.codex import buffer_stop

    result = buffer_stop()
    _emit_result(result)
    return 0 if result.get("success") else 1


_DISPATCH = {
    "status": lambda args: _run_status(),
    "install": _run_install,
    "uninstall": _run_uninstall,
    "set-user-id": _run_set_user_id,
    "codex-buffer-status": lambda args: _run_codex_buffer_status(),
    "codex-buffer-start": lambda args: _run_codex_buffer_start(),
    "codex-buffer-stop": lambda args: _run_codex_buffer_stop(),
}


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point.  Returns the exit code."""
    parser = _build_parser()

    try:
        args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    except SystemExit:
        # argparse calls sys.exit on error; intercept to return exit code 2.
        return 2

    if not args.command:
        parser.print_usage(sys.stderr)
        return 2

    handler = _DISPATCH.get(args.command)
    if handler is None:
        print(f"unknown command: {args.command}", file=sys.stderr)
        return 2

    return handler(args)


def entrypoint() -> None:
    """Console-script entry point (calls sys.exit)."""
    sys.exit(main())
