import importlib
import json
from pathlib import Path

import pytest

from core.setup import prompt_project_name

INSTALLERS = (
    ("tracing.antigravity.install", "antigravity", ("_install_hooks",), {}),
    ("tracing.claude_code.install", "claude-code", ("_register_claude_hooks",), {}),
    (
        "tracing.codex.install",
        "codex",
        ("cleanup_legacy_install", "_write_env_file", "_codex_toml_apply"),
        {},
    ),
    ("tracing.copilot.install", "copilot", ("_install_hooks",), {}),
    ("tracing.cursor.install", "cursor", ("_register_cursor_hooks",), {}),
    ("tracing.devin.install", "devin", ("_register_hooks",), {}),
    ("tracing.gemini.install", "gemini", ("_install_hooks",), {}),
    ("tracing.kiro.install", "kiro", ("_register_kiro_hooks", "_maybe_set_default"), {"agent_name": "test"}),
    ("tracing.omp.install", "omp", ("_install_plugin", "_register_extension"), {}),
    ("tracing.opencode.install", "opencode", ("_install_plugin",), {}),
)


@pytest.mark.parametrize(("module_name", "harness_name", "side_effects", "install_kwargs"), INSTALLERS)
def test_ax_project_default_is_persisted_and_reinstall_is_preserved(
    tmp_path, monkeypatch, module_name, harness_name, side_effects, install_kwargs
):
    module = importlib.import_module(module_name)
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps(
            {
                "user_id": "alice@example.com",
                "harnesses": {"other": {"project_name": "keep", "target": "phoenix"}},
                "logging": {},
            }
        )
    )

    monkeypatch.setattr("core.config.CONFIG_FILE", str(config_file))
    monkeypatch.setattr("core.setup.CONFIG_FILE", config_file)
    monkeypatch.setattr(module, "prompt_project_name", prompt_project_name)
    monkeypatch.setattr(module, "prompt_user_id", lambda: "")
    monkeypatch.setattr(
        module,
        "prompt_backend",
        lambda *args, **kwargs: (
            "arize",
            {"endpoint": "otlp.arize.com:443", "api_key": "key", "space_id": "space"},
        ),
    )
    monkeypatch.setattr(module, "ensure_shared_runtime", lambda: None)
    monkeypatch.setattr(module, "dry_run", lambda: False)
    if hasattr(module, "ensure_harness_installed"):
        monkeypatch.setattr(module, "ensure_harness_installed", lambda *args, **kwargs: True)
    if hasattr(module, "INSTALL_DIR"):
        monkeypatch.setattr(module, "INSTALL_DIR", tmp_path / "install")
    if module_name == "tracing.codex.install":
        monkeypatch.setattr(module, "CONFIG_FILE", config_file)
        monkeypatch.setattr(module, "get_codex_home", lambda: tmp_path / ".codex")
        monkeypatch.setattr(module, "venv_bin", lambda name: Path("/tmp") / name)
    for name in side_effects:
        monkeypatch.setattr(module, name, lambda *args, **kwargs: None)
    monkeypatch.setenv("ARIZE_NONINTERACTIVE", "1")
    monkeypatch.setenv("ARIZE_PROJECT_NAME", "inherited-project")
    monkeypatch.delenv("ARIZE_USER_ID", raising=False)

    module.install(**install_kwargs)

    config = json.loads(config_file.read_text())
    assert config["harnesses"][harness_name]["project_name"] == "harness/alice@example.com"
    assert config["harnesses"]["other"] == {"project_name": "keep", "target": "phoenix"}
    assert config["user_id"] == "alice@example.com"

    config["harnesses"][harness_name]["project_name"] = "saved-project"
    config_file.write_text(json.dumps(config))
    module.install(**install_kwargs)

    reinstalled = json.loads(config_file.read_text())
    assert reinstalled["harnesses"][harness_name]["project_name"] == "saved-project"
