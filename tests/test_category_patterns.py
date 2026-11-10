import datetime
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from SupportStatsGetter import Config, SupportStatsGetter


def make_getter(tmp_path, categories, default="Other", support_issue_types=()):
    config = Config(
        db_path=tmp_path / "test.db",
        support_issue_types=list(support_issue_types),
        request_type_categories=categories,
        request_type_category_default=default,
    )
    getter = SupportStatsGetter(config)
    day = datetime.datetime(2025, 3, 7)
    tickets = [
        ("T-inc-hw", "Incident", "Hardware broken"),
        ("T-inc-sw", "Incident", "Software question"),
        ("T-sr-hw", "Service Request", "Hardware order"),
        ("T-sr-sw", "Service Request", "Software license"),
    ]
    getter.store.insert_many(
        getter.store.jira_tickets,
        [
            {
                "id": ticket_id,
                "key": ticket_id,
                "status": "Open",
                "created": day,
                "updated": day,
                "created_week": "2025.10",
                "active_weeks": "2025.10",
                "issuetype": issuetype,
                "request_type": request_type,
            }
            for ticket_id, issuetype, request_type in tickets
        ],
    )
    return getter


def categories_by_ticket(getter):
    with getter.store.engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id, request_type_category FROM jira_tickets_reporting")
        )
        return dict(rows.all())


def test_mapping_patterns_are_and_connected(tmp_path):
    getter = make_getter(
        tmp_path,
        {
            "Incident HW": [{"issue_type": "incident", "request_type": "%Hardware%"}],
            "Incident": [{"issue_type": "Incident"}],
            "HW": ["%Hardware%"],
        },
    )
    assert categories_by_ticket(getter) == {
        "T-inc-hw": "Incident HW",
        "T-inc-sw": "Incident",
        "T-sr-hw": "HW",
        "T-sr-sw": "Other",
    }
    with getter.store.engine.connect() as conn:
        row = conn.execute(
            text("SELECT * FROM weekly_support_summary WHERE week = '2025.10'")
        ).mappings().one()
    assert row["open_incident_hw"] == 1
    assert row["open_incident"] == 1
    assert row["open_hw"] == 1
    assert row["open_other"] == 1


def test_default_is_one_category_per_support_issue_type(tmp_path):
    getter = make_getter(
        tmp_path, None, default=None, support_issue_types=["Incident", "Service Request"]
    )
    assert categories_by_ticket(getter) == {
        "T-inc-hw": "Incident",
        "T-inc-sw": "Incident",
        "T-sr-hw": "Service Request",
        "T-sr-sw": "Service Request",
    }
    with getter.store.engine.connect() as conn:
        row = conn.execute(
            text("SELECT * FROM weekly_support_summary WHERE week = '2025.10'")
        ).mappings().one()
    assert row["open_incident"] == 2
    assert row["open_service_request"] == 2


def test_default_uses_stored_issue_types_without_support_issue_types(tmp_path):
    getter = make_getter(tmp_path, None, default=None)
    # the views were created before the tickets were stored; the export recreates them
    getter.export_views_to_excel(tmp_path / "out.xlsx")
    with getter.store.engine.connect() as conn:
        columns = conn.execute(
            text("SELECT * FROM weekly_support_summary LIMIT 0")
        ).keys()
    assert {"open_incident", "open_service_request"} <= set(columns)


def test_config_loads_mapping_patterns(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "request_type_categories:\n"
        "  Incident:\n"
        "    - issue_type: Incident\n"
        "  HW:\n"
        "    - '%Hardware%'\n",
        encoding="utf-8",
    )
    assert Config.load(path).request_type_categories == {
        "Incident": [{"issue_type": "Incident"}],
        "HW": ["%Hardware%"],
    }


def test_unknown_pattern_key_is_rejected(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "request_type_categories:\n  X:\n    - issuetype: Incident\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="issuetype"):
        Config.load(path)


def make_label_getter(tmp_path, categories, default="Other"):
    config = Config(
        db_path=tmp_path / "test.db",
        support_issue_types=[],
        request_type_categories=categories,
        request_type_category_default=default,
    )
    getter = SupportStatsGetter(config)
    day = datetime.datetime(2025, 3, 7)
    tickets = [
        ("T-hw-lobster", "Incident", "Hardware broken", "2ndLevel,DIS-Lobster"),
        ("T-sw-lobster", "Support", "Software question", "dis-lobster"),
        ("T-hw", "Support", "Hardware order", "1stLevel"),
        ("T-none", "Support", "Software license", None),
    ]
    getter.store.insert_many(
        getter.store.jira_tickets,
        [
            {
                "id": ticket_id,
                "key": ticket_id,
                "status": "Open",
                "created": day,
                "updated": day,
                "created_week": "2025.10",
                "active_weeks": "2025.10",
                "issuetype": issuetype,
                "request_type": request_type,
                "labels": labels,
            }
            for ticket_id, issuetype, request_type, labels in tickets
        ],
    )
    getter.store._create_views()
    return getter


def test_label_pattern_matches_any_label_containing_the_text(tmp_path):
    getter = make_label_getter(tmp_path, {"DIS": [{"label": "lobster"}]})
    assert categories_by_ticket(getter) == {
        "T-hw-lobster": "DIS",
        "T-sw-lobster": "DIS",
        "T-hw": "Other",
        "T-none": "Other",
    }


def test_label_pattern_is_and_connected_with_the_others(tmp_path):
    getter = make_label_getter(
        tmp_path, {"DIS HW": [{"label": "Lobster", "request_type": "%Hardware%"}]}
    )
    assert categories_by_ticket(getter)["T-hw-lobster"] == "DIS HW"
    assert categories_by_ticket(getter)["T-sw-lobster"] == "Other"


def test_rule_list_is_tried_top_down_and_repeats_categories(tmp_path):
    getter = make_label_getter(
        tmp_path,
        [
            {"category": "HW", "issue_type": "Incident", "request_type": "%Hardware%"},
            {"category": "DIS", "label": "lobster"},
            {"category": "HW", "request_type": "%Hardware%"},
        ],
    )
    assert categories_by_ticket(getter) == {
        "T-hw-lobster": "HW",
        "T-sw-lobster": "DIS",
        "T-hw": "HW",
        "T-none": "Other",
    }
    with getter.store.engine.connect() as conn:
        row = conn.execute(
            text("SELECT * FROM weekly_support_summary WHERE week = '2025.10'")
        ).mappings().one()
    # one column per category, even when it appears in several rules
    assert (row["open_hw"], row["open_dis"], row["open_other"]) == (2, 1, 1)


def test_config_loads_rule_list(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "request_type_categories:\n"
        "  - category: DIS\n"
        "    label: lobster\n"
        "  - category: HW\n"
        "    request_type: '%Hardware%'\n",
        encoding="utf-8",
    )
    assert Config.load(path).request_type_categories == [
        {"category": "DIS", "label": "lobster"},
        {"category": "HW", "request_type": "%Hardware%"},
    ]


@pytest.mark.parametrize(
    "rules",
    [
        "  - label: lobster\n",
        "  - category: DIS\n",
        "  - category: DIS\n    label: 'a,b'\n",
    ],
)
def test_invalid_rules_are_rejected(tmp_path, rules):
    path = tmp_path / "config.yaml"
    path.write_text("request_type_categories:\n" + rules, encoding="utf-8")
    with pytest.raises(ValueError):
        Config.load(path)
