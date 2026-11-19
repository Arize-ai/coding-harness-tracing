"""A system-wide uv tool install: a read-only tool folder outside every user's HOME (#95898)."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import sysconfig
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import dotenv
import json5
import pytest

from core.constants import HARNESSES
from core.hook_table import canonical_harness
from tests._pyproject import REPO_ROOT
from tests.core.test_cli_commands import ARIZE_CREDS, _env, _home

pytestmark = pytest.mark.skipif(os.name == "nt", reason="no system-wide install on Windows")

CREDS = ARIZE_CREDS + "ARIZE_BACKEND=arize\n"
SPAN_LINE = "[dry-run] would send span"
_COMMAND_PATH = re.compile(r"(/[^\s\"'\\]*arize-harness)(?![\w.-])")


# ---------------------------------------------------------------------------
# The system install
# ---------------------------------------------------------------------------


def _tree(root: Path) -> Dict[str, Tuple[int, bytes]]:
    """``{path: (mode, bytes)}`` for every file and link under ``root``."""
    out: Dict[str, Tuple[int, bytes]] = {}
    for path in sorted(root.rglob("*")):
        st = path.lstat()
        if path.is_symlink():
            out[str(path)] = (st.st_mode, os.readlink(path).encode())
        elif path.is_file():
            out[str(path)] = (st.st_mode, path.read_bytes())
        else:
            out[str(path)] = (st.st_mode, b"")
    return out


def _set_writable(root: Path, writable: bool) -> None:
    write = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            continue
        mode = path.stat().st_mode
        path.chmod((mode | stat.S_IWUSR) if writable else (mode & ~write))


@pytest.fixture(scope="module")
def system(tmp_path_factory):
    """``(root, link, tools)``: a uv-tool-style env under ``root/opt``, linked from ``root/usr/local/bin``."""
    root = tmp_path_factory.mktemp("machine")
    tools = root / "opt" / "arize" / "tools"
    prefix = tools / "coding-harness-tracing"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(prefix)], check=True)
    site = Path(sysconfig.get_path("purelib", vars={"base": str(prefix), "platbase": str(prefix)}))
    for package in ("core", "tracing"):
        shutil.copytree(REPO_ROOT / package, site / package, ignore=shutil.ignore_patterns("__pycache__"))
    # Runtime dependencies from this environment, after the copied package on sys.path.
    deps = {str(Path(dotenv.__file__).parent.parent), str(Path(json5.__file__).parent.parent)}
    (site / "_deps.pth").write_text("\n".join(sorted(deps)) + "\n")
    (prefix / "uv-receipt.toml").write_text('[tool]\nrequirements = [{ name = "coding-harness-tracing" }]\n')
    script = prefix / "bin" / "arize-harness"
    script.write_text(f"#!{prefix / 'bin' / 'python'}\nimport sys\nfrom core.cli import main\nsys.exit(main())\n")
    script.chmod(0o755)
    link = root / "usr" / "local" / "bin" / "arize-harness"
    link.parent.mkdir(parents=True)
    link.symlink_to(script)
    (root / "tmp").mkdir()
    _set_writable(root / "opt", False)
    yield root, link, tools
    _set_writable(root / "opt", True)


def _user(root: Path, name: str) -> Tuple[Path, Path]:
    """A fresh user HOME under ``root/users`` and the project folder setup runs in."""
    home = _home(root / "users", f"{name}-{uuid.uuid4().hex[:8]}")
    for sub in (".cursor", ".gemini", ".kiro", ".config/devin"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    (home / "creds.env").write_text(CREDS)
    project = home / "project"
    project.mkdir()
    return home, project


def _system_env(root: Path, home: Path, **extra: str) -> Dict[str, str]:
    env = _env(home, ARIZE_ENV_FILE=str(home / "creds.env"), **extra)
    env.pop("PYTHONPATH", None)  # run the system install's package, not this checkout
    tmp = str(root / "tmp")
    env.update(TMPDIR=tmp, TEMP=tmp, TMP=tmp, PATH=f"{root / 'usr' / 'local' / 'bin'}{os.pathsep}{env['PATH']}")
    return env


def _run(root: Path, home: Path, cwd: Path, args: List[str], stdin: str = "", **extra: str):
    return subprocess.run(
        args,
        input=stdin,
        capture_output=True,
        text=True,
        cwd=cwd,
        env=_system_env(root, home, **extra),
        timeout=120,
        check=False,
        start_new_session=True,
    )


def _snapshot(*roots: Path) -> Dict[str, Tuple[int, bytes]]:
    """``{path: (mtime_ns, bytes)}`` for setup's files, logs left out."""
    out: Dict[str, Tuple[int, bytes]] = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.is_symlink() and "logs" not in path.parts:
                out[str(path)] = (path.stat().st_mtime_ns, path.read_bytes())
    return out


def _command_paths(*roots: Path) -> set:
    found = set()
    for root in roots:
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in (".json", ".toml", ".ts", ".sh", ""):
                found.update(_COMMAND_PATH.findall(path.read_text(errors="ignore")))
    return found


# ---------------------------------------------------------------------------
# Dry-run hook turns, one per harness
# ---------------------------------------------------------------------------

_OPENCODE_MESSAGES = [
    {
        "info": {"id": "msg_u1", "sessionID": "{session}", "role": "user", "time": {"created": 1700000000000}},
        "parts": [{"id": "prt_u1", "sessionID": "{session}", "messageID": "msg_u1", "type": "text", "text": "hi"}],
    },
    {
        "info": {
            "id": "msg_a1",
            "sessionID": "{session}",
            "role": "assistant",
            "parentID": "msg_u1",
            "modelID": "m1",
            "providerID": "p",
            "time": {"created": 1700000000100, "completed": 1700000000200},
            "tokens": {"input": 1, "output": 1, "cache": {}},
        },
        "parts": [{"id": "prt_a1", "sessionID": "{session}", "messageID": "msg_a1", "type": "text", "text": "hello"}],
    },
]

TURNS: Dict[str, List[Tuple[Optional[str], dict]]] = {
    "claude": [
        ("user-prompt-submit", {"session_id": "{session}", "hook_event_name": "UserPromptSubmit", "prompt": "hi"}),
        ("notification", {"session_id": "{session}", "hook_event_name": "Notification", "message": "needs input"}),
    ],
    "codex": [
        (
            "notify",
            {
                "type": "agent-turn-complete",
                "thread-id": "{session}",
                "turn-id": "turn-1",
                "cwd": "{cwd}",
                "input-messages": ["hi"],
                "last-assistant-message": "hello",
            },
        )
    ],
    "copilot": [
        ("user-prompt", {"session_id": "{session}", "prompt": "hi", "cwd": "{cwd}"}),
        ("stop", {"session_id": "{session}", "stop_reason": "end_turn", "cwd": "{cwd}"}),
    ],
    "cursor": [
        (
            None,
            {
                "hook_event_name": "afterAgentThought",
                "conversation_id": "{session}",
                "generation_id": "gen-1",
                "text": "thinking",
                "workspace_roots": ["{cwd}"],
            },
        )
    ],
    "devin": [(None, {"hook_event_name": "Stop", "session_id": "{session}", "cwd": "{cwd}"})],
    "gemini": [
        ("before-agent", {"session_id": "{session}", "hook_event_name": "BeforeAgent", "prompt": "hi"}),
        (
            "after-agent",
            {"session_id": "{session}", "hook_event_name": "AfterAgent", "prompt": "hi", "prompt_response": "hello"},
        ),
    ],
    "kiro": [(None, {"hook_event_name": "stop", "session_id": "{session}", "assistant_response": "done"})],
    "omp": [
        (
            None,
            {
                "type": "turn_end",
                "sessionId": "{session}",
                "cwd": "{cwd}",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hello"}],
                    "model": "m1",
                    "provider": "p",
                },
                "toolResults": [],
            },
        )
    ],
    "opencode": [
        (None, {"type": "close", "sessionID": "{session}", "directory": "{cwd}", "messages": _OPENCODE_MESSAGES})
    ],
    "antigravity": [
        ("stop", {"conversationId": "{session}", "transcriptPath": "{cwd}/transcript.jsonl", "cwd": "{cwd}"}),
    ],
}


def _fill(value, **subs: str):
    if isinstance(value, str):
        for key, sub in subs.items():
            value = value.replace("{" + key + "}", sub)
        return value
    if isinstance(value, list):
        return [_fill(v, **subs) for v in value]
    if isinstance(value, dict):
        return {k: _fill(v, **subs) for k, v in value.items()}
    return value


def _prepare_turn(name: str, home: Path, cwd: Path, session: str) -> Dict[str, str]:
    """Write what a harness's hook reads besides its payload; returns extra env."""
    if name == "antigravity":
        records = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE"},
            {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": "hello"},
        ]
        records[0].update(created_at="2026-06-09T16:00:06Z", content="<USER_REQUEST>\nhi\n</USER_REQUEST>")
        records[1]["created_at"] = "2026-06-09T16:00:07Z"
        (cwd / "transcript.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    if name == "devin":
        db = home / ".local" / "share" / "devin" / "cli" / "sessions.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(db)
        con.execute(
            "CREATE TABLE sessions (id TEXT, working_directory TEXT, backend_type TEXT, "
            "model TEXT, last_activity_at INTEGER, main_chain_id INTEGER)"
        )
        con.execute(
            "CREATE TABLE message_nodes (row_id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
            "node_id INTEGER, parent_node_id INTEGER, chat_message TEXT, created_at INTEGER, metadata TEXT)"
        )
        con.execute(
            "CREATE TABLE prompt_history (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT, "
            "timestamp INTEGER, session_id TEXT, is_shell INTEGER DEFAULT 0)"
        )
        con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?)", (session, str(cwd), "b", "m1", 1, 1))
        message = {
            "role": "assistant",
            "content": "hello",
            "metadata": {
                "request_id": "req-1",
                "generation_model": "m1",
                "metrics": {"input_tokens": 1, "output_tokens": 1},
            },
        }
        con.execute(
            "INSERT INTO message_nodes (session_id, node_id, chat_message, created_at) VALUES (?,?,?,0)",
            (session, 1, json.dumps(message)),
        )
        con.execute("INSERT INTO prompt_history (content, timestamp, session_id) VALUES ('hi', 1, ?)", (session,))
        con.commit()
        con.close()
        return {"DEVIN_PROJECT_DIR": str(cwd)}
    return {}


def _fire_turn(root: Path, link: Path, home: Path, cwd: Path, name: str) -> Path:
    """Run ``name``'s dry-run turn through ``link`` and return its log file."""
    log = home / ".arize" / "harness" / "logs" / f"{name}.log"
    name = canonical_harness(name)
    session = f"system-{uuid.uuid4().hex}"
    extra = _prepare_turn(name, home, cwd, session)
    extra.update(ARIZE_DRY_RUN="true", ARIZE_VERBOSE="true", ARIZE_DISABLE_FORK="true")
    for event, payload in TURNS[name]:
        data = json.dumps(_fill(payload, session=session, cwd=str(cwd)))
        args = [str(link), "hook", name, *([event] if event else [])]
        if name == "codex":
            args.append(data)
        result = _run(root, home, cwd, args, stdin="" if name == "codex" else data, **extra)
        assert result.returncode == 0, f"{args}: {result.stdout}{result.stderr}"
    return log


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(HARNESSES))
def test_setup_hooks_and_status_from_the_system_install(system, name):
    root, link, tools = system
    home, project = _user(root, "alice")
    system_before = _tree(root / "opt")

    first = _run(root, home, project, [str(link), "setup", name, "--non-interactive"])
    assert first.returncode == 0, first.stdout + first.stderr
    paths = _command_paths(home, project)
    assert paths == {str(link)}, paths
    after_one = _snapshot(home)

    second = _run(root, home, project, [str(link), "setup", name, "--non-interactive"])
    assert second.returncode == 0, second.stdout + second.stderr
    assert _snapshot(home) == after_one

    log = _fire_turn(root, link, home, project, name)
    assert SPAN_LINE in log.read_text(), log.read_text()

    status = _run(root, home, project, [str(link), "status", "--json"])
    assert status.returncode == 0, status.stdout + status.stderr
    report = json.loads(status.stdout)
    assert report["arize_harness"] == str(link)
    assert all(h["registered"] for h in report["harnesses"]), report

    assert _tree(root / "opt") == system_before


def test_uninstall_all_leaves_other_users_and_the_system_install(system):
    root, link, _tools = system
    alice, alice_project = _user(root, "alice")
    bob, bob_project = _user(root, "bob")
    for home, project in ((alice, alice_project), (bob, bob_project)):
        for name in ("claude", "codex"):
            result = _run(root, home, project, [str(link), "setup", name, "-y"])
            assert result.returncode == 0, result.stdout + result.stderr
    bob_before = _snapshot(bob)
    system_before = _tree(root / "opt")

    result = _run(root, alice, alice_project, [str(link), "uninstall", "--all"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (alice / ".arize" / "harness").exists()
    assert str(link) not in (alice / ".claude" / "settings.json").read_text()
    assert _snapshot(bob) == bob_before
    assert _tree(root / "opt") == system_before
    assert link.exists()


def test_update_prints_the_root_upgrade_command_and_changes_nothing(system):
    root, link, tools = system
    home, project = _user(root, "alice")
    setup = _run(root, home, project, [str(link), "setup", "claude", "-y"])
    assert setup.returncode == 0, setup.stdout + setup.stderr
    home_before, system_before = _snapshot(home), _tree(root / "opt")

    result = _run(root, home, project, [str(link), "update"])
    assert result.returncode == 1
    assert result.stdout == ""
    line = result.stderr.strip()
    assert "system-wide uv tool install" in line and "as root" in line
    assert f"`sudo sh -c 'umask 022 && UV_TOOL_DIR={tools} UV_TOOL_BIN_DIR={link.parent} " in line
    assert " UV_COMPILE_BYTECODE=1 uv tool upgrade coding-harness-tracing'`" in line
    assert line.endswith("Nothing was changed.")
    assert _snapshot(home) == home_before
    assert _tree(root / "opt") == system_before


def test_interactive_setup_registers_only_the_system_path(system):
    root, link, tools = system
    home, project = _user(root, "alice")
    saved = _run(root, home, project, [str(link), "setup", "claude", "-y"])
    assert saved.returncode == 0, saved.stdout + saved.stderr
    (home / ".claude" / "settings.json").unlink()
    system_before = _tree(root / "opt")

    result = _run(root, home, project, [str(link), "setup", "claude"], stdin="\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _command_paths(home, project) == {str(link)}
    assert _tree(root / "opt") == system_before
