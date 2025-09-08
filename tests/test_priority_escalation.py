import datetime
from pathlib import Path
import sys

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from SupportStatsGetter import Config, SupportStatsGetter
from scripts.import_teamsupport import map_history


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
            "start_assigned_group": "1stLevel",
            "end_assigned_group": "1stLevel",
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

    hist = {
        "ID": "h1",
        "Description": "Changed severity from 'Normal' to 'Critical'",
        "Actor": "Agent",
        "CreatorID": "0",
        "DateCreated": change_time.isoformat(),
    }
    cl = map_history(hist, ticket_id)
    cl["created"] = cl["created"].replace(tzinfo=None)

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
