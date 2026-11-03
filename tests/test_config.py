import dataclasses
import json
import logging
import sys
from pathlib import Path

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
