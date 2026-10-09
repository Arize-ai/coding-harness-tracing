"""Tests for ``arize-harness hook`` (core/cli.py) and the hook table (core/hook_table.py)."""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

from core import cli, hook_table
from tests._pyproject import PYPROJECT, REPO_ROOT, parse_project_scripts

CLAUDE_DIR = REPO_ROOT / "tracing" / "claude_code"


def _legacy_hook_scripts(pyproject: Path = PYPROJECT) -> dict[str, str]:
    scripts = parse_project_scripts(pyproject.read_text())
    return {name: target for name, target in scripts.items() if name.startswith("arize-hook-")}


def _claude_table_scripts() -> dict[str, str]:
    return {
        hook_table.legacy_entry_point("claude", event): target
        for event, target in hook_table.EVENT_HOOKS["claude"].items()
    }


# ---------------------------------------------------------------------------
# Hook table <-> pyproject parity
# ---------------------------------------------------------------------------


class TestHookTableParity:
    def test_parser_reads_both_toml_quote_styles(self) -> None:
        """A single-quoted entry must not be invisible to the parity tests below."""
        text = '[project.scripts]\na = "m:f"\nb = \'m:g\'  # comment\n[tool.x]\nc = "m:h"\n'
        assert parse_project_scripts(text) == {"a": "m:f", "b": "m:g"}

    @pytest.mark.parametrize("line", ['"arize-hook-x" = "m:f"', 'arize-hook-x = "m:f" extra', "arize-hook-x = m:f"])
    def test_parser_rejects_lines_it_cannot_read(self, line) -> None:
        """An entry the parser can't read must fail loudly, not vanish from the parity tests."""
        with pytest.raises(ValueError, match="unparsed"):
            parse_project_scripts(f"[project.scripts]\n{line}\n")

    def test_arize_harness_registered(self) -> None:
        assert parse_project_scripts(PYPROJECT.read_text()).get("arize-harness") == "core.cli:main"

    def test_every_legacy_command_has_a_matching_table_entry(self) -> None:
        """Adding an ``arize-hook-*`` command without a table entry fails here."""
        table = {hook_table.legacy_entry_point(h, e): target for h, e, target in hook_table.iter_hooks()}
        assert table == _legacy_hook_scripts()

    def test_legacy_names_are_unique(self) -> None:
        names = [hook_table.legacy_entry_point(h, e) for h, e, _ in hook_table.iter_hooks()]
        assert len(names) == len(set(names))

    def test_harnesses_are_not_both_single_and_event(self) -> None:
        assert not set(hook_table.SINGLE_HOOKS) & set(hook_table.EVENT_HOOKS)


class TestClaudePluginParity:
    """The Claude plugin keeps its own hook lists; each must match the table's Claude entries."""

    def test_plugin_pyproject_matches_table(self) -> None:
        assert _legacy_hook_scripts(CLAUDE_DIR / "pyproject.toml") == _claude_table_scripts()

    def test_run_hook_launchers_match_table(self) -> None:
        """``run-hook`` writes one launcher per ``<suffix>:<function>`` spec."""
        text = (CLAUDE_DIR / "scripts" / "run-hook").read_text()
        spec_block = re.search(r"for spec in(.*?); do", text, re.DOTALL)
        assert spec_block, "run-hook launcher spec list not found"
        launchers = {
            f"arize-hook-{suffix}": f"tracing.claude_code.hooks.handlers:{function}"
            for suffix, function in re.findall(r"([a-z-]+):([a-z_]+)", spec_block.group(1))
        }
        assert launchers == _claude_table_scripts()

    def test_hook_events_constant_matches_table(self) -> None:
        from tracing.claude_code.constants import HOOK_EVENTS

        assert set(HOOK_EVENTS.values()) == set(_claude_table_scripts())


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_handlers(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Route one event harness and one single-entry harness to a recording handler."""
    calls: list[dict] = []
    module = types.ModuleType("fake_hook_handlers")

    def record() -> str:
        calls.append({"argv": list(sys.argv), "stdin": sys.stdin.read()})
        return "handler-result"

    module.record = record  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fake_hook_handlers", module)
    monkeypatch.setattr(cli, "EVENT_HOOKS", {"claude": {"stop": "fake_hook_handlers:record"}})
    monkeypatch.setattr(cli, "SINGLE_HOOKS", {"cursor": "fake_hook_handlers:record"})
    monkeypatch.setattr(sys, "argv", ["arize-harness"])
    return calls


class TestDispatch:
    def test_event_hook_gets_stdin_and_legacy_argv(self, fake_handlers, monkeypatch) -> None:
        monkeypatch.setattr(sys, "stdin", io.StringIO('{"hook_event_name": "Stop"}'))
        result = cli.main(["hook", "claude", "stop", "extra"])
        assert result == "handler-result"
        assert fake_handlers == [{"argv": ["arize-hook-stop", "extra"], "stdin": '{"hook_event_name": "Stop"}'}]

    def test_claude_code_alias(self, fake_handlers, monkeypatch) -> None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        cli.main(["hook", "claude-code", "stop"])
        assert [c["argv"] for c in fake_handlers] == [["arize-hook-stop"]]

    def test_single_entry_harness_passes_everything_through(self, fake_handlers, monkeypatch) -> None:
        """``hook cursor foo`` runs the Cursor handler with ``foo`` in argv; it is not an unknown event."""
        monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
        cli.main(["hook", "cursor", "foo", "--bar"])
        assert fake_handlers == [{"argv": ["arize-hook-cursor", "foo", "--bar"], "stdin": "{}"}]

    def test_codex_notify_payload_stays_at_argv_1(self, monkeypatch, tmp_path) -> None:
        from tracing.codex.hooks import handlers as codex_handlers

        received: list[dict] = []
        monkeypatch.setattr(sys, "argv", ["arize-harness"])
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        monkeypatch.setattr(codex_handlers, "_handle_notify", received.append)
        payload = {"type": "agent-turn-complete", "thread-id": "t1", "turn-id": "u1"}

        assert cli.main(["hook", "codex", "notify", json.dumps(payload)]) is None
        assert received == [payload]


class TestFailuresNeverBlock:
    @pytest.fixture(autouse=True)
    def hook_log(self, monkeypatch, tmp_path) -> Path:
        """Keep load-failure tracebacks out of the developer's real ~/.arize logs."""
        log = tmp_path / "hook.log"
        monkeypatch.setenv("ARIZE_LOG_FILE", str(log))
        return log

    @pytest.mark.parametrize(
        "args",
        [
            ["hook"],
            ["hook", "nope"],
            ["hook", "nope", "stop"],
            ["hook", "claude"],
            ["hook", "claude", "nope"],
            ["hook", "gemini", "stop"],
        ],
    )
    def test_unknown_or_missing_names_exit_0_with_one_stderr_line(self, args, capsys) -> None:
        assert cli.main(args) == 0
        out, err = capsys.readouterr()
        assert out == ""
        assert len(err.strip().splitlines()) == 1

    def test_import_failure_exits_0_with_one_stderr_line(self, monkeypatch, capsys, hook_log) -> None:
        monkeypatch.setattr(cli, "SINGLE_HOOKS", {"cursor": "tracing.does_not_exist.handlers:main"})
        assert cli.main(["hook", "cursor"]) == 0
        out, err = capsys.readouterr()
        assert out == ""
        assert len(err.strip().splitlines()) == 1
        assert "tracing.does_not_exist" in err
        assert str(hook_log) in err

    def test_import_failure_writes_traceback_to_log(self, monkeypatch, hook_log) -> None:
        """Exit 0 hides stderr from most harnesses, so the log must carry the traceback."""
        monkeypatch.setattr(cli, "SINGLE_HOOKS", {"cursor": "tracing.does_not_exist.handlers:main"})
        cli.main(["hook", "cursor"])
        text = hook_log.read_text()
        assert "could not load hook tracing.does_not_exist.handlers:main" in text
        assert "Traceback (most recent call last)" in text
        assert "ModuleNotFoundError" in text

    @pytest.mark.parametrize("harness", sorted({h for h, _event, _target in hook_table.iter_hooks()}))
    def test_import_failure_defaults_to_harness_log(self, harness, monkeypatch, tmp_path) -> None:
        """Without ARIZE_LOG_FILE the traceback goes to the harness's own default log."""
        monkeypatch.delenv("ARIZE_LOG_FILE")
        log = tmp_path / "logs" / f"{harness}.log"
        monkeypatch.setattr(cli, "_default_log_file", lambda name: log if name == harness else tmp_path / "wrong.log")
        broken = "tracing.does_not_exist.handlers:main"
        if harness in hook_table.SINGLE_HOOKS:
            monkeypatch.setattr(cli, "SINGLE_HOOKS", {harness: broken})
            monkeypatch.setattr(cli, "EVENT_HOOKS", {})
            args = ["hook", harness]
        else:
            monkeypatch.setattr(cli, "EVENT_HOOKS", {harness: {"stop": broken}})
            monkeypatch.setattr(cli, "SINGLE_HOOKS", {})
            args = ["hook", harness, "stop"]

        assert cli.main(args) == 0
        assert "ModuleNotFoundError" in log.read_text()
        assert not (tmp_path / "wrong.log").exists()

    def test_default_log_files_match_each_harness_adapter(self) -> None:
        """The dispatcher's default log must be the file each harness's adapter sets as ARIZE_LOG_FILE."""
        from core.constants import HARNESSES
        from tracing.devin.constants import DEFAULT_LOG_FILE as DEVIN_LOG
        from tracing.kiro.constants import DEFAULT_LOG_FILE as KIRO_LOG

        expected = {
            hook_table.canonical_harness(key): metadata["default_log_file"] for key, metadata in HARNESSES.items()
        }
        expected.update({"kiro": KIRO_LOG, "devin": DEVIN_LOG})
        harnesses = {h for h, _event, _target in hook_table.iter_hooks()}
        assert {h: cli._default_log_file(h) for h in harnesses} == {h: expected[h] for h in harnesses}

    def test_import_failure_after_stderr_redirect_still_warns_on_stderr(self, monkeypatch, tmp_path, capsys, hook_log):
        """An adapter may redirect sys.stderr to the log before a later import fails.

        The warning must still reach the harness's stderr, and the log must get
        the failure once, not once from the warning and again from the traceback.
        """
        from core import common

        (tmp_path / "redirecting_hook_handlers.py").write_text(
            "from core.common import redirect_stderr_to_log_file\n"
            "redirect_stderr_to_log_file()\n"
            "import tracing.does_not_exist\n"
        )
        monkeypatch.syspath_prepend(str(tmp_path))
        monkeypatch.delitem(sys.modules, "redirecting_hook_handlers", raising=False)
        monkeypatch.setattr(cli, "SINGLE_HOOKS", {"cursor": "redirecting_hook_handlers:main"})
        # Start with no redirect active, whatever earlier tests' adapter imports left behind.
        monkeypatch.setattr(common, "_redirected_log_fh", None)
        monkeypatch.setattr(common, "_original_stderr", None)
        try:
            assert cli.main(["hook", "cursor"]) == 0
        finally:
            common.restore_stderr_from_log_file()

        err = capsys.readouterr().err
        assert len(err.strip().splitlines()) == 1
        assert "redirecting_hook_handlers" in err
        assert hook_log.read_text().count("could not load hook") == 1

    def test_sys_exit_during_import_exits_0(self, monkeypatch, tmp_path, capsys) -> None:
        (tmp_path / "exiting_hook_handlers.py").write_text("import sys\nsys.exit(1)\n")
        monkeypatch.syspath_prepend(str(tmp_path))
        monkeypatch.delitem(sys.modules, "exiting_hook_handlers", raising=False)
        monkeypatch.setattr(cli, "SINGLE_HOOKS", {"cursor": "exiting_hook_handlers:main"})
        assert cli.main(["hook", "cursor"]) == 0
        assert len(capsys.readouterr().err.strip().splitlines()) == 1

    def test_missing_function_exits_0(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(cli, "SINGLE_HOOKS", {"cursor": "core.hook_table:no_such_function"})
        assert cli.main(["hook", "cursor"]) == 0
        assert len(capsys.readouterr().err.strip().splitlines()) == 1

    def test_handler_exceptions_propagate_like_legacy_commands(self, monkeypatch) -> None:
        module = types.ModuleType("raising_hook_handlers")

        def boom() -> None:
            raise RuntimeError("boom")

        module.boom = boom  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "raising_hook_handlers", module)
        monkeypatch.setattr(cli, "SINGLE_HOOKS", {"cursor": "raising_hook_handlers:boom"})
        monkeypatch.setattr(sys, "argv", ["arize-harness"])
        with pytest.raises(RuntimeError, match="boom"):
            cli.main(["hook", "cursor"])

    @pytest.mark.parametrize("args", [[], ["nope"], ["hooks", "claude", "pre-tool-use"], ["claude", "stop"]])
    def test_usage_error_exits_0(self, args, capsys) -> None:
        """Claude Code treats exit 2 as a blocking error, so a mistyped hook command must not return it."""
        assert cli.main(args) == 0
        out, err = capsys.readouterr()
        assert out == ""
        assert "usage: arize-harness" in err


class TestList:
    def test_lists_every_pair(self, capsys) -> None:
        assert cli.main(["hook", "--list"]) == 0
        lines = capsys.readouterr().out.splitlines()
        expected = [h if e is None else f"{h} {e}" for h, e, _ in hook_table.iter_hooks()]
        assert lines == expected
        assert len(lines) == len(_legacy_hook_scripts())


# ---------------------------------------------------------------------------
# Startup imports (fresh interpreter)
# ---------------------------------------------------------------------------

_REPORT_MODULES = """
import atexit, json, sys
atexit.register(lambda: sys.__stderr__.write("MODULES=" + json.dumps(sorted(sys.modules)) + "\\n"))
from core.cli import main
sys.exit(main(sys.argv[1:]))
"""


def _modules_loaded_by(args: list[str], stdin: str, tmp_path: Path) -> set[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ARIZE_", "PHOENIX_", "OTEL_"))}
    # ARIZE_DISABLE_FORK keeps span sends in this process, so no detached sender
    # outlives the test and every module a hook imports is in the report.
    env.update(
        {"HOME": str(tmp_path), "USERPROFILE": str(tmp_path), "ARIZE_DRY_RUN": "true", "ARIZE_DISABLE_FORK": "true"}
    )
    result = subprocess.run(
        [sys.executable, "-c", _REPORT_MODULES, *args],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    line = next(line for line in result.stderr.splitlines() if line.startswith("MODULES="))
    return set(json.loads(line[len("MODULES=") :]))


def _other_harness_modules(modules: set[str], harness_package: str) -> set[str]:
    return {m for m in modules if m.startswith("tracing.") and not m.startswith(f"tracing.{harness_package}")}


class TestStartupImports:
    def test_list_imports_no_harness_and_no_dotenv(self, tmp_path) -> None:
        modules = _modules_loaded_by(["hook", "--list"], "", tmp_path)
        assert not {m for m in modules if m.startswith("tracing")}
        assert "dotenv" not in modules
        assert "core.setup" not in modules

    def test_claude_hook_imports_only_claude(self, tmp_path) -> None:
        payload = {"session_id": "s1", "cwd": str(tmp_path), "hook_event_name": "Stop"}
        modules = _modules_loaded_by(["hook", "claude", "stop"], json.dumps(payload), tmp_path)
        assert "tracing.claude_code.hooks.handlers" in modules
        assert _other_harness_modules(modules, "claude_code") == set()
        assert "dotenv" not in modules
        assert "core.setup" not in modules

    def test_gemini_hook_imports_only_gemini(self, tmp_path) -> None:
        modules = _modules_loaded_by(["hook", "gemini", "before-tool"], "{}", tmp_path)
        assert "tracing.gemini.hooks.handlers" in modules
        assert _other_harness_modules(modules, "gemini") == set()
        assert "dotenv" not in modules
