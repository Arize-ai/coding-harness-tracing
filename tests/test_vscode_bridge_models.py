"""Tests for core.vscode_bridge.models builder functions."""

import json

import pytest

from core.vscode_bridge.models import (
    HARNESS_KEYS,
    build_backend,
    build_codex_buffer,
    build_harness_status_item,
    build_install_request,
    build_kiro_options,
    build_operation_result,
    build_status,
)

# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class TestBuildBackend:
    def test_arize_backend(self):
        b = build_backend("arize", "https://otlp.arize.com", "key123", "space1")
        assert b == {
            "target": "arize",
            "endpoint": "https://otlp.arize.com",
            "api_key": "key123",
            "space_id": "space1",
        }

    def test_phoenix_backend(self):
        b = build_backend("phoenix", "http://localhost:6006", "")
        assert b == {
            "target": "phoenix",
            "endpoint": "http://localhost:6006",
            "api_key": "",
            "space_id": None,
        }

    def test_phoenix_no_auth(self):
        b = build_backend("phoenix", "http://localhost:6006")
        assert b["api_key"] == ""
        assert b["space_id"] is None

    def test_arize_requires_space_id(self):
        with pytest.raises(ValueError, match="space_id is required"):
            build_backend("arize", "https://otlp.arize.com", "key")

    def test_phoenix_rejects_space_id(self):
        with pytest.raises(ValueError, match="space_id must be None"):
            build_backend("phoenix", "http://localhost:6006", "", "space1")

    def test_unknown_target_rejected(self):
        with pytest.raises(ValueError, match="unknown target"):
            build_backend("other", "http://x")

    def test_empty_endpoint_rejected(self):
        with pytest.raises(ValueError, match="endpoint"):
            build_backend("phoenix", "")


# ---------------------------------------------------------------------------
# HarnessStatusItem
# ---------------------------------------------------------------------------


class TestBuildHarnessStatusItem:
    def test_unconfigured(self):
        h = build_harness_status_item("codex")
        assert h == {
            "name": "codex",
            "configured": False,
            "project_name": None,
            "backend": None,
            "scope": None,
            "kiro_options": None,
            "repo_paths": None,
        }

    def test_configured(self):
        backend = build_backend("phoenix", "http://localhost:6006")
        h = build_harness_status_item("cursor", configured=True, project_name="my-proj", backend=backend)
        assert h["configured"] is True
        assert h["project_name"] == "my-proj"
        assert h["backend"]["target"] == "phoenix"

    def test_unknown_harness_rejected(self):
        with pytest.raises(ValueError, match="unknown harness"):
            build_harness_status_item("vim")

    def test_all_harness_keys_accepted(self):
        for key in HARNESS_KEYS:
            h = build_harness_status_item(key)
            assert h["name"] == key

    def test_exact_keys(self):
        h = build_harness_status_item("gemini")
        assert set(h.keys()) == {
            "name",
            "configured",
            "project_name",
            "backend",
            "scope",
            "kiro_options",
            "repo_paths",
        }

    def test_kiro_options_for_kiro(self):
        h = build_harness_status_item(
            "kiro",
            configured=True,
            kiro_options=build_kiro_options("my-agent"),
        )
        assert h["kiro_options"] == {"agent_name": "my-agent", "set_default": False}

    def test_kiro_options_rejected_for_other_harness(self):
        with pytest.raises(ValueError, match="kiro_options only valid"):
            build_harness_status_item(
                "codex",
                kiro_options={"agent_name": "x", "set_default": False},
            )


# ---------------------------------------------------------------------------
# StatusPayload
# ---------------------------------------------------------------------------


class TestBuildStatus:
    def test_defaults(self):
        s = build_status(success=True)
        assert s["success"] is True
        assert s["error"] is None
        assert s["user_id"] is None
        assert len(s["harnesses"]) == 6
        assert s["logging"] is None
        assert s["codex_buffer"] is None
        names = [h["name"] for h in s["harnesses"]]
        assert tuple(names) == HARNESS_KEYS

    def test_with_logging(self):
        s = build_status(
            success=True,
            logging={"prompts": False, "tool_details": True, "tool_content": False},
        )
        assert s["logging"] == {
            "prompts": False,
            "tool_details": True,
            "tool_content": False,
        }

    def test_logging_defaults_missing_keys(self):
        s = build_status(success=True, logging={})
        assert s["logging"] == {
            "prompts": True,
            "tool_details": True,
            "tool_content": True,
        }

    def test_error_payload(self):
        s = build_status(success=False, error="config_not_found")
        assert s["success"] is False
        assert s["error"] == "config_not_found"

    def test_exact_keys(self):
        s = build_status(success=True)
        assert set(s.keys()) == {
            "success",
            "error",
            "user_id",
            "harnesses",
            "logging",
            "codex_buffer",
        }


# ---------------------------------------------------------------------------
# InstallRequest
# ---------------------------------------------------------------------------


class TestBuildInstallRequest:
    def test_minimal(self):
        backend = build_backend("phoenix", "http://localhost:6006")
        r = build_install_request("codex", backend, "my-project")
        assert r == {
            "harness": "codex",
            "backend": backend,
            "project_name": "my-project",
            "user_id": None,
            "with_skills": False,
            "logging": None,
            "kiro_options": None,
            "repo_path": None,
        }

    def test_full(self):
        backend = build_backend("arize", "https://otlp.arize.com", "k", "s")
        r = build_install_request(
            "claude-code",
            backend,
            "proj",
            user_id="u1",
            with_skills=True,
            logging={"prompts": True, "tool_details": False, "tool_content": True},
        )
        assert r["with_skills"] is True
        assert r["user_id"] == "u1"
        assert r["logging"]["tool_details"] is False

    def test_unknown_harness(self):
        backend = build_backend("phoenix", "http://localhost:6006")
        with pytest.raises(ValueError, match="unknown harness"):
            build_install_request("neovim", backend, "proj")

    def test_empty_project_name(self):
        backend = build_backend("phoenix", "http://localhost:6006")
        with pytest.raises(ValueError, match="project_name"):
            build_install_request("codex", backend, "")

    def test_exact_keys(self):
        backend = build_backend("phoenix", "http://localhost:6006")
        r = build_install_request("copilot", backend, "p")
        assert set(r.keys()) == {
            "harness",
            "backend",
            "project_name",
            "user_id",
            "with_skills",
            "logging",
            "kiro_options",
            "repo_path",
        }

    def test_kiro_options_for_kiro(self):
        backend = build_backend("phoenix", "http://localhost:6006")
        r = build_install_request(
            "kiro",
            backend,
            "proj",
            kiro_options=build_kiro_options("x", set_default=True),
        )
        assert r["kiro_options"] == {"agent_name": "x", "set_default": True}

    def test_kiro_options_rejected_for_other_harness(self):
        backend = build_backend("phoenix", "http://localhost:6006")
        with pytest.raises(ValueError, match="kiro_options only valid"):
            build_install_request(
                "codex",
                backend,
                "proj",
                kiro_options={"agent_name": "x", "set_default": False},
            )

    def test_kiro_options_default_none(self):
        backend = build_backend("phoenix", "http://localhost:6006")
        for harness in HARNESS_KEYS:
            r = build_install_request(harness, backend, "proj")
            assert "kiro_options" in r
            assert r["kiro_options"] is None


# ---------------------------------------------------------------------------
# OperationResult
# ---------------------------------------------------------------------------


class TestBuildOperationResult:
    def test_success(self):
        r = build_operation_result(True, harness="cursor", logs=["installed"])
        assert r == {
            "success": True,
            "error": None,
            "harness": "cursor",
            "logs": ["installed"],
        }

    def test_failure(self):
        r = build_operation_result(False, error="install_failed", harness="codex")
        assert r["success"] is False
        assert r["error"] == "install_failed"

    def test_no_harness_for_buffer_ops(self):
        r = build_operation_result(True)
        assert r["harness"] is None
        assert r["logs"] == []

    def test_unknown_harness(self):
        with pytest.raises(ValueError, match="unknown harness"):
            build_operation_result(True, harness="zsh")

    def test_exact_keys(self):
        r = build_operation_result(True)
        assert set(r.keys()) == {"success", "error", "harness", "logs"}


# ---------------------------------------------------------------------------
# CodexBufferPayload
# ---------------------------------------------------------------------------


class TestBuildCodexBuffer:
    def test_running(self):
        c = build_codex_buffer(True, state="running", host="127.0.0.1", port=4318, pid=12345)
        assert c == {
            "success": True,
            "error": None,
            "state": "running",
            "host": "127.0.0.1",
            "port": 4318,
            "pid": 12345,
        }

    def test_unknown_default(self):
        c = build_codex_buffer(False, error="not_running")
        assert c["state"] == "unknown"
        assert c["host"] is None
        assert c["port"] is None
        assert c["pid"] is None

    def test_invalid_state(self):
        with pytest.raises(ValueError, match="unknown codex buffer state"):
            build_codex_buffer(True, state="broken")

    def test_exact_keys(self):
        c = build_codex_buffer(True, state="stopped")
        assert set(c.keys()) == {"success", "error", "state", "host", "port", "pid"}


# ---------------------------------------------------------------------------
# JSON round-trip
# ---------------------------------------------------------------------------


class TestJsonRoundTrip:
    def test_status_roundtrip(self):
        backend = build_backend("arize", "https://otlp.arize.com", "k", "s")
        harnesses = [
            build_harness_status_item("claude-code", True, "proj", backend),
        ] + [build_harness_status_item(k) for k in HARNESS_KEYS if k != "claude-code"]
        codex_buf = build_codex_buffer(True, state="running", host="127.0.0.1", port=4318, pid=1)
        status = build_status(
            success=True,
            user_id="u1",
            harnesses=harnesses,
            logging={"prompts": True, "tool_details": True, "tool_content": False},
            codex_buffer=codex_buf,
        )
        roundtripped = json.loads(json.dumps(status))
        assert roundtripped == status

    def test_install_request_roundtrip(self):
        backend = build_backend("phoenix", "http://localhost:6006")
        req = build_install_request("gemini", backend, "proj", logging={"prompts": False})
        roundtripped = json.loads(json.dumps(req))
        assert roundtripped == req

    def test_operation_result_roundtrip(self):
        r = build_operation_result(False, error="timeout", harness="copilot", logs=["a", "b"])
        roundtripped = json.loads(json.dumps(r))
        assert roundtripped == r

    def test_codex_buffer_roundtrip(self):
        c = build_codex_buffer(True, state="stale", host="127.0.0.1", port=9999, pid=42)
        roundtripped = json.loads(json.dumps(c))
        assert roundtripped == c


# ---------------------------------------------------------------------------
# HARNESS_KEYS constant
# ---------------------------------------------------------------------------


class TestHarnessKeys:
    def test_all_six_present(self):
        assert len(HARNESS_KEYS) == 6

    def test_is_tuple(self):
        assert isinstance(HARNESS_KEYS, tuple)

    def test_expected_values(self):
        assert set(HARNESS_KEYS) == {
            "claude-code",
            "codex",
            "cursor",
            "copilot",
            "gemini",
            "kiro",
        }

    def test_contains_kiro(self):
        assert "kiro" in HARNESS_KEYS


# ---------------------------------------------------------------------------
# KiroOptions
# ---------------------------------------------------------------------------


class TestBuildKiroOptions:
    def test_valid(self):
        assert build_kiro_options("my-agent", set_default=True) == {
            "agent_name": "my-agent",
            "set_default": True,
        }

    def test_default_set_default(self):
        assert build_kiro_options("my-agent") == {
            "agent_name": "my-agent",
            "set_default": False,
        }

    def test_rejects_empty_name(self):
        with pytest.raises(ValueError, match="agent_name"):
            build_kiro_options("")


# ---------------------------------------------------------------------------
# Copilot repo paths (HarnessStatusItem.repo_paths, InstallRequest.repo_path)
# ---------------------------------------------------------------------------


class TestRepoPaths:
    def test_status_item_with_repo_paths(self):
        h = build_harness_status_item(name="copilot", repo_paths=["/a", "/b"])
        assert h["repo_paths"] == ["/a", "/b"]

    def test_status_item_default_none(self):
        h = build_harness_status_item(name="copilot")
        assert h["repo_paths"] is None

    def test_status_item_empty_list(self):
        h = build_harness_status_item(name="copilot", repo_paths=[])
        assert h["repo_paths"] == []

    def test_status_item_rejects_non_copilot(self):
        with pytest.raises(ValueError, match="repo_paths only valid when harness is 'copilot'"):
            build_harness_status_item(name="claude-code", repo_paths=["/x"])

    def test_status_item_rejects_non_string_item(self):
        with pytest.raises(ValueError):
            build_harness_status_item(name="copilot", repo_paths=[123])

    def test_status_item_rejects_empty_string_item(self):
        with pytest.raises(ValueError):
            build_harness_status_item(name="copilot", repo_paths=[""])

    def test_install_request_with_repo_path(self):
        backend = build_backend("phoenix", "http://localhost:6006")
        r = build_install_request(
            harness="copilot",
            backend=backend,
            project_name="proj",
            repo_path="/foo",
        )
        assert r["repo_path"] == "/foo"

    def test_install_request_default_none(self):
        backend = build_backend("phoenix", "http://localhost:6006")
        r = build_install_request(
            harness="copilot",
            backend=backend,
            project_name="proj",
        )
        assert r["repo_path"] is None

    def test_install_request_rejects_non_copilot(self):
        backend = build_backend("phoenix", "http://localhost:6006")
        with pytest.raises(ValueError, match="repo_path only valid when harness is 'copilot'"):
            build_install_request(
                harness="claude-code",
                backend=backend,
                project_name="proj",
                repo_path="/foo",
            )

    def test_install_request_rejects_empty_string(self):
        backend = build_backend("phoenix", "http://localhost:6006")
        with pytest.raises(ValueError, match="repo_path"):
            build_install_request(
                harness="copilot",
                backend=backend,
                project_name="proj",
                repo_path="",
            )
