import datetime
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from SupportStatsGetter import Config, SupportStatsGetter


@pytest.mark.parametrize(
    "thresholds, expected",
    [
        ({}, (1, 1, 1)),  # defaults 5 / 30 / 5: 40 days open, 8 weekdays no contact
        ({"aged_days_short": 39, "aged_days_long": 40, "no_contact_days": 8}, (1, 0, 0)),
        ({"aged_days_short": 40, "aged_days_long": 50, "no_contact_days": 7}, (0, 0, 1)),
    ],
)
def test_backlog_thresholds_are_configurable(tmp_path, thresholds, expected):
    config = Config(db_path=tmp_path / "test.db", support_issue_types=[], **thresholds)
    getter = SupportStatsGetter(config)
    week = "2025.10"
    day = datetime.datetime(2025, 3, 7)  # a Friday
    getter.store.insert_many(
        getter.store.jira_tickets,
        [{"id": "T-1", "key": "T-1", "status": "Open", "created": day,
          "updated": day, "created_week": week, "issuetype": "Incident"}],
    )
    getter.store.insert_many(
        getter.store.jira_calendar,
        [{"record_id": "rec-1", "ticket_id": "T-1", "ticket_key": "T-1", "date": day,
          "end_status": "Open", "end_issuetype": "Incident", "total_days_open": 40,
          "weekdays_days_since_last_contact": 8, "is_junk": False, "cw": week,
          "weekday": 5}],
    )
    with getter.store.engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT aged_5_plus, aged_30_plus, over5_days_no_contact "
                "FROM weekly_support_summary WHERE week = :week"
            ),
            {"week": week},
        ).one()
    assert tuple(row) == expected


def test_dashboard_labels_show_thresholds(tmp_path):
    from openpyxl import load_workbook

    config = Config(
        db_path=tmp_path / "test.db", aged_days_short=7, aged_days_long=60, no_contact_days=3
    )
    getter = SupportStatsGetter(config)
    output = tmp_path / "out.xlsx"
    getter.export_views_to_excel(output)
    values = {
        cell.value
        for row in load_workbook(output)["Dashboard"].iter_rows()
        for cell in row
        if isinstance(cell.value, str)
    }
    assert {"Open > 7 days", "Open > 60 days", "> 3 days no contact"} <= values
