import datetime
import sys
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from SupportStatsGetter import Config, SupportStatsGetter


def test_recalculate_all_preserves_response_dates_for_comments_without_email(tmp_path):
    config = Config(db_path=tmp_path / "test.db")
    getter = SupportStatsGetter(config, use_sqlite=True)

    ticket_id = "T-1"
    created = datetime.datetime(2023, 1, 1, 0, 0)
    comment_time = created + datetime.timedelta(hours=1)

    getter.store.insert_many(getter.store.jira_tickets, [
        {
            "id": ticket_id,
            "key": ticket_id,
            "summary": "Test",
            "status": "Open",
            "created": created,
            "updated": comment_time,
        }
    ])
    getter.store.insert_many(getter.store.jira_comments, [
        {
            "id": "c0",
            "ticket_id": ticket_id,
            "issue_key": ticket_id,
            "author": "Customer",
            "author_email": "cust@example.com",
            "body": "Initial",
            "public": True,
            "internal_user": False,
            "created": created,
            "updated": created,
        },
        {
            "id": "c1",
            "ticket_id": ticket_id,
            "issue_key": ticket_id,
            "author": "Agent",
            "author_email": None,
            "body": "Response",
            "public": True,
            "internal_user": True,
            "created": comment_time,
            "updated": comment_time,
        },
    ])

    getter.recalculate_all()

    with getter.store.engine.connect() as conn:
        row = (
            conn.execute(
                select(
                    getter.store.jira_tickets.c.first_response_date,
                    getter.store.jira_tickets.c.last_response_date,
                ).where(getter.store.jira_tickets.c.id == ticket_id)
            )
            .mappings()
            .fetchone()
        )

    assert row["first_response_date"] == comment_time
    assert row["last_response_date"] == comment_time
