import dataclasses
import json
import logging
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from SupportStatsGetter import Config

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yaml"


def test_example_sets_every_option():
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    options = {f.name for f in dataclasses.fields(Config)}
    assert set(data) == options


def test_example_loads(caplog):
    with caplog.at_level(logging.WARNING):
        config = Config.load(EXAMPLE)
    assert not caplog.records
    assert config.excel_write_mode == "overwrite_with_backup"
    assert config.request_type_categories is None
    assert config.priority_positions["High"] == 100
    assert config.dashboard_start_quarter is None


def test_yaml_and_json_give_same_config(tmp_path):
    data = {
        "db_path": "C:\\Stats\\support.sqlite",
        "output_excel": "out.xlsx",
        "support_issue_types": ["Incident"],
        "priority_positions": {"Urgent": 300},
    }
    yaml_path = tmp_path / "config.yaml"
    json_path = tmp_path / "config.json"
    yaml_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    json_path.write_text(json.dumps(data), encoding="utf-8")
    assert Config.load(yaml_path) == Config.load(json_path)
    assert Config.load(yaml_path).db_path == Path("C:\\Stats\\support.sqlite")


@pytest.fixture(autouse=True)
def no_credentials_in_environment(monkeypatch):
    # the tests mustn't see the credentials saved for the Windows user
    for name in ("SUPPORTSTATS_JIRA_USER", "SUPPORTSTATS_JIRA_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def test_empty_yaml_uses_defaults(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text("# nothing set\n", encoding="utf-8")
    assert Config.load(path) == Config()


def test_list_option_without_entries_is_empty(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("non_support_request_types:\nsupport_issue_types:\n", encoding="utf-8")
    config = Config.load(path)
    assert config.non_support_request_types == []
    assert config.support_issue_types == []


def test_unknown_option_is_reported(tmp_path, caplog):
    path = tmp_path / "config.yaml"
    path.write_text("log_levle: DEBUG\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        Config.load(path)
    assert "log_levle" in caplog.text


def test_exe_config_keeps_files_in_the_exe_folder():
    sys.path.insert(0, str(EXAMPLE.parent / "exe"))
    from make_exe_config import make_exe_config

    data = yaml.safe_load(make_exe_config(EXAMPLE.read_text(encoding="utf-8")))
    assert data["db_path"] == "support_stats.sqlite"
    assert data["log_file"] == "support_stats.log"
    assert data["output_excel"] == r"reports\SupportStats.xlsx"
    # run.bat asks for the credentials and stores them in environment variables
    assert data["jira_user"] is None and data["jira_token"] is None
    # every other option is the same as in the example
    example = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    changed = ("db_path", "log_file", "output_excel", "jira_user", "jira_token")
    assert {k: v for k, v in data.items() if k not in changed} == {
        k: v for k, v in example.items() if k not in changed
    }


def test_credentials_fall_back_to_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPPORTSTATS_JIRA_USER", "env@example.com")
    monkeypatch.setenv("SUPPORTSTATS_JIRA_TOKEN", "env-token")
    path = tmp_path / "config.yaml"
    path.write_text("jira_user: null\n", encoding="utf-8")
    config = Config.load(path)
    assert (config.jira_user, config.jira_token) == ("env@example.com", "env-token")


def test_config_credentials_win_over_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPPORTSTATS_JIRA_USER", "env@example.com")
    monkeypatch.setenv("SUPPORTSTATS_JIRA_TOKEN", "env-token")
    path = tmp_path / "config.yaml"
    path.write_text("jira_user: me@example.com\njira_token: file-token\n", encoding="utf-8")
    config = Config.load(path)
    assert (config.jira_user, config.jira_token) == ("me@example.com", "file-token")


def test_missing_credentials_are_reported(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("SUPPORTSTATS_JIRA_USER", raising=False)
    monkeypatch.delenv("SUPPORTSTATS_JIRA_TOKEN", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text(
        "jira_base_url: https://x.atlassian.net\njira_token: ''\n", encoding="utf-8"
    )
    with caplog.at_level(logging.WARNING):
        config = Config.load(path)
    assert config.jira_token is None
    assert "SUPPORTSTATS_JIRA_TOKEN" in caplog.text


def test_no_credential_warning_without_jira_site(tmp_path, monkeypatch, caplog):
    # an export-only config (no jira_base_url) needs no credentials
    monkeypatch.delenv("SUPPORTSTATS_JIRA_USER", raising=False)
    monkeypatch.delenv("SUPPORTSTATS_JIRA_TOKEN", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text("db_path: 'x.sqlite'\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        config = Config.load(path)
    assert (config.jira_user, config.jira_token) == (None, None)
    assert not caplog.records


@pytest.mark.parametrize(
    "config, env, code",
    [
        ("jira_user: me@example.com\njira_token: t\n", {}, 0),
        ("jira_user: null\njira_token: null\n", {}, 24),
        ("jira_user: me@example.com\njira_token: null\n", {}, 16),
        ("jira_user: null\njira_token: t\n", {}, 8),
        (
            "jira_user: null\njira_token: null\n",
            {"SUPPORTSTATS_JIRA_USER": "u", "SUPPORTSTATS_JIRA_TOKEN": "t"},
            0,
        ),
    ],
)
def test_missing_credentials_exit_code(tmp_path, config, env, code):
    import os
    import subprocess

    path = tmp_path / "config.yaml"
    path.write_text(config, encoding="utf-8")
    environ = {
        k: v for k, v in os.environ.items() if not k.startswith("SUPPORTSTATS_")
    } | env
    result = subprocess.run(
        [sys.executable, str(EXAMPLE.parent / "SupportStatsGetter.py"),
         "-c", str(path), "--missing-credentials"],
        env=environ, capture_output=True, cwd=tmp_path,
    )
    assert result.returncode == code


def run_script(tmp_path, config):
    import os
    import subprocess

    path = tmp_path / "config.yaml"
    path.write_text(config, encoding="utf-8")
    environ = {k: v for k, v in os.environ.items() if not k.startswith("SUPPORTSTATS_")}
    return subprocess.run(
        [sys.executable, str(EXAMPLE.parent / "SupportStatsGetter.py"), "-c", str(path)],
        env=environ, capture_output=True, text=True, cwd=tmp_path,
    )


def test_missing_data_folder_gives_clear_error(tmp_path):
    result = run_script(
        tmp_path,
        "db_path: '..\\missing_data\\db.sqlite'\n"
        "output_excel: 'missing_reports\\out.xlsx'\n",
    )
    assert result.returncode == 1
    assert "The folder for db_path does not exist" in result.stderr
    assert str((tmp_path.parent / "missing_data").resolve()) in result.stderr
    assert "The folder for output_excel does not exist" in result.stderr
    assert not (tmp_path.parent / "missing_data").exists()


def test_missing_log_folder_gives_clear_error(tmp_path):
    result = run_script(tmp_path, "log_file: 'missing_logs/run.log'\n")
    assert result.returncode == 1
    assert "The folder for log_file does not exist" in result.stderr
