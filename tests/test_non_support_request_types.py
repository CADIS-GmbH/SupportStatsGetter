import datetime
import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from SupportStatsGetter import Config, SupportStatsGetter


def test_aged_counts_exclude_configured_request_types(tmp_path):
    config = Config(
        db_path=tmp_path / "test.db",
        non_support_request_types=[
            "Maintenance & Service Operations",
            "Product trial questions",
            "Custom Non Support",
        ],
    )
    getter = SupportStatsGetter(config)

    reporting_week = "2025.10"
    day = datetime.datetime(2025, 3, 7)

    getter.store.insert_many(
        getter.store.jira_tickets,
        [
            {
                "id": "T-include",
                "key": "T-include",
                "summary": "Counted ticket",
                "status": "Open",
                "created": day,
                "updated": day,
                "created_week": reporting_week,
                "request_type": "Support question",
                "work_type": "Support",
            },
            {
                "id": "T-custom",
                "key": "T-custom",
                "summary": "Excluded by config",
                "status": "Open",
                "created": day,
                "updated": day,
                "created_week": reporting_week,
                "request_type": "Custom Non Support",
                "work_type": "Support",
            },
            {
                "id": "T-maint",
                "key": "T-maint",
                "summary": "Excluded by default",
                "status": "Open",
                "created": day,
                "updated": day,
                "created_week": reporting_week,
                "request_type": "Maintenance & Service Operations",
                "work_type": "Support",
            },
            {
                "id": "T-feature",
                "key": "T-feature",
                "summary": "Excluded by work type",
                "status": "Open",
                "created": day,
                "updated": day,
                "created_week": reporting_week,
                "request_type": "Support question",
                "work_type": "Feature",
            },
        ],
    )

    calendar_base = {
        "date": day,
        "comments": 0,
        "public_comments": 0,
        "start_assigned_user": None,
        "end_assigned_user": None,
        "start_assigned_user_email": None,
        "end_assigned_user_email": None,
        "start_request_type": None,
        "start_work_type": None,
        "start_issue_category": None,
        "end_issue_category": None,
        "did_escalate_priority": False,
        "did_escalate_cs": False,
        "did_escalate_management": False,
        "start_priority_txt": None,
        "start_priority_pos": 0,
        "end_priority_txt": None,
        "end_priority_pos": 0,
        "time_spent_min": 0,
        "start_status": "Open",
        "end_status": "In Progress",
        "did_resolve": False,
        "total_days_open": 40,
        "weekdays_days_since_last_contact": 0,
        "adjusted_resolution_days": 0,
        "adjusted_resolution_weekdays": 0,
        "is_junk": False,
        "cw": reporting_week,
        "weekday": 5,
        "date_modified": day,
    }

    getter.store.insert_many(
        getter.store.jira_calendar,
        [
            {
                **calendar_base,
                "record_id": "rec-include",
                "ticket_id": "T-include",
                "ticket_key": "T-include",
                "end_request_type": "Support question",
                "end_work_type": "Support",
            },
            {
                **calendar_base,
                "record_id": "rec-custom",
                "ticket_id": "T-custom",
                "ticket_key": "T-custom",
                "end_request_type": "Custom Non Support",
                "end_work_type": "Support",
            },
            {
                **calendar_base,
                "record_id": "rec-maint",
                "ticket_id": "T-maint",
                "ticket_key": "T-maint",
                "end_request_type": "Maintenance & Service Operations",
                "end_work_type": "Support",
            },
            {
                **calendar_base,
                "record_id": "rec-feature",
                "ticket_id": "T-feature",
                "ticket_key": "T-feature",
                "end_request_type": "Support question",
                "end_work_type": "Feature",
            },
        ],
    )

    with getter.store.engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT aged_5_plus, aged_30_plus, new_tickets, overall_open "
                    "FROM weekly_support_summary WHERE week = :week"
                ),
                {"week": reporting_week},
            )
            .mappings()
            .one()
        )

    assert row["aged_5_plus"] == 1
    assert row["aged_30_plus"] == 1
    assert row["new_tickets"] == 1
    assert row["overall_open"] == 1
