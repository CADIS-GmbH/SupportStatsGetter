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
