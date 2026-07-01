"""Tests for core/config.py — pure functions and CLI main()."""

import io
import json
import os
import stat
from pathlib import Path

import pytest

from core.config import _format_output, _parse_value, delete_value, get_value, load_config, main, save_config, set_value


@pytest.fixture(autouse=True)
def _mock_sleep(monkeypatch):
    sleep_calls = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))
    return sleep_calls


# ---------------------------------------------------------------------------
# Pure function tests
# ---------------------------------------------------------------------------


class TestParseValue:
    def test_true(self):
        assert _parse_value("true") is True

    def test_true_mixed_case(self):
        assert _parse_value("True") is True

    def test_false(self):
        assert _parse_value("false") is False

    def test_false_mixed_case(self):
        assert _parse_value("FALSE") is False

    def test_integer(self):
        assert _parse_value("42") == 42

    def test_negative_integer(self):
        assert _parse_value("-7") == -7

    def test_string(self):
        assert _parse_value("hello") == "hello"

    def test_float_stays_string(self):
        assert _parse_value("3.14") == "3.14"


class TestFormatOutput:
    def test_none(self):
        assert _format_output(None) == ""

    def test_dict(self):
        assert _format_output({"a": 1}) == '{"a": 1}'

    def test_list(self):
        assert _format_output([1, 2]) == "[1, 2]"

    def test_bool_true(self):
        assert _format_output(True) == "true"

    def test_bool_false(self):
        assert _format_output(False) == "false"

    def test_int(self):
        assert _format_output(42) == "42"

    def test_string(self):
        assert _format_output("hello") == "hello"


class TestGetValue:
    def test_simple_key(self):
        assert get_value({"a": 1}, "a") == 1

    def test_nested_key(self):
        cfg = {"backend": {"target": "phoenix"}}
        assert get_value(cfg, "backend.target") == "phoenix"

    def test_missing_key(self):
        assert get_value({"a": 1}, "b") is None

    def test_missing_nested_key(self):
        assert get_value({"a": {"b": 1}}, "a.c") is None

    def test_partial_path_non_dict(self):
        assert get_value({"a": "scalar"}, "a.b") is None


class TestSetValue:
    def test_creates_nested_path(self):
        cfg = {}
        set_value(cfg, "a.b.c", 42)
        assert cfg == {"a": {"b": {"c": 42}}}

    def test_overwrites_existing(self):
        cfg = {"a": {"b": 1}}
        set_value(cfg, "a.b", 2)
        assert cfg["a"]["b"] == 2

    def test_creates_intermediate_dicts(self):
        cfg = {"a": "old"}
        set_value(cfg, "a.b", 1)
        assert cfg == {"a": {"b": 1}}

    def test_returns_config(self):
        cfg = {}
        result = set_value(cfg, "x", 1)
        assert result is cfg


class TestDeleteValue:
    def test_removes_key(self):
        cfg = {"a": 1, "b": 2}
        delete_value(cfg, "a")
        assert cfg == {"b": 2}

    def test_removes_nested_key(self):
        cfg = {"a": {"b": 1, "c": 2}}
        delete_value(cfg, "a.b")
        assert cfg == {"a": {"c": 2}}

    def test_missing_key_noop(self):
        cfg = {"a": 1}
        delete_value(cfg, "b")
        assert cfg == {"a": 1}

    def test_non_dict_parent(self):
        cfg = {"a": "scalar"}
        delete_value(cfg, "a.b")
        assert cfg == {"a": "scalar"}

    def test_returns_config(self):
        cfg = {"a": 1}
        result = delete_value(cfg, "a")
        assert result is cfg


# ---------------------------------------------------------------------------
# save_config / load_config
# ---------------------------------------------------------------------------


class TestSaveConfig:
    def test_writes_valid_json(self, tmp_path):
        path = tmp_path / "config.json"
        save_config({"a": 1}, config_path=str(path))
        data = json.loads(path.read_text())
        assert data == {"a": 1}

    def test_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "sub" / "dir" / "config.json"
        save_config({"x": "y"}, config_path=str(path))
        assert path.exists()

    def test_permissions_600(self, tmp_path):
        path = tmp_path / "config.json"
        save_config({}, config_path=str(path))
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600


class TestLoadConfig:
    def test_missing_file_returns_empty(self, tmp_path):
        assert load_config(str(tmp_path / "nope.json")) == {}

    def test_empty_file_returns_empty(self, tmp_path):
        path = tmp_path / "empty.json"
        path.write_text("")
        assert load_config(str(path)) == {}

    def test_null_file_returns_empty(self, tmp_path):
        path = tmp_path / "null.json"
        path.write_text("null")
        assert load_config(str(path)) == {}

    def test_loads_valid_json(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"a": 1}, indent=2))
        assert load_config(str(path)) == {"a": 1}

    def test_non_mapping_raises(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text('["item1", "item2"]')
        with pytest.raises(ValueError, match="not a JSON mapping"):
            load_config(str(path))

    def test_malformed_json_raises(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not valid json")
        with pytest.raises(ValueError, match="Malformed JSON"):
            load_config(str(path))


# ---------------------------------------------------------------------------
# CLI main() tests
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_config(tmp_harness_dir, sample_config, monkeypatch):
    """Patch core.config.CONFIG_FILE to the tmp harness config path."""
    config_path = str(tmp_harness_dir / "config.json")
    monkeypatch.setattr("core.config.CONFIG_FILE", config_path)
    return config_path


class TestMainGet:
    def test_get_existing_key(self, cli_config, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["config.py", "get", "harnesses.claude-code.target"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0
        assert capsys.readouterr().out.strip() == "phoenix"

    def test_get_nonexistent_key(self, cli_config, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["config.py", "get", "nonexistent.key"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0
        assert capsys.readouterr().out.strip() == ""

    def test_get_missing_arg(self, cli_config, monkeypatch):
        monkeypatch.setattr("sys.argv", ["config.py", "get"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1


class TestMainSet:
    def test_set_value(self, cli_config, monkeypatch):
        monkeypatch.setattr("sys.argv", ["config.py", "set", "harnesses.codex.collector.port", "9999"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0
        data = json.loads(Path(cli_config).read_text())
        assert data["harnesses"]["codex"]["collector"]["port"] == 9999

    def test_set_missing_args(self, cli_config, monkeypatch):
        monkeypatch.setattr("sys.argv", ["config.py", "set", "key"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1


class TestMainDelete:
    def test_delete_key(self, cli_config, monkeypatch):
        monkeypatch.setattr("sys.argv", ["config.py", "delete", "harnesses.codex"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0
        data = json.loads(Path(cli_config).read_text())
        assert "codex" not in data["harnesses"]

    def test_delete_missing_arg(self, cli_config, monkeypatch):
        monkeypatch.setattr("sys.argv", ["config.py", "delete"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1


class TestMainDump:
    def test_dump_prints_json(self, cli_config, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["config.py", "dump"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["harnesses"]["claude-code"]["target"] == "phoenix"


class TestMainExists:
    def test_exists_when_present(self, cli_config, monkeypatch):
        monkeypatch.setattr("sys.argv", ["config.py", "exists"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0

    def test_exists_when_missing(self, tmp_harness_dir, monkeypatch):
        missing = str(tmp_harness_dir / "no_such_config.json")
        monkeypatch.setattr("core.config.CONFIG_FILE", missing)
        monkeypatch.setattr("sys.argv", ["config.py", "exists"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1


class TestMainWrite:
    def test_write_from_stdin(self, cli_config, monkeypatch):
        input_json = json.dumps({"new_key": "new_value"}, indent=2)
        monkeypatch.setattr("sys.stdin", io.StringIO(input_json))
        monkeypatch.setattr("sys.argv", ["config.py", "write"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0
        data = json.loads(Path(cli_config).read_text())
        assert data == {"new_key": "new_value"}

    def test_write_empty_stdin(self, cli_config, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        monkeypatch.setattr("sys.argv", ["config.py", "write"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0
        data = json.loads(Path(cli_config).read_text())
        assert data == {}

    def test_write_non_mapping_fails(self, cli_config, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO('["item"]'))
        monkeypatch.setattr("sys.argv", ["config.py", "write"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1


class TestMainEdgeCases:
    def test_no_args(self, cli_config, monkeypatch):
        monkeypatch.setattr("sys.argv", ["config.py"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1

    def test_unknown_command(self, cli_config, monkeypatch):
        monkeypatch.setattr("sys.argv", ["config.py", "bogus"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1
