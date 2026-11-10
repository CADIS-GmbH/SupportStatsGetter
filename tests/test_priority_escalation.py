import datetime
from pathlib import Path
import sys

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from SupportStatsGetter import Config, SupportStatsGetter, make_jira_ticket_calendar


def test_recalculate_all_preserves_priority_escalation(tmp_path):
    config = Config(db_path=tmp_path / "test.db")
    getter = SupportStatsGetter(config)

    ticket_id = "T-1"
    created = datetime.datetime(2023, 1, 1, 0, 0)
    change_time = datetime.datetime(2023, 1, 2, 12, 0)

    getter.store.insert_many(getter.store.jira_tickets, [
        {
            "id": ticket_id,
            "key": ticket_id,
            "summary": "Test",
            "status": "Open",
            "priority": "Critical",
            "priority_pos": 200,
            "created": created,
            "updated": change_time,
        }
    ])

    getter.store.insert_many(getter.store.jira_changelog, [
        {
            "id": "cl1",
            "history_id": "cl1",
            "ticket_id": ticket_id,
            "author": "Agent",
            "author_email": "agent@example.com",
            "internal_user": True,
            "created": change_time,
            "field": "priority",
            "fieldtype": None,
            "from_value": None,
            "from_string": "Normal",
            "to_value": None,
            "to_string": "Critical",
        }
    ])

    getter.store.insert_many(getter.store.jira_calendar, [
        {
            "record_id": "r1",
            "ticket_id": ticket_id,
            "ticket_key": ticket_id,
            "date": change_time.date(),
            "comments": 0,
            "public_comments": 0,
            "start_assigned_user": None,
            "end_assigned_user": None,
            "start_assigned_user_email": None,
            "end_assigned_user_email": None,
            "did_escalate_priority": True,
            "did_escalate_cs": False,
            "did_escalate_management": False,
            "start_priority_txt": "Normal",
            "start_priority_pos": 0,
            "end_priority_txt": "Critical",
            "end_priority_pos": 200,
            "time_spent_min": 0,
            "start_status": "Open",
            "end_status": "Open",
            "did_resolve": False,
            "total_days_open": 0,
            "weekdays_days_since_last_contact": 0,
            "adjusted_resolution_days": 0,
            "adjusted_resolution_weekdays": 0,
            "is_junk": False,
            "cw": None,
            "weekday": change_time.date().isoweekday(),
            "date_modified": change_time,
        }
    ])

    with getter.store.engine.connect() as conn:
        before = (
            conn.execute(
                select(getter.store.jira_calendar.c.did_escalate_priority).where(
                    getter.store.jira_calendar.c.ticket_id == ticket_id
                )
            )
            .fetchone()
        )
    assert before[0] is True

    getter.recalculate_all()

    with getter.store.engine.connect() as conn:
        rows = (
            conn.execute(
                select(
                    getter.store.jira_calendar.c.date,
                    getter.store.jira_calendar.c.did_escalate_priority,
                ).where(getter.store.jira_calendar.c.ticket_id == ticket_id)
            )
            .mappings()
            .all()
        )
    assert any(
        r["date"].date() == change_time.date() and r["did_escalate_priority"]
        for r in rows
    )


def test_recalculate_all_detects_priority_escalation_from_history(tmp_path):
    config = Config(db_path=tmp_path / "test.db")
    getter = SupportStatsGetter(config)

    ticket_id = "T-1"
    created = datetime.datetime(2023, 1, 1, 0, 0)
    change_time = datetime.datetime(2023, 1, 2, 12, 0)

    getter.store.insert_many(getter.store.jira_tickets, [
        {
            "id": ticket_id,
            "key": ticket_id,
            "summary": "Test",
            "status": "Open",
            "priority": "Normal",
            "priority_pos": 0,
            "created": created,
            "updated": change_time,
        }
    ])

    cl = {
        "id": "h1-0",
        "history_id": "h1",
        "ticket_id": ticket_id,
        "author": "Agent",
        "created": change_time,
        "field": "priority",
        "from_string": "Normal",
        "to_string": "Critical",
    }

    getter.store.insert_many(getter.store.jira_changelog, [cl])

    getter.recalculate_all()

    with getter.store.engine.connect() as conn:
        rows = (
            conn.execute(
                select(
                    getter.store.jira_calendar.c.date,
                    getter.store.jira_calendar.c.start_priority_txt,
                    getter.store.jira_calendar.c.end_priority_txt,
                    getter.store.jira_calendar.c.start_priority_pos,
                    getter.store.jira_calendar.c.end_priority_pos,
                    getter.store.jira_calendar.c.did_escalate_priority,
                ).where(getter.store.jira_calendar.c.ticket_id == ticket_id)
            )
            .mappings()
            .all()
        )

    assert any(
        r["date"].date() == change_time.date()
        and r["start_priority_txt"] == "Normal"
        and r["end_priority_txt"] == "Critical"
        and r["start_priority_pos"] == 0
        and r["end_priority_pos"] == 200
        and r["did_escalate_priority"]
        for r in rows
    )


def test_priority_escalation_ignores_quick_reversal():
    issue = {
        "id": "1",
        "key": "TST-1",
        "created": datetime.datetime(2025, 1, 1, 9, 0, tzinfo=datetime.UTC),
        "resolved": datetime.datetime(2025, 1, 1, 18, 0, tzinfo=datetime.UTC),
        "fields": {"priority": {"name": "Normal"}},
    }
    changelog = [
        {
            "created": datetime.datetime(2025, 1, 1, 10, 0, tzinfo=datetime.UTC),
            "items": [
                {"field": "priority", "fromString": "Normal", "toString": "Critical"}
            ],
        },
        {
            "created": datetime.datetime(2025, 1, 1, 10, 3, tzinfo=datetime.UTC),
            "items": [
                {"field": "priority", "fromString": "Critical", "toString": "Normal"}
            ],
        },
    ]

    rows = make_jira_ticket_calendar(
        issue,
        [],
        [],
        changelog,
        priority_deescalation_ignore_minutes=5,
    )

    assert rows[0]["did_escalate_priority"] is False


def test_priority_escalation_counts_after_grace_period():
    issue = {
        "id": "1",
        "key": "TST-1",
        "created": datetime.datetime(2025, 1, 1, 9, 0, tzinfo=datetime.UTC),
        "resolved": datetime.datetime(2025, 1, 1, 18, 0, tzinfo=datetime.UTC),
        "fields": {"priority": {"name": "Normal"}},
    }
    changelog = [
        {
            "created": datetime.datetime(2025, 1, 1, 10, 0, tzinfo=datetime.UTC),
            "items": [
                {"field": "priority", "fromString": "Normal", "toString": "Critical"}
            ],
        },
        {
            "created": datetime.datetime(2025, 1, 1, 10, 10, tzinfo=datetime.UTC),
            "items": [
                {"field": "priority", "fromString": "Critical", "toString": "Normal"}
            ],
        },
    ]

    rows = make_jira_ticket_calendar(
        issue,
        [],
        [],
        changelog,
        priority_deescalation_ignore_minutes=5,
    )

    assert rows[0]["did_escalate_priority"] is True
