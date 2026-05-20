"""Codex-specific runtime helpers for buffer status, start, and stop.

This module owns live buffer state.  ``bridge-status`` does not touch it.
All functions return a ``CodexBufferPayload`` dict (see ``models.py``) and
never raise — every exception is caught and surfaced via the ``error`` field.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from core.vscode_bridge.models import build_codex_buffer


def _resolve_host_port_safe() -> tuple:
    """Return (host, port) from codex_buffer_ctl, or (None, None) on failure.

    Uses ``tracing.codex.codex_buffer_ctl._resolve_host_port`` — the only
    available accessor for the configured buffer address.  There is no public
    equivalent; the underscore name is an implementation detail of
    ``codex_buffer_ctl`` that we accept as a dependency.
    """
    try:
        from tracing.codex.codex_buffer_ctl import _resolve_host_port

        host, port = _resolve_host_port()
        return (str(host), int(port))
    except Exception:
        return (None, None)


def _detect_state(host: Optional[str], port: Optional[int]) -> tuple:
    """Determine buffer state with stale detection.

    Returns (state, pid) where state is one of
    ``"running"`` | ``"stopped"`` | ``"stale"`` | ``"unknown"``.
    """
    if host is None or port is None:
        return ("unknown", None)

    try:
        from tracing.codex.codex_buffer_ctl import _expected_build_path, _health_check, _health_identity, _listener_pid
    except Exception:
        return ("unknown", None)

    try:
        healthy = _health_check(host, port, timeout=2.0)
    except Exception:
        return ("unknown", None)

    if not healthy:
        # No response on the port — buffer is stopped.
        return ("stopped", None)

    # Port is healthy — check identity to distinguish running vs stale.
    try:
        identity = _health_identity(host, port, timeout=2.0)
        pid = identity.get("pid")
        remote_bp = identity.get("build_path")
    except Exception:
        identity = {}
        pid = None
        remote_bp = None

    if not remote_bp:
        # Healthy but no build_path in identity — treat as stale (old buffer).
        if pid is None:
            try:
                pid = _listener_pid(host, port)
            except Exception:
                pass
        return ("stale", pid)

    try:
        expected = _expected_build_path()
        if os.path.realpath(remote_bp) == os.path.realpath(expected):
            # Identity matches — genuinely running.
            if pid is None:
                try:
                    pid = _listener_pid(host, port)
                except Exception:
                    pass
            return ("running", pid)
        else:
            # Identity mismatch — stale.
            if pid is None:
                try:
                    pid = _listener_pid(host, port)
                except Exception:
                    pass
            return ("stale", pid)
    except Exception:
        return ("stale", pid)


# ---- public API ----


def buffer_status() -> Dict[str, Any]:
    """Return a ``CodexBufferPayload`` describing the current buffer state.

    ``success`` is ``True`` whenever a definitive answer was reached — even if
    the state is ``"stopped"``.
    """
    try:
        host, port = _resolve_host_port_safe()
        state, pid = _detect_state(host, port)
        if state == "unknown":
            return build_codex_buffer(
                success=False,
                error="buffer_unreachable",
                state="unknown",
                host=host,
                port=port,
                pid=pid,
            )
        return build_codex_buffer(
            success=True,
            state=state,
            host=host,
            port=port,
            pid=pid,
        )
    except Exception:
        return build_codex_buffer(
            success=False,
            error="buffer_unreachable",
            state="unknown",
        )


def buffer_start() -> Dict[str, Any]:
    """Attempt to start the buffer and return a ``CodexBufferPayload``.

    On success the payload reflects the post-start state.  On failure
    ``success`` is ``False`` with ``error="buffer_start_failed"``.
    """
    try:
        host, port = _resolve_host_port_safe()
        try:
            from tracing.codex.codex_buffer_ctl import buffer_start as _ctl_start

            ok = _ctl_start()
        except Exception:
            return build_codex_buffer(
                success=False,
                error="buffer_start_failed",
                state="unknown",
                host=host,
                port=port,
            )

        if not ok:
            # Start returned False — re-probe for best-effort state info.
            state, pid = _detect_state(host, port)
            return build_codex_buffer(
                success=False,
                error="buffer_start_failed",
                state=state,
                host=host,
                port=port,
                pid=pid,
            )

        # Started successfully — probe state.
        state, pid = _detect_state(host, port)
        return build_codex_buffer(
            success=True,
            state=state if state != "unknown" else "running",
            host=host,
            port=port,
            pid=pid,
        )
    except Exception:
        return build_codex_buffer(
            success=False,
            error="buffer_start_failed",
            state="unknown",
        )


def buffer_stop() -> Dict[str, Any]:
    """Attempt to stop the buffer and return a ``CodexBufferPayload``.

    On success the payload has ``state="stopped"``.  On failure ``success``
    is ``False`` with ``error="buffer_stop_failed"``.
    """
    try:
        host, port = _resolve_host_port_safe()
        try:
            from tracing.codex.codex_buffer_ctl import buffer_stop as _ctl_stop

            result = _ctl_stop()
        except Exception:
            return build_codex_buffer(
                success=False,
                error="buffer_stop_failed",
                state="unknown",
                host=host,
                port=port,
            )

        if result == "stopped":
            return build_codex_buffer(
                success=True,
                state="stopped",
                host=host,
                port=port,
            )

        # "refused" or unexpected value — re-probe state.
        state, pid = _detect_state(host, port)
        return build_codex_buffer(
            success=False,
            error="buffer_stop_failed",
            state=state,
            host=host,
            port=port,
            pid=pid,
        )
    except Exception:
        return build_codex_buffer(
            success=False,
            error="buffer_stop_failed",
            state="unknown",
        )
