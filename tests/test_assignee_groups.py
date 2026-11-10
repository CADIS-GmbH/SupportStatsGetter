import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from SupportStatsGetter import (
    Config,
    SupportStatsGetter,
    make_jira_ticket_calendar,
    parse_labels,
)

GROUPS = ["1stLevel", "2ndLevel", "product-team"]


def label_change(when, frm, to):
    return {"created": when, "items": [{"field": "labels", "fromString": frm, "toString": to}]}


def issue(labels):
    return {
        "id": "1",
        "key": "SUP-1",
        "created": datetime.datetime(2026, 10, 5, 9, 0),
        "resolved": datetime.datetime(2026, 10, 7, 9, 0),
        "fields": {"status": {"name": "Resolved"}, "labels": labels},
    }


def groups(tmp_path, labels, changelog, **config):
    SupportStatsGetter(Config(db_path=tmp_path / "test.db", **config))
    rows = make_jira_ticket_calendar(issue(labels), [], [], changelog)
    return [(r["start_assigned_group"], r["end_assigned_group"]) for r in rows]


def test_parse_labels():
    assert parse_labels(["a", " b "]) == ["a", "b"]
    assert parse_labels("a b c") == ["a b c", "a", "b", "c"]
    assert parse_labels("Cadis - 1st Level,2ndLevel") == [
        "Cadis - 1st Level", "2ndLevel", "Cadis", "-", "1st", "Level"
    ]
    assert parse_labels(None) == []


def test_group_follows_label_changes(tmp_path):
    changelog = [
        # created with 1stLevel, moved to 2ndLevel on day 2
        label_change(datetime.datetime(2026, 10, 6, 10, 0), "1stLevel", "2ndLevel customer-x"),
    ]
    assert groups(
        tmp_path, ["2ndLevel", "customer-x"], changelog, assignee_groups=GROUPS
    ) == [
        ("1stLevel", "1stLevel"),
        ("1stLevel", "2ndLevel"),
        ("2ndLevel", "2ndLevel"),
    ]


def test_first_configured_group_wins_and_case_is_ignored(tmp_path):
    assert groups(
        tmp_path, ["product-team", "2ndlevel"], [], assignee_groups=GROUPS
    )[0] == ("2ndLevel", "2ndLevel")


def test_default_group_without_label(tmp_path):
    assert groups(
        tmp_path,
        ["customer-x"],
        [],
        assignee_groups=GROUPS,
        assignee_group_default="1stLevel",
    )[0] == ("1stLevel", "1stLevel")
    assert groups(tmp_path, ["customer-x"], [], assignee_groups=GROUPS)[0] == (None, None)


def test_no_groups_configured(tmp_path):
    assert groups(tmp_path, ["1stLevel"], [], assignee_group_default="1stLevel")[0] == (
        None,
        None,
    )


def test_config_loads_groups(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "assignee_groups:\n  - 1stLevel\n  - 2ndLevel\nassignee_group_default: 1stLevel\n",
        encoding="utf-8",
    )
    config = Config.load(path)
    assert config.assignee_groups == ["1stLevel", "2ndLevel"]
    assert config.assignee_group_default == "1stLevel"
