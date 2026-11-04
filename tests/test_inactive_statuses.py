import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from SupportStatsGetter import Config, SupportStatsGetter, calculate_kpi_fields


def change(when, frm, to):
    return {"created": when, "items": [{"field": "status", "fromString": frm, "toString": to}]}


ISSUE = {
    "id": "1",
    "created": datetime.datetime(2026, 10, 5, 9, 0),  # Monday, week 41
    "updated": datetime.datetime(2026, 10, 29, 9, 0),
    "fields": {"status": {"name": "Resolved"}},
}
CHANGELOG = [
    # in development from Tuesday of week 41 until Wednesday of week 44
    change(datetime.datetime(2026, 10, 6, 9, 0), "Waiting for support", "Waiting for development"),
    change(datetime.datetime(2026, 10, 28, 9, 0), "Waiting for development", "Waiting for support"),
    change(datetime.datetime(2026, 10, 29, 9, 0), "Waiting for support", "Resolved"),
]


def weeks(tmp_path, inactive, changelog=CHANGELOG):
    SupportStatsGetter(Config(db_path=tmp_path / "test.db", inactive_statuses=inactive))
    return calculate_kpi_fields(ISSUE, [], changelog)["ActiveWeeks"]


def test_weeks_spent_in_inactive_statuses_are_not_active(tmp_path):
    assert weeks(tmp_path, ["waiting for Development"]) == "2026.41,2026.44"


def test_without_inactive_statuses_every_week_counts(tmp_path):
    assert weeks(tmp_path, []) == "2026.41,2026.42,2026.43,2026.44"


def test_change_at_week_start_belongs_to_the_new_week(tmp_path):
    changelog = [
        change(datetime.datetime(2026, 10, 12, 0, 0), "Waiting for support", "Waiting for development"),
        change(datetime.datetime(2026, 10, 29, 9, 0), "Waiting for development", "Resolved"),
    ]
    # active only in week 41; week 44 ends in development and then resolved
    assert weeks(tmp_path, ["Waiting for development"], changelog) == "2026.41"
