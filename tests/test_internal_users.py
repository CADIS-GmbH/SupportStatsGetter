import datetime
import sys
from pathlib import Path

import requests
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import SupportStatsGetter as SupportStatsGetter_module
from SupportStatsGetter import Config, SupportStatsGetter, is_internal_user


def test_internal_email_domains(tmp_path):
    config = Config(db_path=tmp_path / "test.db", internal_email_domains=["manus.plus", "@Other.org"])
    SupportStatsGetter(config)
    assert is_internal_user({"emailAddress": "agent@manus.plus"})
    assert is_internal_user({"emailAddress": "Agent@MANUS.PLUS"})
    assert is_internal_user({"emailAddress": "x@other.org"})
    assert not is_internal_user({"emailAddress": "customer@example.com"})
    assert not is_internal_user({"emailAddress": "x@manus.plus.example.com"})
    assert not is_internal_user({"emailAddress": "x@notmanus.plus"})
    # without an email the account type still decides
    assert is_internal_user({"accountType": "atlassian"})


def test_config_loads_internal_email_domains(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("internal_email_domains:\n  - manus.plus\n", encoding="utf-8")
    assert Config.load(path).internal_email_domains == ["manus.plus"]


class FakeResponse:
    def __init__(self, data, status_code=200):
        self._data, self.status_code = data, status_code

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


def group_config(tmp_path):
    return Config(
        db_path=tmp_path / "test.db",
        jira_base_url="https://jira.example",
        internal_email_domains=["manus.plus"],
        internal_groups=["service-desk-agents"],
    )


def test_internal_groups_decide_without_matching_email(tmp_path, monkeypatch):
    pages = [
        {"values": [{"accountId": "a1", "displayName": "Agent One"}], "isLast": False},
        {"values": [{"accountId": "a2", "displayName": "Agent Two"},
                    {"accountId": "app1", "displayName": "Automation for Jira",
                     "accountType": "app"}], "isLast": True},
    ]
    monkeypatch.setattr(SupportStatsGetter_module.requests.Session, "get",
                        lambda *a, **kw: FakeResponse(pages.pop(0)))
    getter = SupportStatsGetter(group_config(tmp_path))
    getter.load_internal_groups()

    assert is_internal_user({"accountId": "a2", "accountType": "atlassian"})
    # full Jira account, but not in the group
    assert not is_internal_user({"accountId": "x9", "accountType": "atlassian"})
    # stored records have no account id: the display name decides
    assert is_internal_user({"displayName": "Agent One"})
    assert not is_internal_user({"displayName": "Donna"})
    # apps in the group are not agents
    assert not is_internal_user({"accountId": "app1", "accountType": "app"})
    assert not is_internal_user({"displayName": "Automation for Jira"})
    # the email domain still counts
    assert is_internal_user({"accountId": "x9", "emailAddress": "new@manus.plus"})

    # a later run without Jira access keeps the stored members
    def fail(*a, **kw):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(SupportStatsGetter_module.requests.Session, "get", fail)
    getter = SupportStatsGetter(group_config(tmp_path))
    getter.load_internal_groups()
    assert is_internal_user({"accountId": "a1"})


def test_recalculate_applies_internal_groups(tmp_path):
    day = datetime.datetime(2025, 3, 4, 10, 0)
    getter = SupportStatsGetter(group_config(tmp_path))
    getter.store.replace_internal_group_members("service-desk-agents", [("a1", "Agent One")])
    getter = SupportStatsGetter(group_config(tmp_path))  # picks up the stored members
    store = getter.store
    store.insert_many(store.jira_tickets, [{"id": "T-1", "key": "T-1", "status": "Open",
                                            "created": day, "updated": day}])
    # flagged internal by the old account type rule
    store.insert_many(store.jira_comments, [
        {"id": "c1", "ticket_id": "T-1", "author": "Agent One", "created": day, "internal_user": True},
        {"id": "c2", "ticket_id": "T-1", "author": "Donna", "created": day, "internal_user": True},
    ])
    store.insert_many(store.jira_changelog, [
        {"id": "h1-0", "history_id": "h1", "ticket_id": "T-1", "author": "Donna",
         "created": day, "field": "status", "internal_user": True},
    ])
    getter.recalculate_tickets(["T-1"])
    with store.engine.connect() as conn:
        comments = dict(conn.execute(text("SELECT author, internal_user FROM jira_comments")).all())
        changelog = conn.execute(text("SELECT internal_user FROM jira_changelog")).scalar_one()
    assert comments == {"Agent One": 1, "Donna": 0}
    assert changelog == 0
