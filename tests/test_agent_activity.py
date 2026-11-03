import datetime
import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from SupportStatsGetter import Config, SupportStatsGetter

DAY = datetime.datetime(2025, 3, 4, 10, 0)  # ISO week 2025.10


def make_getter(tmp_path):
    getter = SupportStatsGetter(
        Config(db_path=tmp_path / "test.db", internal_email_domains=["agents.example"])
    )
    getter.store.insert_many(
        getter.store.jira_tickets,
        [
            {"id": t, "key": t, "status": "Open", "created": DAY, "updated": DAY}
            for t in ("T-1", "T-2", "T-3")
        ],
    )
    return getter


def comment(cid, ticket, author, internal, public=True):
    return {"id": cid, "ticket_id": ticket, "author": author, "created": DAY,
            "internal_user": internal, "public": public}


def history(hid, author, items):
    return {"id": hid, "author": author, "created": DAY,
            "items": [{"field": f, "fromString": "a", "toString": "b"} for f in items]}


def activity(getter):
    with getter.store.engine.connect() as conn:
        rows = conn.execute(text("SELECT * FROM agent_weekly_activity")).mappings().all()
    return {r["agent"]: dict(r) for r in rows}


def test_weekly_activity_per_agent(tmp_path):
    getter = make_getter(tmp_path)
    getter.store.insert_many(
        getter.store.jira_comments,
        [
            comment("c1", "T-1", "Ann", True),
            comment("c2", "T-1", "Ann", True, public=False),
            comment("c3", "T-2", "Customer", False),
        ],
    )
    agent = {"displayName": "Ann", "emailAddress": "ann@agents.example"}
    getter.store_jira_changelog("T-2", [
        history("h1", agent, ["status", "resolution"]),  # one status change
        history("h2", agent, ["assignee"]),
    ])
    getter.store_jira_changelog("T-3", [
        history("h3", agent, ["priority"]),
        history("h4", {"displayName": "Automation for Jira", "accountType": "app"}, ["status"]),
        history("h5", {"displayName": "Customer", "accountType": "customer"}, ["status"]),
    ])

    rows = activity(getter)
    assert set(rows) == {"Ann"}
    ann = rows["Ann"]
    assert ann["week"] == "2025.10"
    assert ann["agent_email"] == "ann@agents.example"
    assert ann["tickets_total"] == 3
    assert ann["tickets_commented"] == 1
    assert ann["tickets_status_changed"] == 1
    assert ann["tickets_assignment_changed"] == 1
    assert ann["tickets_other_changes"] == 1
    assert ann["comments"] == 2
    assert ann["public_comments"] == 1
    assert ann["status_changes"] == 1
    assert ann["assignment_changes"] == 1
    assert ann["other_changes"] == 1


def test_changelog_without_flag_uses_internal_comment_authors(tmp_path):
    getter = make_getter(tmp_path)
    getter.store.insert_many(getter.store.jira_comments, [comment("c1", "T-1", "Ann", True)])
    # rows stored before the internal_user column existed
    getter.store.insert_many(
        getter.store.jira_changelog,
        [
            {"id": "h1-0", "history_id": "h1", "ticket_id": "T-2", "author": "Ann",
             "created": DAY, "field": "status"},
            {"id": "h2-0", "history_id": "h2", "ticket_id": "T-3", "author": "Bob",
             "created": DAY, "field": "status"},
        ],
    )
    rows = activity(getter)
    assert set(rows) == {"Ann"}
    assert rows["Ann"]["tickets_total"] == 2
    assert rows["Ann"]["tickets_status_changed"] == 1
