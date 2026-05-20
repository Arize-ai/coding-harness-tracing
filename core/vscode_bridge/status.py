"""
Bridge status — read-only view of the current config.yaml state.

Returns a StatusPayload dict with one HarnessStatusItem per HARNESS_KEYS
entry, regardless of whether that harness is configured.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from core.config import load_config
from core.constants import CONFIG_FILE
from core.vscode_bridge.models import (
    HARNESS_KEYS,
    build_backend,
    build_harness_status_item,
    build_kiro_options,
    build_status,
)


def _extract_backend(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Build a Backend dict from a harness config entry, or None."""
    target = entry.get("target")
    endpoint = entry.get("endpoint")
    if not target or not endpoint:
        return None
    api_key = entry.get("api_key", "")
    space_id = entry.get("space_id") if target == "arize" else None
    try:
        return build_backend(
            target=target,
            endpoint=endpoint,
            api_key=api_key,
            space_id=space_id,
        )
    except (ValueError, TypeError):
        return None


def _extract_harness_item(name: str, entry: Any) -> Dict[str, Any]:
    """Build a HarnessStatusItem from a config entry (or unconfigured stub)."""
    if not isinstance(entry, dict):
        return build_harness_status_item(name=name)

    kiro_options = None
    if name == "kiro":
        agent_name = entry.get("agent_name")
        if isinstance(agent_name, str) and agent_name:
            kiro_options = build_kiro_options(
                agent_name=agent_name,
                set_default=False,
            )

    repo_paths = None
    if name == "copilot":
        raw_paths = entry.get("repo_paths")
        if isinstance(raw_paths, list) and raw_paths and all(isinstance(p, str) and p for p in raw_paths):
            repo_paths = list(raw_paths)

    return build_harness_status_item(
        name=name,
        configured=True,
        project_name=entry.get("project_name"),
        backend=_extract_backend(entry),
        kiro_options=kiro_options,
        repo_paths=repo_paths,
    )


def load_status() -> Dict[str, Any]:
    """Load the current config and return a StatusPayload dict.

    Never raises.  Missing/empty config → success with all harnesses
    unconfigured.  Malformed YAML → success=False with error string.
    """
    try:
        config = load_config(str(CONFIG_FILE))
    except ValueError:
        return build_status(
            success=False,
            error="config_malformed",
        )

    harnesses_block = config.get("harnesses")
    if not isinstance(harnesses_block, dict):
        harnesses_block = {}

    items = []
    for key in HARNESS_KEYS:
        entry = harnesses_block.get(key)
        if entry is not None:
            items.append(_extract_harness_item(key, entry))
        else:
            items.append(build_harness_status_item(name=key))

    user_id = config.get("user_id")
    if user_id is not None:
        user_id = str(user_id)

    logging_block = config.get("logging")
    if isinstance(logging_block, dict):
        logging_val = logging_block
    else:
        logging_val = None

    return build_status(
        success=True,
        user_id=user_id,
        harnesses=items,
        logging=logging_val,
    )
