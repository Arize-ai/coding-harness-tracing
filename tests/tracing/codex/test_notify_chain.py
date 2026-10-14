"""Notification chaining must preserve argv, payloads, and desktop ownership."""

import json
import subprocess
import sys
from unittest.mock import Mock

import pytest

from tracing.codex import install
from tracing.codex.hooks import handlers
from tracing.codex.notify_chain import install_notify, remove_notify, run_previous

HOOK = "/venv/bin/arize-hook-codex-notify"
DESKTOPS = [
    ["/Applications/Computer Use.app/SkyComputerUseClient", "turn-ended"],
    [r"C:\Users\test\codex-computer-use.exe", "turn-ended"],
    [r"C:\Users\test\codex-computer-use-arm64.exe", "turn-ended"],
]


@pytest.mark.parametrize("previous", [[], ["/other hook", "literal argument"], *DESKTOPS])
def test_reinstall_and_uninstall_restore_original(previous):
    command = install_notify(previous, HOOK)
    assert install_notify(command, HOOK) == command
    assert remove_notify(command, HOOK) == previous
    if previous in DESKTOPS:
        assert command[:3] == previous + ["--previous-notify"]
        assert json.loads(command[3]) == [HOOK]


@pytest.mark.parametrize("desktop", DESKTOPS)
def test_desktop_previous_callback_is_preserved(desktop):
    previous = ["/my callback", "fixed arg"]
    original = desktop + ["--previous-notify", json.dumps(previous)]
    command = install_notify(original, HOOK)
    assert json.loads(command[3]) == [HOOK, "--previous-notify", json.dumps(previous)]
    assert install_notify(command, HOOK) == command
    assert remove_notify(command, HOOK) == original


@pytest.mark.parametrize("previous", [["/other", "arg"], *DESKTOPS])
def test_recovers_old_appended_executable(previous):
    command = install_notify(previous + [HOOK], HOOK)
    assert remove_notify(command, HOOK) == previous


def test_recovers_old_append_after_existing_desktop_chain():
    original = DESKTOPS[0] + ["--previous-notify", '["/other", "arg"]']
    command = install_notify(original + [HOOK], HOOK)
    assert remove_notify(command, HOOK) == original


@pytest.mark.parametrize("value", [123, [123], DESKTOPS[0] + ["--previous-notify", "bad-json"]])
def test_invalid_config_not_overwritten(tmp_path, value):
    path = tmp_path / "config.toml"
    path.write_text(f'notify = {json.dumps(value)}\nmodel = "original"\n')
    original = path.read_bytes()
    with pytest.raises(ValueError):
        install._codex_toml_apply(path, HOOK)
    assert path.read_bytes() == original


@pytest.mark.parametrize("text", ['model = "original"\n', 'notify = "/other"\n'])
def test_uninstall_without_registration_does_not_rewrite(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    install._codex_toml_remove(path, HOOK)
    assert path.read_text() == text


def test_real_callback_receives_payload_as_one_literal_argument(tmp_path):
    recorder = tmp_path / "callback with spaces.py"
    output = tmp_path / "args.json"
    recorder.write_text(
        "import json, pathlib, sys\n" "pathlib.Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))\n"
    )
    previous = [sys.executable, str(recorder), str(output), "fixed arg"]
    payload = json.dumps({"type": "agent-turn-complete", "text": "quote ' $() `cmd`\nline"})
    assert run_previous(["--previous-notify", json.dumps(previous), payload]) == payload
    assert json.loads(output.read_text()) == ["fixed arg", payload]


@pytest.mark.parametrize("failure", [OSError("missing"), subprocess.TimeoutExpired("callback", 20), None])
def test_previous_callback_failure_does_not_block_tracing(monkeypatch, failure):
    monkeypatch.setattr("tracing.codex.notify_chain.subprocess.run", Mock(side_effect=failure))
    payload = {"type": "agent-turn-complete", "thread-id": "synthetic-session"}
    monkeypatch.setattr(sys, "argv", [HOOK, "--previous-notify", '["/other"]', json.dumps(payload)])
    monkeypatch.setattr(handlers, "load_env_file", lambda _: None)
    monkeypatch.setattr(handlers, "check_requirements", lambda: True)
    receive = Mock()
    monkeypatch.setattr(handlers, "_handle_notify", receive)
    handlers.notify()
    receive.assert_called_once_with(payload)


def test_previous_callback_runs_when_tracing_disabled(monkeypatch):
    run = Mock()
    monkeypatch.setattr("tracing.codex.notify_chain.subprocess.run", run)
    monkeypatch.setattr(sys, "argv", [HOOK, "--previous-notify", '["/other"]', "{}"])
    monkeypatch.setattr(handlers, "load_env_file", lambda _: None)
    monkeypatch.setattr(handlers, "check_requirements", lambda: False)
    handlers.notify()
    assert run.call_args.args[0] == ["/other", "{}"]
