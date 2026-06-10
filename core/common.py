#!/usr/bin/env python3
"""Shared library for coding-harness-tracing: state management, file locking, and span building.

Provides FileLock (cross-platform file locking), StateManager (per-session
key-value state backed by YAML files), and OTLP span building functions.
Replaces the jq-based state functions in common.sh lines 46-109 and
build_span/build_multi_span from common.sh lines 277-317 / codex common.sh lines 110-145.
"""

import atexit
import functools
import json as _json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Optional

import yaml

# ---------------------------------------------------------------------------
# Environment helper — reads tracing-related env vars with defaults
# ---------------------------------------------------------------------------


class _Env:
    """Lazy accessor for tracing-related environment variables.

    Property reads are live (not cached) so tests can monkeypatch os.environ.
    """

    @property
    def trace_enabled(self) -> bool:
        return os.environ.get("ARIZE_TRACE_ENABLED", "true").lower() == "true"

    @property
    def project_name(self) -> str:
        return os.environ.get("ARIZE_PROJECT_NAME", "")

    @property
    def user_id(self) -> str:
        # env var > config.yaml top-level `user_id` > ""
        raw = os.environ.get("ARIZE_USER_ID")
        if raw is not None:
            return raw
        val = self._top_level_config.get("user_id")
        return str(val) if val else ""

    @property
    def verbose(self) -> bool:
        return os.environ.get("ARIZE_VERBOSE", "").lower() == "true"

    @property
    def dry_run(self) -> bool:
        return os.environ.get("ARIZE_DRY_RUN", "false").lower() == "true"

    @property
    def log_file(self) -> str:
        # Each harness adapter sets ARIZE_LOG_FILE to its per-harness path under
        # ~/.arize/harness/logs/ at import time; the env var also acts as the
        # explicit user override. Fall back to the shared kit log only when no
        # adapter has run (e.g. ad-hoc imports of core.common).
        override = os.environ.get("ARIZE_LOG_FILE")
        if override:
            return override
        from core.constants import LOG_DIR

        return str(LOG_DIR / "agent-kit.log")

    @property
    def phoenix_endpoint(self) -> str:
        return os.environ.get("PHOENIX_ENDPOINT", "")

    @property
    def api_key(self) -> str:
        return os.environ.get("ARIZE_API_KEY", "")

    @property
    def space_id(self) -> str:
        return os.environ.get("ARIZE_SPACE_ID", "")

    @functools.cached_property
    def _top_level_config(self) -> dict:
        """Full top-level config.yaml mapping. Loaded once per process."""
        try:
            from core.config import load_config

            data = load_config()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @functools.cached_property
    def _logging_config(self) -> dict:
        """Top-level `logging:` block from config.yaml. Loaded once per process."""
        try:
            from core.config import load_config

            block = load_config().get("logging")
            return block if isinstance(block, dict) else {}
        except Exception:
            return {}

    def _resolve_log_flag(self, env_key: str, config_key: str, default: bool) -> bool:
        """env var > config.yaml `logging.<key>` > default."""
        raw = os.environ.get(env_key)
        if raw is not None:
            return raw.lower() == "true"
        val = self._logging_config.get(config_key)
        if isinstance(val, bool):
            return val
        return default

    @property
    def log_prompts(self) -> bool:
        return self._resolve_log_flag("ARIZE_LOG_PROMPTS", "prompts", True)

    @property
    def log_tool_details(self) -> bool:
        return self._resolve_log_flag("ARIZE_LOG_TOOL_DETAILS", "tool_details", True)

    @property
    def log_tool_content(self) -> bool:
        return self._resolve_log_flag("ARIZE_LOG_TOOL_CONTENT", "tool_content", True)


env = _Env()


def redact_content(allowed: bool, content: str) -> str:
    """Return content if logging is enabled, else a length-only placeholder.

    Used to keep size/debugging signal while stripping sensitive payloads
    (prompts, tool arguments, tool output) from exported spans.
    """
    text = content or ""
    if allowed:
        return text
    return f"<redacted ({len(text)} chars)>"


# ---------------------------------------------------------------------------
# ID and timestamp generation
# ---------------------------------------------------------------------------


def generate_trace_id() -> str:
    """Generate a 32-hex-char trace ID (replaces uuidgen | tr -d '-')."""
    return os.urandom(16).hex()


def generate_span_id() -> str:
    """Generate a 16-hex-char span ID (replaces uuidgen | tr -d '-' | cut -c1-16)."""
    return os.urandom(8).hex()


def get_timestamp_ms() -> int:
    """Current time in milliseconds since epoch (replaces date +%s%3N)."""
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _is_verbose() -> bool:
    return os.environ.get("ARIZE_VERBOSE", "").lower() == "true"


def log(msg: str) -> None:
    """Verbose log — only written when ARIZE_VERBOSE=true. Goes to stderr."""
    if _is_verbose():
        print(f"[arize] {msg}", file=sys.stderr, flush=True)


def error(msg: str) -> None:
    """Error log — always written. Goes to stderr."""
    print(f"[arize:error] {msg}", file=sys.stderr, flush=True)


# Module-level handles for the active stderr redirect. Kept so tests (and any
# future caller) can restore the original stderr via restore_stderr_from_log_file().
_original_stderr: Optional[IO] = None
_redirected_log_fh: Optional[IO] = None


def redirect_stderr_to_log_file() -> None:
    """Tee ``sys.stderr`` to ``ARIZE_LOG_FILE`` (append, line-buffered).

    Each hook invocation is a separate process. Adapters should call this
    once at module import time so every entry point in the harness benefits
    without needing to wrap every function. With the redirect in place:

      - ``error(...)`` always writes → always lands in the log file.
      - ``log(...)`` writes only when ``ARIZE_VERBOSE=true`` → verbose
        noise lands in the file only when explicitly opted in.

    Idempotent: if a redirect is already active, this is a no-op. The original
    ``sys.stderr`` and the open file handle are stashed at module scope so
    ``restore_stderr_from_log_file()`` can reverse the redirect — useful in
    tests where adapter imports would otherwise leak stderr state across
    cases.

    Fail-soft: if the path can't be opened, stderr is left untouched and
    output falls back to whatever the host CLI does with hook stderr.
    No-op when ``ARIZE_LOG_FILE`` is unset.
    """
    global _original_stderr, _redirected_log_fh

    if _redirected_log_fh is not None:
        return  # already redirected

    log_file = os.environ.get("ARIZE_LOG_FILE")
    if not log_file:
        return
    try:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a", buffering=1, encoding="utf-8")
    except OSError:
        return

    _original_stderr = sys.stderr
    _redirected_log_fh = fh
    sys.stderr = fh
    # Restore on interpreter shutdown so atexit hooks (and any late prints)
    # don't write to an FD that's about to close.
    atexit.register(restore_stderr_from_log_file)


def restore_stderr_from_log_file() -> None:
    """Reverse ``redirect_stderr_to_log_file()``. Idempotent.

    Restores the saved ``sys.stderr`` and closes the log file handle. Safe to
    call multiple times. No-op if no redirect is active.
    """
    global _original_stderr, _redirected_log_fh

    if _redirected_log_fh is None:
        return

    if _original_stderr is not None:
        sys.stderr = _original_stderr

    try:
        _redirected_log_fh.close()
    except OSError:
        pass

    _redirected_log_fh = None
    _original_stderr = None


def debug_dump(label: str, data: object) -> None:
    """Trace-level debug dump — only when ARIZE_TRACE_DEBUG=true.

    Writes YAML files to {STATE_DIR}/debug/{label}_{timestamp}.yaml.
    Used by Codex hooks for detailed payload inspection.
    """
    if os.environ.get("ARIZE_TRACE_DEBUG", "").lower() != "true":
        return
    try:
        from core.constants import STATE_BASE_DIR

        debug_dir = STATE_BASE_DIR / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time() * 1000)
        dump_file = debug_dir / f"{label}_{ts}.yaml"
        dump_file.write_text(yaml.safe_dump(data, default_flow_style=False), encoding="utf-8")
    except Exception:
        pass  # debug dumps must never cause failures


# ---------------------------------------------------------------------------
# Target detection and span sending
# ---------------------------------------------------------------------------


def get_target() -> str:
    """Detect backend target from env vars.

    Returns "phoenix", "arize", or "none".
    """
    if env.phoenix_endpoint:
        return "phoenix"
    if env.api_key and env.space_id:
        return "arize"
    return "none"


def resolve_backend(span_dict: dict) -> dict:
    """Resolve backend config for a span payload.

    Precedence per field: env var > harnesses.<service_name> in
    ~/.arize/harness/config.yaml > defaults. Used so marketplace-installed
    plugins (which skip the interactive wizard) can supply credentials
    purely via the runtime env block in ~/.claude/settings.json.

    Env vars consulted:
      - PHOENIX_ENDPOINT     → target=phoenix (overrides YAML target)
      - ARIZE_API_KEY        → target=arize when paired with ARIZE_SPACE_ID
      - ARIZE_SPACE_ID       → required for arize when env-only
      - ARIZE_PROJECT_NAME   → project_name override

    service_name is pulled from the span's resource attributes
    (resource.attributes[service.name]).

    Returns:
      {"target": "phoenix", "endpoint", "api_key", "project_name"} or
      {"target": "arize",   "endpoint", "api_key", "space_id", "project_name"}

    On any missing required field, logs a clear error via error() and
    returns {"target": "none", "project_name": project_name_or_""}.
    """
    _none = {"target": "none", "project_name": ""}

    # Extract service.name from span resource attributes
    service_name = ""
    for rs in span_dict.get("resourceSpans", []):
        for attr in rs.get("resource", {}).get("attributes", []):
            if attr.get("key") == "service.name":
                service_name = attr.get("value", {}).get("stringValue", "")
                break
        if service_name:
            break

    if not service_name:
        error("No service.name attribute found on span — cannot resolve harness config.")
        return _none

    # Load YAML config (may be empty or missing the harness entry)
    try:
        from core.config import load_config

        cfg = load_config()
    except Exception:
        cfg = {}

    harness_cfg = cfg.get("harnesses", {}).get(service_name) or {}

    # Resolve project_name: env > YAML > service_name
    project_name = env.project_name or harness_cfg.get("project_name", "") or service_name

    # Resolve target: env-derived backend takes precedence over YAML target
    if env.phoenix_endpoint:
        target = "phoenix"
    elif env.api_key and env.space_id:
        target = "arize"
    else:
        target = harness_cfg.get("target", "")

    if not target:
        error(
            f"No backend configured for harness '{service_name}': set "
            f"ARIZE_API_KEY+ARIZE_SPACE_ID (or PHOENIX_ENDPOINT) in env, "
            f"or add a 'harnesses.{service_name}' entry to ~/.arize/harness/config.yaml."
        )
        return {"target": "none", "project_name": project_name}

    if target == "phoenix":
        endpoint = env.phoenix_endpoint or harness_cfg.get("endpoint", "")
        if not endpoint:
            error(
                f"Incomplete phoenix config for harness '{service_name}': missing endpoint "
                f"(set PHOENIX_ENDPOINT in env or add 'endpoint' to config.yaml)."
            )
            return {"target": "none", "project_name": project_name}
        return {
            "target": "phoenix",
            "endpoint": endpoint,
            "api_key": env.api_key or harness_cfg.get("api_key", ""),
            "project_name": project_name,
        }

    if target == "arize":
        endpoint = harness_cfg.get("endpoint", "") or "otlp.arize.com:443"
        api_key = env.api_key or harness_cfg.get("api_key", "")
        space_id = env.space_id or harness_cfg.get("space_id", "")

        missing = []
        if not api_key:
            missing.append("api_key (set ARIZE_API_KEY)")
        if not space_id:
            missing.append("space_id (set ARIZE_SPACE_ID)")
        if missing:
            error(
                f"Incomplete arize config for harness '{service_name}': missing "
                f"{', '.join(missing)} or add to config.yaml."
            )
            return {"target": "none", "project_name": project_name}
        return {
            "target": "arize",
            "endpoint": endpoint,
            "api_key": api_key,
            "space_id": space_id,
            "project_name": project_name,
        }

    error(f"Unknown target '{target}' for harness '{service_name}'. " f"Expected 'arize' or 'phoenix'.")
    return {"target": "none", "project_name": project_name}


def _inject_arize_project_name(span_dict: dict, project_name: str) -> dict:
    """Return a copy of span_dict with arize.project.name on every span's attributes.

    Arize requires this attribute on each span (not just the resource).
    Modifies a shallow copy — does not mutate the original.
    """
    import copy

    payload = copy.deepcopy(span_dict)
    project_attr = {"key": "arize.project.name", "value": {"stringValue": project_name}}
    for rs in payload.get("resourceSpans", []):
        # Also add to resource attributes
        rs.setdefault("resource", {}).setdefault("attributes", []).append(project_attr)
        for ss in rs.get("scopeSpans", []):
            for span in ss.get("spans", []):
                span.setdefault("attributes", []).append(project_attr)
    return payload


def _otlp_attr_value_to_python(value: dict):
    """Convert an OTLP AnyValue JSON object to a plain JSON value."""
    if not isinstance(value, dict):
        return value
    if "stringValue" in value:
        return value["stringValue"]
    if "boolValue" in value:
        return value["boolValue"]
    if "intValue" in value:
        try:
            return int(value["intValue"])
        except (TypeError, ValueError):
            return value["intValue"]
    if "doubleValue" in value:
        return value["doubleValue"]
    if "bytesValue" in value:
        return value["bytesValue"]
    if "arrayValue" in value:
        values = value.get("arrayValue", {}).get("values", [])
        return [_otlp_attr_value_to_python(item) for item in values]
    if "kvlistValue" in value:
        values = value.get("kvlistValue", {}).get("values", [])
        return {item.get("key", ""): _otlp_attr_value_to_python(item.get("value", {})) for item in values}
    return value


def _otlp_attrs_to_dict(attrs: list) -> dict:
    """Convert OTLP attribute arrays to the object shape Phoenix REST expects."""
    result = {}
    for attr in attrs or []:
        key = attr.get("key")
        if not key:
            continue
        result[key] = _otlp_attr_value_to_python(attr.get("value", {}))
    return result


def _unix_nano_to_iso(value) -> str:
    """Convert OTLP Unix nanoseconds to an ISO-8601 UTC timestamp."""
    try:
        ns = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid Unix nanosecond timestamp: {value!r}")
    seconds, nanos = divmod(ns, 1_000_000_000)
    dt = datetime.fromtimestamp(seconds, tz=timezone.utc).replace(microsecond=nanos // 1000)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _phoenix_status_code(status: dict) -> str:
    """Convert OTLP status code enum values to Phoenix status strings."""
    if not isinstance(status, dict):
        return "UNSET"
    raw_code = status.get("code", 0)
    if raw_code is None:
        raw_code = 0
    try:
        code = int(raw_code)
    except (TypeError, ValueError):
        code = 0
    return {1: "OK", 2: "ERROR"}.get(code, "UNSET")


def _phoenix_span_kind(span: dict, attrs: dict) -> str:
    """Prefer OpenInference span kind attributes, falling back to OTLP kind."""
    oi_kind = attrs.get("openinference.span.kind")
    if oi_kind:
        return str(oi_kind).upper()
    try:
        otlp_kind = int(span.get("kind", 0))
    except (TypeError, ValueError):
        otlp_kind = 0
    return {2: "SERVER", 3: "CLIENT", 4: "PRODUCER", 5: "CONSUMER"}.get(otlp_kind, "UNKNOWN")


def _otlp_to_phoenix_payload(span_dict: dict) -> dict:
    """Translate OTLP JSON into Phoenix's native REST create-spans schema."""
    phoenix_spans = []
    for rs in span_dict.get("resourceSpans", []):
        resource_attrs = _otlp_attrs_to_dict(rs.get("resource", {}).get("attributes", []))
        for ss in rs.get("scopeSpans", []):
            for span in ss.get("spans", []):
                attrs = {**resource_attrs, **_otlp_attrs_to_dict(span.get("attributes", []))}
                item = {
                    "name": span.get("name", "unknown"),
                    "context": {
                        "trace_id": span.get("traceId", ""),
                        "span_id": span.get("spanId", ""),
                    },
                    "span_kind": _phoenix_span_kind(span, attrs),
                    "start_time": _unix_nano_to_iso(span.get("startTimeUnixNano")),
                    "end_time": _unix_nano_to_iso(span.get("endTimeUnixNano")),
                    "status_code": _phoenix_status_code(span.get("status", {})),
                    "status_message": (span.get("status", {}) or {}).get("message", ""),
                    "attributes": attrs,
                }
                parent_id = span.get("parentSpanId")
                if parent_id:
                    item["parent_id"] = parent_id

                events = []
                for event in span.get("events", []) or []:
                    events.append(
                        {
                            "name": event.get("name", "event"),
                            "timestamp": _unix_nano_to_iso(event.get("timeUnixNano")),
                            "attributes": _otlp_attrs_to_dict(event.get("attributes", [])),
                        }
                    )
                if events:
                    item["events"] = events
                phoenix_spans.append(item)
    return {"data": phoenix_spans}


def _extract_span_name(span_dict: dict) -> str:
    """Extract the first span name from an OTLP payload."""
    try:
        return span_dict["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"]
    except (KeyError, IndexError, TypeError):
        return "unknown"


def send_span(span_dict: dict) -> bool:
    """Send a span payload directly to the configured backend.

    Backend is resolved per harness via ``resolve_backend()``, which checks
    env vars (ARIZE_API_KEY+ARIZE_SPACE_ID, PHOENIX_ENDPOINT,
    ARIZE_PROJECT_NAME) before falling back to
    ``~/.arize/harness/config.yaml``. If neither path yields a complete
    backend, the span is dropped and an error is logged.

    Never raises. Returns True on success, False on failure.
    """
    try:
        if env.dry_run:
            log(f"[dry-run] would send span: {_extract_span_name(span_dict)}")
            return True

        if env.verbose:
            log(f"span payload: {_json.dumps(span_dict)}")

        backend = resolve_backend(span_dict)
        target = backend["target"]

        if target == "phoenix":
            project = backend["project_name"]
            endpoint = backend["endpoint"]
            api_key = backend.get("api_key", "")
            project_identifier = urllib.parse.quote(project, safe="")
            url = f"{endpoint.rstrip('/')}/v1/projects/{project_identifier}/spans"
            body = _json.dumps(_otlp_to_phoenix_payload(span_dict)).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return 200 <= resp.status < 300
            except urllib.error.HTTPError as e:
                try:
                    detail = e.read().decode("utf-8", errors="replace")
                except Exception:
                    detail = ""
                error(f"Phoenix send failed: HTTP {e.code}: {detail or e.reason}")
                return False
            except Exception as e:
                error(f"Phoenix send failed: {e}")
                return False
        elif target == "arize":
            project = backend["project_name"]
            endpoint = backend.get("endpoint", "otlp.arize.com:443")
            api_key = backend["api_key"]
            space_id = backend.get("space_id", "")

            # Inject arize.project.name into span attributes (required by Arize)
            payload = _inject_arize_project_name(span_dict, project)

            # Normalize endpoint to HTTPS URL for HTTP/JSON transport
            if endpoint.startswith("http://") or endpoint.startswith("https://"):
                url = f"{endpoint.rstrip('/')}/v1/traces"
            else:
                url = f"https://{endpoint}/v1/traces"

            body = _json.dumps(payload).encode("utf-8")
            headers = {
                "Content-Type": "application/json",
                "authorization": f"Bearer {api_key}",
                "space_id": space_id,
            }
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return 200 <= resp.status < 300
            except urllib.error.HTTPError as e:
                try:
                    detail = e.read().decode("utf-8", errors="replace")
                except Exception:
                    detail = ""
                error(f"Arize send failed: HTTP {e.code}: {detail or e.reason}")
                return False
            except Exception as e:
                error(f"Arize send failed: {e}")
                return False
        else:
            error("No backend configured (set PHOENIX_ENDPOINT or ARIZE_API_KEY+ARIZE_SPACE_ID)")
            return False
    except Exception as e:
        error(f"send_span failed: {e}")
        return False


# --- Platform-specific lock implementation detection ---
try:
    import fcntl

    _LOCK_IMPL = "fcntl"
except ImportError:
    try:
        import msvcrt

        _LOCK_IMPL = "msvcrt"
    except ImportError:
        _LOCK_IMPL = "mkdir"


class FileLock:
    """Cross-platform file lock.

    Uses fcntl.flock on Unix, msvcrt.locking on Windows.
    Falls back to mkdir-based locking if neither is available.

    Usage:
        with FileLock(Path("/path/to/.lock"), timeout=3.0):
            # exclusive access

    The lock_path can be a file or directory path:
    - fcntl/msvcrt mode: creates/opens lock_path as a file
    - mkdir fallback: creates lock_path as a directory (matches bash behavior)
    """

    def __init__(self, lock_path: Path, timeout: float = 3.0) -> None:
        self.lock_path = Path(lock_path)
        self.timeout = timeout
        self._fd: Optional[IO[str]] = None
        self._method = _LOCK_IMPL

    def __enter__(self) -> "FileLock":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

        if self._method == "fcntl":
            self._acquire_fcntl()
        elif self._method == "msvcrt":
            self._acquire_msvcrt()
        else:
            self._acquire_mkdir()
        return self

    def __exit__(self, *args) -> None:
        if self._method == "fcntl":
            self._release_fcntl()
        elif self._method == "msvcrt":
            self._release_msvcrt()
        else:
            self._release_mkdir()

    def _acquire_fcntl(self) -> None:
        self._fd = open(self.lock_path, "w")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    # Force-acquire: close, remove, reopen
                    self._fd.close()
                    try:
                        self.lock_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    self._fd = open(self.lock_path, "w")
                    fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return
                time.sleep(0.1)

    def _release_fcntl(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                self._fd.close()
            except OSError:
                pass
            self._fd = None

    def _acquire_msvcrt(self) -> None:
        self._fd = open(self.lock_path, "w")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                msvcrt.locking(self._fd.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
                return
            except (OSError, IOError):
                if time.monotonic() >= deadline:
                    self._fd.close()
                    try:
                        self.lock_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    self._fd = open(self.lock_path, "w")
                    msvcrt.locking(self._fd.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
                    return
                time.sleep(0.1)

    def _release_msvcrt(self) -> None:
        if self._fd is not None:
            try:
                msvcrt.locking(self._fd.fileno(), msvcrt.LK_UNLOCK, 1)  # type: ignore[attr-defined]
            except OSError:
                pass
            try:
                self._fd.close()
            except OSError:
                pass
            self._fd = None

    def _acquire_mkdir(self) -> None:
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.lock_path.mkdir()
                return
            except FileExistsError:
                if time.monotonic() >= deadline:
                    # Force-acquire: remove and recreate (matches bash lines 67-70)
                    try:
                        shutil.rmtree(self.lock_path)
                    except OSError:
                        pass
                    try:
                        self.lock_path.mkdir()
                    except FileExistsError:
                        pass
                    return
                time.sleep(0.1)

    def _release_mkdir(self) -> None:
        try:
            self.lock_path.rmdir()
        except OSError:
            pass


class StateManager:
    """Per-session key-value state backed by a YAML file.

    All values are stored as strings (matching bash behavior where jq
    reads/writes everything as string arguments via --arg).

    The state_file and lock_path are set by the adapter when resolving
    the session (e.g., state_<session_id>.yaml with .lock_<session_id>).
    """

    def __init__(
        self,
        state_dir: Path,
        state_file: "Path | None" = None,
        lock_path: "Path | None" = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.state_file = Path(state_file) if state_file is not None else None
        self._lock_path = Path(lock_path) if lock_path is not None else None

    def init_state(self) -> None:
        """Create state directory and file.

        If file doesn't exist, create with empty dict.
        If file exists but is corrupted, overwrite with empty dict.
        Matches bash init_state() at common.sh:49-59.
        """
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if self.state_file is None:
            return
        if not self.state_file.exists():
            self._write({})
        else:
            # Validate existing file; overwrite if corrupted
            try:
                data = self._read()
                if not isinstance(data, dict):
                    self._write({})
            except Exception:
                self._write({})

    def get(self, key: str) -> "str | None":
        """Read a value by key. Returns None if key missing or file missing.

        Does NOT acquire lock (read-only, matches bash get_state which
        doesn't call _lock_state).
        """
        data = self._read_safe()
        val = data.get(key)
        if val is None:
            return None
        return str(val)

    def set(self, key: str, value: str) -> None:
        """Set a key-value pair. Acquires lock.

        Value is always stored as string (matches bash: jq --arg v "$2").
        Uses atomic write: write to .tmp.{pid} then rename.
        """
        if self.state_file is None:
            return
        try:
            with self._lock():
                data = self._read_safe()
                data[key] = str(value)
                self._write(data)
        except Exception as e:
            error(f"set_state failed for key={key}: {e}")

    def delete(self, key: str) -> None:
        """Remove a key. No-op if missing. Acquires lock."""
        if self.state_file is None:
            return
        try:
            with self._lock():
                data = self._read_safe()
                data.pop(key, None)
                self._write(data)
        except Exception as e:
            error(f"del_state failed for key={key}: {e}")

    def increment(self, key: str) -> None:
        """Increment a numeric string value. Acquires lock.

        Missing key treated as "0" -> becomes "1".
        Non-numeric value treated as 0 -> becomes "1".
        Matches bash inc_state() at common.sh:101-108.
        """
        if self.state_file is None:
            return
        try:
            with self._lock():
                data = self._read_safe()
                current = data.get(key, "0")
                try:
                    num = int(current)
                except (ValueError, TypeError):
                    num = 0
                data[key] = str(num + 1)
                self._write(data)
        except Exception as e:
            error(f"inc_state failed for key={key}: {e}")

    def _lock(self) -> FileLock:
        """Return a FileLock for this state file."""
        if self._lock_path is not None:
            return FileLock(self._lock_path)
        if self.state_file is None:
            raise RuntimeError("StateManager has neither lock_path nor state_file")
        return FileLock(self.state_file.with_suffix(".lock"))

    def _read_safe(self) -> dict:
        """Read state file, return {} on any error (missing, corrupt, permission)."""
        try:
            return self._read()
        except Exception:
            return {}

    def _read(self) -> dict:
        """Read state file, raise on error."""
        if self.state_file is None:
            return {}
        text = self.state_file.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise ValueError(f"State file is not a mapping: {type(data)}")
        return data

    def _write(self, data: dict) -> None:
        """Write dict to state file atomically via tmp+rename."""
        if self.state_file is None:
            return
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(f".tmp.{os.getpid()}")
        try:
            tmp.write_text(yaml.safe_dump(data, default_flow_style=False), encoding="utf-8")
            tmp.replace(self.state_file)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise


# ── OTLP Span Building ────────────────────────────────────────────────────

# Map string kind names to OTLP SpanKind integer values.
# Case-insensitive lookup (caller passes "LLM", "TOOL", etc.)
SPAN_KIND_MAP: dict = {
    # kind 1 = SPAN_KIND_INTERNAL (used for LLM, CHAIN, TOOL, INTERNAL in OpenInference)
    "": 1,
    "llm": 1,
    "chain": 1,
    "tool": 1,
    "internal": 1,
    "span_kind_internal": 1,
    # kind 2 = SPAN_KIND_SERVER
    "server": 2,
    "span_kind_server": 2,
    # kind 3 = SPAN_KIND_CLIENT
    "client": 3,
    "span_kind_client": 3,
    # kind 4 = SPAN_KIND_PRODUCER
    "producer": 4,
    "span_kind_producer": 4,
    # kind 5 = SPAN_KIND_CONSUMER
    "consumer": 5,
    "span_kind_consumer": 5,
    # kind 0 = SPAN_KIND_UNSPECIFIED
    "unspecified": 0,
    "span_kind_unspecified": 0,
}


def _resolve_kind(kind: str) -> int:
    """Resolve a span kind string to an OTLP SpanKind integer.

    Case-insensitive lookup in SPAN_KIND_MAP. If not found and numeric, parse
    as int. Otherwise default to 1 (SPAN_KIND_INTERNAL).
    """
    lookup = SPAN_KIND_MAP.get(kind.lower())
    if lookup is not None:
        return lookup
    # Numeric string (matches bash: if [[ "$kind" =~ ^[0-9]+$ ]])
    try:
        return int(kind)
    except (ValueError, TypeError):
        return 1


def _to_otlp_attr_value(value) -> dict:
    """Convert a Python value to OTLP attribute value dict.

    Matches the jq type-detection logic in build_span:
    - bool → {"boolValue": v}          (check BEFORE int — bool is subclass of int)
    - int → {"intValue": v}
    - float with no fractional part → {"intValue": int(v)}   (matches jq: floor == value)
    - float with fractional part → {"doubleValue": v}
    - everything else → {"stringValue": str(v)}
    """
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": value}
    if isinstance(value, float):
        if value == int(value):
            return {"intValue": int(value)}
        return {"doubleValue": value}
    return {"stringValue": str(value)}


def _attrs_to_otlp(attrs: dict) -> list:
    """Convert a flat Python dict to OTLP attribute list.

    Input:  {"session.id": "abc", "llm.token_count.prompt": 100}
    Output: [{"key": "session.id", "value": {"stringValue": "abc"}},
             {"key": "llm.token_count.prompt", "value": {"intValue": 100}}]
    """
    return [{"key": k, "value": _to_otlp_attr_value(v)} for k, v in attrs.items()]


def build_span(
    name: str,
    kind: str,
    span_id: str,
    trace_id: str,
    parent_span_id: str = "",
    start_ms: "int | str" = 0,
    end_ms: "int | str" = 0,
    attrs: "dict | None" = None,
    service_name: str = "coding-harness-tracing",
    scope_name: str = "coding-harness-tracing",
) -> dict:
    """Build an OTLP JSON span payload.

    Returns a dict matching the exact structure produced by core/common.sh:build_span().

    Timestamp handling: start_ms and end_ms are in milliseconds. The OTLP format
    requires nanoseconds as strings. Bash appends "000000" (line 312):
        "startTimeUnixNano":"${start}000000"
    Python does the same: f"{int(start_ms)}000000"

    If end_ms is empty/None/0, defaults to start_ms (matches bash: end="${7:-$start}").
    """
    if attrs is None:
        attrs = {}

    start = int(start_ms) if start_ms else 0
    end = int(end_ms) if end_ms else start

    kind_value = _resolve_kind(kind or "")

    span_obj = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": name,
        "kind": kind_value,
        "startTimeUnixNano": f"{start}000000",
        "endTimeUnixNano": f"{end}000000",
        "attributes": _attrs_to_otlp(attrs),
        "status": {"code": 1},
    }

    # parentSpanId only included if non-empty (matches bash conditional)
    if parent_span_id:
        span_obj["parentSpanId"] = parent_span_id

    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service_name}}]},
                "scopeSpans": [{"scope": {"name": scope_name}, "spans": [span_obj]}],
            }
        ]
    }


def build_multi_span(
    span_payloads: list,
    service_name: str = "coding-harness-tracing",
    scope_name: str = "coding-harness-tracing",
) -> dict:
    """Merge multiple build_span() outputs into a single resourceSpans payload.

    Extracts the span object from each payload's
    resourceSpans[0].scopeSpans[0].spans[0] and combines them under
    one resource/scope envelope.

    Returns {} if no valid spans found (matches bash: echo "{}"; return 1).
    """
    spans = []
    for payload in span_payloads:
        try:
            span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
            spans.append(span)
        except (KeyError, IndexError, TypeError):
            continue

    if not spans:
        return {}

    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service_name}}]},
                "scopeSpans": [{"scope": {"name": scope_name}, "spans": spans}],
            }
        ]
    }
