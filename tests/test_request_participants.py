import datetime
from sqlalchemy import select

from SupportStatsGetter import Config, SupportStatsGetter


def test_store_request_participants(tmp_path):
    config = Config(db_path=tmp_path / "test.db")
    getter = SupportStatsGetter(config)
    issue = {
        "id": "1",
        "key": "T-1",
        "created": datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC),
        "updated": datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC),
        "fields": {
            "summary": "Test",
            "requestParticipants": [
                {"emailAddress": "a@example.com", "displayName": "A"},
                {"displayName": "B"},
            ],
        },
    }
    getter.store_jira_tickets([issue])
    with getter.store.engine.connect() as conn:
        stored = (
            conn.execute(
                select(getter.store.jira_tickets.c.request_participants).where(
                    getter.store.jira_tickets.c.id == "1"
                )
            ).scalar_one()
        )
    assert stored == "a@example.com,B"


def test_recalculate_keeps_participants_and_creator(tmp_path):
    config = Config(db_path=tmp_path / "test.db")
    getter = SupportStatsGetter(config)
    issue = {
        "id": "1",
        "key": "T-1",
        "created": datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC),
        "updated": datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC),
        "fields": {
            "summary": "Test",
            "status": {"name": "Open"},
            "creator":{"displayName": "C", "emailAddress": "c@example.com"},
            "requestParticipants": [
                {"emailAddress": "a@example.com", "displayName": "A"},
                {"displayName": "B"},
            ],
        },
    }
    getter.store_jira_tickets([issue])
    getter.recalculate_tickets(["1"])
    t = getter.store.jira_tickets.c
    with getter.store.engine.connect() as conn:
        row = conn.execute(
            select(t.request_participants, t.creator, t.creator_email).where(t.id == "1")
        ).one()
    assert tuple(row) == ("a@example.com,B", "C", "c@example.com")
